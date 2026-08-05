#!/usr/bin/env python3
"""Deterministic local setup operations for the enterprise knowledge plugin."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from kb_core import (
    COMPANY_CONFIG_SCHEMA as COMPANY_SCHEMA,
    ORGANIZATION_SCHEMA as ORG_SCHEMA,
    PROJECT_BINDING_SCHEMA,
    CoreError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_path,
    load_json,
    mapping_value,
    sha256_file,
    validate_company_config,
)
from membership_policy import (
    MEMBERSHIP_VERIFICATION_SCHEMA,
    SHARE_SCOPE_SCHEMA,
    MembershipError,
    build_membership_plan,
    canonical_hash,
    execute_membership_change,
    human_preview,
    resolve_share_scope,
    validate_policy,
    validate_share_scope,
    verify_employee_access,
    verify_member_readback,
)
from feishu_company_adapter import CompanyAdapterError, LarkMembershipAdapter


PLUGIN_VERSION = "0.5.0"
SCHEMA = "kb-setup-state/v3"
PROJECT_SKILL_NAME = "enterprise-knowledge-base"
LEGACY_SKILL_NAMES = ("manage-knowledge-base", "collect-local-knowledge", "publish-enterprise-knowledge")
V041_AGENTS_SHA256 = "5bd24ff9ae2cfbae96e5ebcfe93d9f0154e84cad73471053d261c670fccd64d8"
VISIBLE_DIRECTORIES = (
    "00_收件箱",
    "10_来源",
    "20_知识",
    "30_导航",
    "90_归档",
)
SYSTEM_DIRECTORIES = (
    ".kb/config",
    ".kb/mappings",
    ".kb/state",
    ".kb/logs",
    ".kb/temp",
)
ROLES = {"admin", "employee", "local"}
PUBLISH_POLICIES = {"members"}
class SetupError(RuntimeError):
    pass


def project_binding(vault: Path) -> dict[str, Any]:
    try:
        value = load_json(vault / ".kb/config/project-binding.json", {})
        error = ""
    except CoreError as exc:
        value = {}
        error = str(exc)
    resolved = canonical_path(vault)
    valid = bool(
        isinstance(value, dict)
        and value.get("schema") == PROJECT_BINDING_SCHEMA
        and value.get("project_root") == resolved
        and value.get("vault_root") == resolved
        and value.get("binding_mode") == "same-root"
    )
    project_specific = bool(
        valid
        and value.get("skill_name") == PROJECT_SKILL_NAME
        and value.get("skill_scope") == "project-only"
    )
    return {
        "valid": valid,
        "project_specific": project_specific,
        "value": value,
        "resolved_root": resolved,
        "error": error,
    }


def bind_project(vault: Path, project: Path) -> dict[str, Any]:
    vault = vault.expanduser().resolve()
    project = project.expanduser().resolve()
    if canonical_path(vault) != canonical_path(project):
        raise SetupError("The Codex project root and Obsidian Vault root must be identical.")
    if not (vault / "AGENTS.md").is_file() or not (vault / ".kb").is_dir():
        raise SetupError("Initialize the knowledge Vault before binding the project.")
    value = {
        "schema": PROJECT_BINDING_SCHEMA,
        "binding_mode": "same-root",
        "project_root": canonical_path(project),
        "vault_root": canonical_path(vault),
        "skill_name": PROJECT_SKILL_NAME,
        "skill_scope": "project-only",
    }
    atomic_write_json(vault / ".kb/config/project-binding.json", value)
    return {"ok": True, "binding": value}


def current_project() -> Path:
    """Use the active Codex working directory as the only setup target."""
    return Path.cwd().resolve()


def legacy_layout(vault: Path) -> bool:
    return bool(
        (vault / "AGENTS.md").is_file()
        and (vault / ".kb").is_dir()
        and all(
            (vault / ".agents/skills" / name / "SKILL.md").is_file()
            for name in LEGACY_SKILL_NAMES
        )
    )


def managed_agents_hashes() -> set[str]:
    template = template_agents()
    hashes = {V041_AGENTS_SHA256}
    if template.is_file():
        hashes.add(sha256_file(template))
    return hashes


def inspect_project(vault: Path) -> dict[str, Any]:
    """Build a read-only, fail-closed initialization preview."""
    vault = vault.resolve()
    conflicts: list[dict[str, str]] = []
    if not vault.is_dir():
        conflicts.append({"code": "project-root-not-directory", "path": "."})
    binding = project_binding(vault)
    legacy = legacy_layout(vault)
    configured = bool(
        (vault / "AGENTS.md").is_file()
        and (vault / ".kb").is_dir()
        and binding["valid"]
    )

    for relative in (*VISIBLE_DIRECTORIES, *SYSTEM_DIRECTORIES):
        path = vault / relative
        if path.exists() and not path.is_dir():
            conflicts.append({"code": "reserved-path-type-conflict", "path": relative})

    kb = vault / ".kb"
    if kb.exists() and not kb.is_dir():
        conflicts.append({"code": "invalid-kb", "path": ".kb"})
    elif kb.is_dir() and not configured and not legacy:
        conflicts.append({"code": "unrecognized-kb", "path": ".kb"})

    agents = vault / "AGENTS.md"
    if agents.exists() and not agents.is_file():
        conflicts.append({"code": "reserved-path-type-conflict", "path": "AGENTS.md"})
    elif agents.is_file() and not legacy:
        try:
            if sha256_file(agents) not in managed_agents_hashes():
                conflicts.append({"code": "unknown-agents", "path": "AGENTS.md"})
        except OSError:
            conflicts.append({"code": "unreadable-agents", "path": "AGENTS.md"})

    managed_files = (
        ".kb/config/project-binding.json",
        ".kb/config/organization.json",
        ".kb/config/setup-state.json",
        ".kb/mappings/feishu_nodes.json",
        ".kb/mappings/feishu_documents.json",
        ".kb/state/processed_files.json",
        ".kb/state/publish_queue.json",
    )
    for relative in managed_files:
        path = vault / relative
        if path.exists() and not path.is_file():
            conflicts.append({"code": "managed-file-type-conflict", "path": relative})

    ordinary_entries = [
        path.name
        for path in sorted(vault.iterdir(), key=lambda item: item.name.casefold())
        if path.name not in {".kb", ".agents", "AGENTS.md"}
    ] if vault.is_dir() else []
    status = (
        "blocked" if conflicts else "legacy" if legacy else "configured"
        if configured else "ready"
    )
    return {
        "ok": not conflicts,
        "mode": "current-project-initialization",
        "project_root": str(vault),
        "vault_root": str(vault),
        "project_name": vault.name,
        "path_status": status,
        "configured": configured,
        "legacy_skills": list(LEGACY_SKILL_NAMES) if legacy else [],
        "project_binding": binding,
        "ordinary_entries_preserved": ordinary_entries,
        "obsidian_directory_preserved": (vault / ".obsidian").exists(),
        "claudian_directory_preserved": (vault / ".claudian").exists(),
        "network_required": False,
        "system_software_installation": False,
        "conflicts": conflicts,
        "confirmation": "初始化当前 Codex 项目为同根知识库；保留所有普通笔记、附件和 .obsidian",
    }


def inspect_current_project() -> dict[str, Any]:
    return inspect_project(current_project())


def retire_legacy_skills(vault: Path) -> dict[str, Any]:
    vault = vault.resolve()
    sources = [vault / ".agents/skills" / name for name in LEGACY_SKILL_NAMES]
    existing = [path for path in sources if path.is_dir()]
    if not existing:
        return {"migrated": False, "backup": "", "skills": []}
    if len(existing) != len(sources):
        raise SetupError("The legacy three-Skill layout is incomplete.")
    backup = vault / ".kb/legacy-skill-backup/v0.4.1"
    if backup.exists():
        raise SetupError("A legacy Skill backup already exists; inspect it before retrying.")
    template = template_agents()
    agents = vault / "AGENTS.md"
    if not template.is_file() or not agents.is_file():
        raise SetupError("The managed project rules required for migration are missing.")
    backup.mkdir(parents=True)
    backup_agents = backup / "AGENTS.md"
    shutil.copy2(agents, backup_agents)
    moved: list[tuple[Path, Path]] = []
    try:
        for source in sources:
            destination = backup / source.name
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
        atomic_write_bytes(agents, template.read_bytes())
    except Exception:
        atomic_write_bytes(agents, backup_agents.read_bytes())
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
        backup_agents.unlink(missing_ok=True)
        try:
            backup.rmdir()
        except OSError:
            pass
        raise
    return {"migrated": True, "backup": backup.relative_to(vault).as_posix(),
            "skills": list(LEGACY_SKILL_NAMES)}


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[3]


def template_agents() -> Path:
    return plugin_root() / "assets/vault-template/AGENTS.md"


def layout_status(vault: Path) -> dict[str, Any]:
    return {
        "agents": (vault / "AGENTS.md").is_file(),
        "visible": {
            name: (vault / name).is_dir() for name in VISIBLE_DIRECTORIES
        },
        "system": {
            name: (vault / name).is_dir() for name in SYSTEM_DIRECTORIES
        },
    }


def tool_status() -> dict[str, Any]:
    return {
        "platform": platform.system(),
        "python": {
            "ok": sys.version_info >= (3, 10),
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "node": bool(shutil.which("node")),
        "npm": bool(shutil.which("npm")),
        "lark_cli": bool(shutil.which("lark-cli")),
    }


def inspect(vault: Path) -> dict[str, Any]:
    state = load_json(vault / ".kb/config/setup-state.json", {})
    organization = load_json(vault / ".kb/config/organization.json", {})
    return {
        "ok": True,
        "vault": str(vault),
        "exists": vault.exists(),
        "configured": bool(state.get("schema") == SCHEMA),
        "role": organization.get("role", ""),
        "layout": layout_status(vault),
        "project_binding": project_binding(vault),
        "tools": tool_status(),
        "obsidian_runtime_dependency": False,
        "legacy_obsidian_configuration_preserved": (vault / ".obsidian").exists(),
        "legacy_claudian_configuration_preserved": (vault / ".claudian").exists(),
        "role_options": [
            "我是飞书管理员，首次为公司部署",
            "我是普通员工，加入公司已有知识库",
            "暂时只使用本地知识库",
        ],
    }


def initialize(
    vault: Path,
    role: str,
    company_name: str,
    *,
    confirmed: bool = True,
) -> dict[str, Any]:
    """Initialize one already-open Codex project without network or app writes."""
    if not confirmed:
        raise SetupError("Current-project initialization requires explicit confirmation.")
    if role not in ROLES:
        raise SetupError("Unsupported setup role.")
    vault = vault.resolve()
    vault.mkdir(parents=True, exist_ok=True)
    preview = inspect_project(vault)
    if not preview["ok"]:
        codes = ", ".join(item["code"] for item in preview["conflicts"])
        raise SetupError(f"Current project initialization is blocked: {codes}")

    existing_organization = load_json(vault / ".kb/config/organization.json", {})
    if existing_organization:
        existing_role = str(existing_organization.get("role", ""))
        if existing_role not in ROLES:
            raise SetupError("The existing organization role is invalid.")
        if existing_role != role:
            raise SetupError("The requested role conflicts with the existing project role.")

    for relative in (*VISIBLE_DIRECTORIES, *SYSTEM_DIRECTORIES):
        (vault / relative).mkdir(parents=True, exist_ok=True)

    agents = vault / "AGENTS.md"
    template = template_agents()
    if not template.is_file():
        raise SetupError("The packaged Vault rules template is missing.")
    if preview["path_status"] == "legacy":
        legacy_migration = retire_legacy_skills(vault)
    else:
        legacy_migration = {"migrated": False, "backup": "", "skills": []}
        if not agents.exists() or sha256_file(agents) != sha256_file(template):
            atomic_write_bytes(agents, template.read_bytes())

    bind_project(vault, vault)

    organization = {
        "schema": ORG_SCHEMA,
        "role": role,
        "company_name": company_name.strip(),
        "feishu_enabled": role != "local",
        "publish_policy": "members",
        "share_scope": {
            "schema": SHARE_SCOPE_SCHEMA,
            "type": "all-employees",
            "selectors": [],
            "resolution_status": "pending",
        },
        "membership_verified": False,
        "employee_access_verified": False,
    }
    organization_path = vault / ".kb/config/organization.json"
    if not organization_path.exists():
        atomic_write_json(organization_path, organization)
    setup_path = vault / ".kb/config/setup-state.json"
    if not setup_path.exists():
        atomic_write_json(
            setup_path,
            {"schema": SCHEMA, "role": role, "completed_stages": ["vault_initialized"]},
        )
    defaults = {
        ".kb/mappings/feishu_nodes.json": {
            "version": 2,
            "space_name": "",
            "space_id": "",
            "nodes": [],
        },
        ".kb/mappings/feishu_documents.json": {"version": 2, "documents": []},
        ".kb/state/processed_files.json": {"version": 2, "files": []},
        ".kb/state/publish_queue.json": {"version": 2, "items": []},
    }
    for relative, value in defaults.items():
        path = vault / relative
        if not path.exists():
            atomic_write_json(path, value)
    result = inspect(vault)
    result.update(
        {
            "mode": "current-project-initialization",
            "network_requests": 0,
            "system_software_installations": 0,
            "obsidian_writes": 0,
            "legacy_migration": legacy_migration,
        }
    )
    return result


def initialize_current_project(
    role: str,
    company_name: str,
    confirmed: bool,
) -> dict[str, Any]:
    return initialize(
        current_project(),
        role,
        company_name,
        confirmed=confirmed,
    )


def validate_company(value: Any) -> dict[str, Any]:
    try:
        return validate_company_config(
            value,
            validate_policy=validate_policy,
            validate_share_scope=validate_share_scope,
            membership_verification_schema=MEMBERSHIP_VERIFICATION_SCHEMA,
            plugin_version=PLUGIN_VERSION,
        )
    except CoreError as exc:
        raise SetupError(str(exc)) from exc


def import_company(vault: Path, source: Path) -> dict[str, Any]:
    value = validate_company(load_json(source))
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "employee":
        raise SetupError("Company configuration import is restricted to employee setup.")
    organization.update(
        {
            "company_name": value["company_name"],
            "feishu_enabled": True,
            "feishu_brand": value["feishu_brand"],
            "app_id": value["app_id"],
            "publish_policy": value["effective_employee_policy"],
            "share_scope": value["share_scope"],
            "membership_verified": False,
            "employee_access_verified": False,
            "tenant_key_hash": value["tenant_key_hash"],
        }
    )
    atomic_write_json(vault / ".kb/config/organization.json", organization)
    atomic_write_json(
        vault / ".kb/mappings/feishu_nodes.json",
        {
            "version": 2,
            "space_name": value["space_name"],
            "space_id": str(value["space_id"]),
            "nodes": value["nodes"],
        },
    )
    atomic_write_json(vault / ".kb/config/company.json", value)
    state = load_json(vault / ".kb/config/setup-state.json", {})
    state["completed_stages"] = ["vault_initialized", "company_config_imported"]
    state["pending_stage"] = "employee_membership_verification"
    atomic_write_json(vault / ".kb/config/setup-state.json", state)
    return {
        "ok": True,
        "company": value["company_name"],
        "imported": True,
        "membership_verified": False,
        "requires_employee_oauth_and_membership_verification": True,
    }


def record_membership_verification(
    vault: Path,
    share_scope_file: Path,
    readback_file: Path,
    employee_policy: str,
) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "admin":
        raise SetupError("Membership verification recording is restricted to administrators.")
    scope = load_json(share_scope_file)
    readback = load_json(readback_file)
    mapping = mapping_value(vault)
    try:
        plan = build_membership_plan(mapping["space_id"], scope, employee_policy)
        verification = verify_member_readback(plan, readback)
    except MembershipError as exc:
        raise SetupError(str(exc)) from exc
    verification.update(
        {
            "mapping_hash": canonical_hash(mapping),
            "effective_employee_policy": employee_policy,
        }
    )
    organization.update(
        {
            "share_scope": scope,
            "publish_policy": employee_policy,
            "membership_verified": True,
        }
    )
    atomic_write_json(vault / ".kb/config/organization.json", organization)
    atomic_write_json(vault / ".kb/state/membership-verification.json", verification)
    return {"ok": True, "membership_verified": True, "writes": 0}


def preview_company_membership(vault: Path, adapter: Any | None = None) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "admin":
        raise SetupError("Company membership preview is restricted to administrators.")
    mapping = mapping_value(vault)
    adapter = adapter or LarkMembershipAdapter(mapping["space_id"])
    try:
        administrator_hash = adapter.current_user_principal_hash()
        adapter.allowed_admin_principal_hashes = {administrator_hash}
        scope = resolve_share_scope("all-employees", [adapter.resolve_all_employees()])
        plan = build_membership_plan(mapping["space_id"], scope, "members")
        preview = human_preview(plan)
    except (MembershipError, CompanyAdapterError) as exc:
        raise SetupError(str(exc)) from exc
    receipt = {
        "schema": "kb-membership-preview/v1",
        "plan": plan,
        "mapping_hash": canonical_hash(mapping),
        "administrator_principal_hash": administrator_hash,
    }
    atomic_write_json(vault / ".kb/state/membership-preview.json", receipt)
    return {"ok": True, "preview": preview, "writes": 0}


def apply_company_membership(
    vault: Path,
    confirmation: str,
    adapter: Any | None = None,
) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "admin":
        raise SetupError("Company membership changes are restricted to administrators.")
    receipt = load_json(vault / ".kb/state/membership-preview.json", {})
    plan = receipt.get("plan", {}) if isinstance(receipt, dict) else {}
    mapping = mapping_value(vault)
    if receipt.get("mapping_hash") != canonical_hash(mapping):
        raise SetupError("Membership preview is stale because the space mapping changed.")
    administrator_hash = str(receipt.get("administrator_principal_hash", ""))
    adapter = adapter or LarkMembershipAdapter(
        mapping["space_id"],
        allowed_admin_principal_hashes={administrator_hash},
    )
    adapter.allowed_admin_principal_hashes = {administrator_hash}
    try:
        current_hash = adapter.current_user_principal_hash()
        current_scope = resolve_share_scope("all-employees", [adapter.resolve_all_employees()])
        current_plan = build_membership_plan(mapping["space_id"], current_scope, "members")
        if current_hash != administrator_hash or current_plan.get("plan_hash") != plan.get("plan_hash"):
            raise SetupError("Membership preview is stale and must be regenerated.")
        result = execute_membership_change(adapter, plan, confirmation)
    except (MembershipError, CompanyAdapterError) as exc:
        raise SetupError(str(exc)) from exc
    if not result["ok"]:
        return result
    verification = result["verification"]
    verification.update(
        {
            "mapping_hash": canonical_hash(mapping),
            "effective_employee_policy": "members",
        }
    )
    organization.update(
        {
            "share_scope": plan["share_scope"],
            "publish_policy": "members",
            "membership_verified": True,
            "administrator_principal_hash": administrator_hash,
        }
    )
    atomic_write_json(vault / ".kb/config/organization.json", organization)
    atomic_write_json(vault / ".kb/state/membership-verification.json", verification)
    return {
        "ok": True,
        "membership_verified": True,
        "writes": result["writes"],
        "verification": verification,
    }


def verify_employee(
    vault: Path,
    evidence_file: Path,
    *,
    perform_initial_sync: bool = False,
    sync_reader: Any | None = None,
) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "employee":
        raise SetupError("Employee access verification is restricted to employee setup.")
    company = validate_company(load_json(vault / ".kb/config/company.json"))
    evidence = load_json(evidence_file)
    try:
        result = verify_employee_access(company, evidence)
    except MembershipError as exc:
        raise SetupError(str(exc)) from exc
    organization_path = vault / ".kb/config/organization.json"
    access_path = vault / ".kb/state/employee-access.json"
    setup_path = vault / ".kb/config/setup-state.json"
    backups = {
        path: path.read_bytes() if path.is_file() else None
        for path in (organization_path, access_path, setup_path)
    }
    organization["employee_access_verified"] = True
    atomic_write_json(organization_path, organization)
    atomic_write_json(
        access_path,
        {
            "schema": "kb-employee-access-state/v1",
            "verified": True,
            "space_id": result["space_id"],
            "effective_employee_policy": result["effective_employee_policy"],
            "membership_hash": company["membership_verification"]["member_list_hash"],
        },
    )
    state = load_json(setup_path, {})
    state["completed_stages"] = [
        "vault_initialized",
        "company_config_imported",
        "employee_membership_verified",
    ]
    state.pop("pending_stage", None)
    atomic_write_json(setup_path, state)
    if perform_initial_sync:
        try:
            from company_sync_coordinator import initial_employee_sync

            result = {**result, "initial_sync": initial_employee_sync(vault, sync_reader)}
        except Exception:
            for path, data in backups.items():
                if data is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            raise
    return result


def export_company(vault: Path, destination: Path) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "admin":
        raise SetupError("Company configuration export is restricted to administrators.")
    membership = load_json(vault / ".kb/state/membership-verification.json", {})
    mapping_value_current = mapping_value(vault)
    mapping_hash = canonical_hash(mapping_value_current)
    share_scope = organization.get("share_scope", {})
    policy = organization.get("publish_policy", "members")
    if (
        organization.get("membership_verified") is not True
        or membership.get("verified") is not True
        or membership.get("mapping_hash") != mapping_hash
        or membership.get("share_scope_hash") != canonical_hash(share_scope)
        or membership.get("effective_employee_policy") != policy
    ):
        raise SetupError("Verified membership, mapping, and employee policy are required before export.")
    value = {
        "schema": COMPANY_SCHEMA,
        "minimum_plugin_version": PLUGIN_VERSION,
        "company_name": organization.get("company_name", ""),
        "feishu_brand": organization.get("feishu_brand", "feishu"),
        "app_id": organization.get("app_id", ""),
        "tenant_key_hash": organization.get("tenant_key_hash", ""),
        "space_name": mapping_value_current["space_name"],
        "space_id": mapping_value_current["space_id"],
        "nodes": mapping_value_current["nodes"],
        "space_mapping_hash": mapping_hash,
        "share_scope": share_scope,
        "effective_employee_policy": policy,
        "membership_verification": membership,
    }
    validate_company(value)
    atomic_write_json(destination, value)
    atomic_write_json(vault / ".kb/config/company.json", value)
    return {
        "ok": True,
        "company": value["company_name"],
        "exported": True,
        "file": str(destination),
    }


def install_lark_cli(confirmed: bool) -> dict[str, Any]:
    if shutil.which("lark-cli"):
        return {"ok": True, "already_installed": True}
    if not confirmed:
        raise SetupError("Feishu CLI installation requires explicit confirmation.")
    if not shutil.which("npm"):
        return {
            "ok": False,
            "requires_node": True,
            "platform": platform.system(),
        }
    completed = subprocess.run(
        ["npm", "install", "--global", "@larksuite/cli"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise SetupError("Feishu CLI installation did not complete successfully.")
    return {"ok": True, "installed": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inspect-current-project")

    initialize_parser = subparsers.add_parser("initialize-current-project")
    initialize_parser.add_argument("--role", choices=sorted(ROLES), required=True)
    initialize_parser.add_argument("--company-name", default="")
    initialize_parser.add_argument("--yes", action="store_true")

    import_parser = subparsers.add_parser("import-company")
    import_parser.add_argument("--vault", type=Path, required=True)
    import_parser.add_argument("--file", type=Path, required=True)

    export_parser = subparsers.add_parser("export-company")
    export_parser.add_argument("--vault", type=Path, required=True)
    export_parser.add_argument("--file", type=Path, required=True)

    membership_parser = subparsers.add_parser("record-membership")
    membership_parser.add_argument("--vault", type=Path, required=True)
    membership_parser.add_argument("--share-scope", type=Path, required=True)
    membership_parser.add_argument("--readback", type=Path, required=True)
    membership_parser.add_argument(
        "--employee-policy", choices=sorted(PUBLISH_POLICIES), required=True
    )

    employee_parser = subparsers.add_parser("verify-employee")
    employee_parser.add_argument("--vault", type=Path, required=True)
    employee_parser.add_argument("--evidence", type=Path, required=True)

    preview_membership_parser = subparsers.add_parser("preview-membership")
    preview_membership_parser.add_argument("--vault", type=Path, required=True)

    apply_membership_parser = subparsers.add_parser("apply-membership")
    apply_membership_parser.add_argument("--vault", type=Path, required=True)
    apply_membership_parser.add_argument("--confirmation", required=True)

    lark_parser = subparsers.add_parser("install-lark-cli")
    lark_parser.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "inspect-current-project":
            result = inspect_current_project()
        elif args.command == "initialize-current-project":
            result = initialize_current_project(
                args.role,
                args.company_name,
                args.yes,
            )
        elif args.command == "import-company":
            result = import_company(
                args.vault.expanduser().resolve(),
                args.file.expanduser().resolve(),
            )
        elif args.command == "export-company":
            result = export_company(
                args.vault.expanduser().resolve(),
                args.file.expanduser().resolve(),
            )
        elif args.command == "record-membership":
            result = record_membership_verification(
                args.vault.expanduser().resolve(),
                args.share_scope.expanduser().resolve(),
                args.readback.expanduser().resolve(),
                args.employee_policy,
            )
        elif args.command == "verify-employee":
            result = verify_employee(
                args.vault.expanduser().resolve(),
                args.evidence.expanduser().resolve(),
                perform_initial_sync=True,
            )
        elif args.command == "preview-membership":
            result = preview_company_membership(args.vault.expanduser().resolve())
        elif args.command == "apply-membership":
            result = apply_company_membership(
                args.vault.expanduser().resolve(),
                args.confirmation,
            )
        else:
            result = install_lark_cli(args.yes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except (OSError, UnicodeError, json.JSONDecodeError, CoreError, SetupError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

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
    EXACT_CONFIRMATION,
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
from feishu_company_adapter import (
    CompanyAdapterError,
    LarkMembershipAdapter,
    LarkSpaceSetupAdapter,
)


PLUGIN_VERSION = "0.7.1"
SCHEMA = "kb-setup-state/v4"
SPACE_CREATE_PREVIEW_SCHEMA = "kb-space-create-preview/v1"
SPACE_CREATE_CONFIRMATION = "确认创建知识空间"
WIKI_TEMPLATE_SCHEMA = "kb-wiki-template/v1"
WIKI_TEMPLATE_PREVIEW_SCHEMA = "kb-wiki-template-preview/v1"
WIKI_TEMPLATE_CONFIRMATION = "确认初始化知识库模板"
MEMBERSHIP_PREVIEW_SCHEMA = "kb-membership-preview/v2"
MANAGED_MEMBERS_SCHEMA = "kb-managed-members/v1"
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


def wiki_template_file() -> Path:
    return (
        plugin_root()
        / "skills/enterprise-knowledge-base/assets/wiki-template.json"
    )


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


def _space_create_receipt_path(vault: Path) -> Path:
    return vault / ".kb/state/space-create-preview.json"


def _require_admin_space_setup(
    vault: Path, *, allow_configured: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = project_binding(vault)
    if not binding["project_specific"]:
        raise SetupError("Knowledge-space setup requires the bound current Codex project.")
    organization = load_json(vault / ".kb/config/organization.json", {})
    if not isinstance(organization, dict) or organization.get("role") != "admin":
        raise SetupError("Knowledge-space creation is restricted to administrators.")
    mapping = mapping_value(vault)
    if not allow_configured and (
        mapping["space_id"] or mapping["space_name"] or mapping["nodes"]
    ):
        raise SetupError("A company knowledge space is already configured; do not replace it.")
    return organization, mapping


def _clean_space_text(value: str, field: str, *, required: bool) -> str:
    cleaned = str(value).strip()
    if required and not cleaned:
        raise SetupError(f"Knowledge-space {field} must not be blank.")
    if any(character in cleaned for character in ("\x00", "\r", "\n", "\t")):
        raise SetupError(f"Knowledge-space {field} contains unsupported control characters.")
    return cleaned


def _space_snapshot(spaces: list[dict[str, Any]]) -> str:
    return canonical_hash(
        sorted(
            (
                str(space.get("space_id", "")),
                str(space.get("name", "")),
                str(space.get("visibility", "")),
                str(space.get("open_sharing", "")),
            )
            for space in spaces
        )
    )


def preview_space_creation(
    vault: Path,
    name: str,
    description: str = "",
    adapter: Any | None = None,
) -> dict[str, Any]:
    vault = vault.resolve()
    _require_admin_space_setup(vault)
    name = _clean_space_text(name, "name", required=True)
    description = _clean_space_text(description, "description", required=False)
    existing_receipt = load_json(_space_create_receipt_path(vault), {})
    if isinstance(existing_receipt, dict) and existing_receipt.get("status") in {
        "executing",
        "outcome_unknown",
        "created_unverified",
    }:
        raise SetupError(
            "A previous knowledge-space creation is unresolved; do not replace its receipt."
        )
    adapter = adapter or LarkSpaceSetupAdapter()
    try:
        administrator_hash = adapter.current_user_principal_hash()
        spaces = adapter.list_spaces()
    except CompanyAdapterError as exc:
        raise SetupError(str(exc)) from exc
    if any(space.get("name") == name for space in spaces):
        raise SetupError("An accessible knowledge space already has this exact name.")
    receipt = {
        "schema": SPACE_CREATE_PREVIEW_SCHEMA,
        "status": "ready",
        "name": name,
        "description": description,
        "administrator_principal_hash": administrator_hash,
        "space_snapshot_hash": _space_snapshot(spaces),
        "confirmation": SPACE_CREATE_CONFIRMATION,
    }
    atomic_write_json(_space_create_receipt_path(vault), receipt)
    return {
        "ok": True,
        "status": "ready_for_confirmation",
        "preview": {
            "action": "新建飞书知识库",
            "name": name,
            "description": description,
            "space_type": "团队知识空间",
            "visibility": "私有",
            "external_sharing": "关闭",
            "identity": "当前管理员本人 OAuth",
            "member_change": "不包含；创建后单独预览和确认",
        },
        "confirmation": SPACE_CREATE_CONFIRMATION,
        "remote_writes": 0,
        "local_state_writes": 1,
    }


def _finish_space_creation(
    vault: Path,
    receipt: dict[str, Any],
    adapter: Any,
    *,
    remote_writes: int,
    recovered: bool,
) -> dict[str, Any]:
    space_id = str(receipt.get("created_space_id", ""))
    if not space_id:
        raise SetupError("The created knowledge-space ID is missing.")
    try:
        space = adapter.verify_space(space_id)
    except CompanyAdapterError as exc:
        raise SetupError(f"Created knowledge-space readback failed: {exc}") from exc
    if (
        space.get("space_id") != space_id
        or space.get("name") != receipt.get("name")
        or space.get("description", "") != receipt.get("description", "")
    ):
        raise SetupError("Created knowledge-space readback does not match the preview.")
    mapping_value_new = {
        "version": 2,
        "space_name": space["name"],
        "space_id": space_id,
        "nodes": [],
    }
    state_path = vault / ".kb/config/setup-state.json"
    state = load_json(state_path, {})
    completed = state.get("completed_stages", [])
    if not isinstance(completed, list):
        raise SetupError("The setup state has an invalid completed-stage list.")
    _, mapping = _require_admin_space_setup(vault, allow_configured=True)
    if mapping != {"space_name": "", "space_id": "", "nodes": []}:
        if mapping != {
            "space_name": mapping_value_new["space_name"],
            "space_id": mapping_value_new["space_id"],
            "nodes": mapping_value_new["nodes"],
        }:
            raise SetupError("The local company-space mapping changed before finalization.")
    else:
        atomic_write_json(vault / ".kb/mappings/feishu_nodes.json", mapping_value_new)
    if "space_verified" not in completed:
        completed.append("space_verified")
    state["completed_stages"] = completed
    state["pending_stage"] = "wiki_template_preview"
    atomic_write_json(state_path, state)
    receipt.update(
        {
            "status": "verified",
            "readback_hash": canonical_hash(space),
            "mapping_hash": canonical_hash(mapping_value_new),
        }
    )
    atomic_write_json(_space_create_receipt_path(vault), receipt)
    return {
        "ok": True,
        "status": "created_and_verified",
        "space": {
            "name": space["name"],
            "space_id": space_id,
            "visibility": space["visibility"],
            "external_sharing": space["open_sharing"],
        },
        "remote_writes": remote_writes,
        "membership_writes": 0,
        "recovered": recovered,
        "next_step": "预览固定 Wiki 结构模板",
        "next_confirmation": WIKI_TEMPLATE_CONFIRMATION,
    }


def apply_space_creation(
    vault: Path,
    confirmation: str,
    adapter: Any | None = None,
) -> dict[str, Any]:
    vault = vault.resolve()
    _require_admin_space_setup(vault, allow_configured=True)
    receipt_path = _space_create_receipt_path(vault)
    receipt = load_json(receipt_path, {})
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != SPACE_CREATE_PREVIEW_SCHEMA
    ):
        raise SetupError("A valid knowledge-space creation preview is required.")
    if confirmation != SPACE_CREATE_CONFIRMATION:
        return {
            "ok": False,
            "status": "confirmation_required",
            "expected": SPACE_CREATE_CONFIRMATION,
            "remote_writes": 0,
        }
    adapter = adapter or LarkSpaceSetupAdapter()
    try:
        principal = adapter.current_user_principal_hash()
    except CompanyAdapterError as exc:
        raise SetupError(str(exc)) from exc
    if principal != receipt.get("administrator_principal_hash"):
        raise SetupError("The administrator identity changed after preview.")
    status = str(receipt.get("status", ""))
    if status == "verified":
        mapping = mapping_value(vault)
        if (
            mapping["space_id"] == str(receipt.get("created_space_id", ""))
            and mapping["space_name"] == receipt.get("name")
        ):
            return {
                "ok": True,
                "status": "already_verified",
                "remote_writes": 0,
                "membership_writes": 0,
            }
        raise SetupError("Verified knowledge-space state conflicts with the local mapping.")
    if status == "created_unverified":
        return _finish_space_creation(
            vault, receipt, adapter, remote_writes=0, recovered=True
        )
    if status in {"executing", "outcome_unknown"}:
        raise SetupError(
            "The previous knowledge-space creation outcome is unknown; do not retry creation."
        )
    if status != "ready":
        raise SetupError("Knowledge-space creation preview is not executable.")
    try:
        spaces = adapter.list_spaces()
    except CompanyAdapterError as exc:
        raise SetupError(str(exc)) from exc
    if any(space.get("name") == receipt.get("name") for space in spaces):
        raise SetupError("The knowledge-space name became occupied after preview.")
    receipt["status"] = "executing"
    receipt["pre_create_snapshot_hash"] = _space_snapshot(spaces)
    atomic_write_json(receipt_path, receipt)
    try:
        created = adapter.create_space(
            str(receipt.get("name", "")), str(receipt.get("description", ""))
        )
    except CompanyAdapterError as exc:
        receipt["status"] = "outcome_unknown"
        receipt["error"] = "remote-create-outcome-unknown"
        atomic_write_json(receipt_path, receipt)
        raise SetupError(
            "Knowledge-space creation did not return a verifiable result; do not retry."
        ) from exc
    receipt["created_space_id"] = created.get("space_id", "")
    receipt["status"] = "created_unverified"
    atomic_write_json(receipt_path, receipt)
    if (
        created.get("name") != receipt.get("name")
        or created.get("description", "") != receipt.get("description", "")
    ):
        raise SetupError("Created knowledge-space response does not match the preview.")
    return _finish_space_creation(
        vault, receipt, adapter, remote_writes=1, recovered=False
    )


def _wiki_template_receipt_path(vault: Path) -> Path:
    return vault / ".kb/state/wiki-template-preview.json"


def _load_wiki_template() -> dict[str, Any]:
    path = wiki_template_file()
    if not path.is_file():
        raise SetupError("The packaged Wiki structure template is missing.")
    value = load_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema") != WIKI_TEMPLATE_SCHEMA
        or value.get("template_id") != "obsidian-enterprise-knowledge-base"
        or not isinstance(value.get("nodes"), list)
        or not value["nodes"]
        or len(value["nodes"]) > 50
    ):
        raise SetupError("The packaged Wiki structure template is invalid.")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    forbidden = {"space_id", "node_token", "obj_token", "content", "body"}
    for raw in value["nodes"]:
        if not isinstance(raw, dict) or forbidden.intersection(raw):
            raise SetupError("The Wiki template must not contain live IDs or content.")
        node = {
            "key": str(raw.get("key", "")).strip(),
            "title": str(raw.get("title", "")).strip(),
            "parent_key": str(raw.get("parent_key", "")).strip(),
            "obj_type": str(raw.get("obj_type", "")).strip(),
        }
        if (
            not node["key"]
            or node["key"] in seen
            or not node["title"]
            or node["obj_type"] != "docx"
            or (node["parent_key"] and node["parent_key"] not in seen)
        ):
            raise SetupError("The Wiki template topology is invalid or non-deterministic.")
        seen.add(node["key"])
        normalized.append(node)
    return {
        "schema": WIKI_TEMPLATE_SCHEMA,
        "template_id": value["template_id"],
        "nodes": normalized,
    }


def _template_tree_hash(nodes: list[dict[str, Any]]) -> str:
    return canonical_hash(
        sorted(
            (
                str(node.get("node_token", "")),
                str(node.get("obj_token", "")),
                str(node.get("obj_type", "")),
                str(node.get("title", "")),
                str(node.get("parent_node_token", "")),
            )
            for node in nodes
        )
    )


def _require_new_space_template_setup(
    vault: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    organization, mapping = _require_admin_space_setup(vault, allow_configured=True)
    space_receipt = load_json(_space_create_receipt_path(vault), {})
    if (
        not isinstance(space_receipt, dict)
        or space_receipt.get("schema") != SPACE_CREATE_PREVIEW_SCHEMA
        or space_receipt.get("status") != "verified"
        or str(space_receipt.get("created_space_id", "")) != mapping["space_id"]
        or not mapping["space_id"]
    ):
        raise SetupError(
            "The fixed Wiki template is only initialized for a newly created verified space."
        )
    return organization, mapping, space_receipt


def _validate_created_template_nodes(
    template: dict[str, Any],
    created_nodes: dict[str, dict[str, str]],
    tree: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specs = {node["key"]: node for node in template["nodes"]}
    remote = {str(node.get("node_token", "")): node for node in tree}
    if "" in remote or len(remote) != len(tree):
        raise SetupError("Wiki template readback contains invalid or duplicate node IDs.")
    known_tokens: set[str] = set()
    for key, created in created_nodes.items():
        spec = specs.get(key)
        if not spec:
            raise SetupError("Wiki template receipt contains an unknown node key.")
        token = str(created.get("node_token", ""))
        node = remote.get(token)
        parent_key = spec["parent_key"]
        expected_parent = (
            str(created_nodes.get(parent_key, {}).get("node_token", ""))
            if parent_key
            else ""
        )
        if parent_key and not expected_parent:
            raise SetupError("Wiki template receipt is missing a parent node.")
        if (
            not token
            or not node
            or str(node.get("obj_token", "")) != str(created.get("obj_token", ""))
            or str(node.get("obj_type", "")) != spec["obj_type"]
            or str(node.get("title", "")) != spec["title"]
            or str(node.get("parent_node_token", "")) != expected_parent
        ):
            raise SetupError("A created Wiki template node failed exact readback.")
        known_tokens.add(token)
    return [node for token, node in remote.items() if token not in known_tokens]


def preview_wiki_template(
    vault: Path,
    adapter: Any | None = None,
) -> dict[str, Any]:
    vault = vault.resolve()
    _, mapping, _ = _require_new_space_template_setup(vault)
    if mapping["nodes"]:
        raise SetupError("The new knowledge space already has a local node mapping.")
    receipt_path = _wiki_template_receipt_path(vault)
    existing = load_json(receipt_path, {})
    if isinstance(existing, dict) and existing.get("status") in {
        "executing",
        "outcome_unknown",
        "created_unverified",
    }:
        raise SetupError("A previous Wiki template initialization is unresolved.")
    template = _load_wiki_template()
    adapter = adapter or LarkSpaceSetupAdapter()
    try:
        administrator_hash = adapter.current_user_principal_hash()
        space = adapter.verify_space(mapping["space_id"])
        tree = adapter.list_tree(mapping["space_id"])
    except CompanyAdapterError as exc:
        raise SetupError(str(exc)) from exc
    if space.get("space_id") != mapping["space_id"]:
        raise SetupError("The Wiki template target space changed before preview.")
    if tree:
        raise SetupError("The newly created space is not empty; template initialization stopped.")
    titles = {node["key"]: node["title"] for node in template["nodes"]}
    receipt = {
        "schema": WIKI_TEMPLATE_PREVIEW_SCHEMA,
        "status": "ready",
        "space_id": mapping["space_id"],
        "administrator_principal_hash": administrator_hash,
        "template": template,
        "template_hash": canonical_hash(template),
        "pre_template_tree_hash": _template_tree_hash(tree),
        "created_nodes": {},
        "confirmation": WIKI_TEMPLATE_CONFIRMATION,
    }
    atomic_write_json(receipt_path, receipt)
    return {
        "ok": True,
        "status": "ready_for_confirmation",
        "preview": {
            "action": "初始化固定 Wiki 结构模板",
            "template": "Obsidian企业知识库结构",
            "space_id": mapping["space_id"],
            "node_count": len(template["nodes"]),
            "nodes": [
                {
                    "title": node["title"],
                    "parent": (
                        titles[node["parent_key"]]
                        if node["parent_key"]
                        else "知识空间根目录"
                    ),
                    "type": node["obj_type"],
                }
                for node in template["nodes"]
            ],
            "content_write": "不包含正文或现有业务内容",
            "member_change": "不包含；模板完成后单独预览和确认",
        },
        "confirmation": WIKI_TEMPLATE_CONFIRMATION,
        "remote_writes": 0,
        "local_state_writes": 1,
    }


def _recover_pending_template_node(
    receipt: dict[str, Any],
    tree: list[dict[str, Any]],
) -> None:
    pending = receipt.get("pending_node", {})
    template = receipt["template"]
    created_nodes = receipt.get("created_nodes", {})
    if not isinstance(pending, dict) or not isinstance(created_nodes, dict):
        raise SetupError("Wiki template recovery state is invalid.")
    unexpected = _validate_created_template_nodes(template, created_nodes, tree)
    key = str(pending.get("key", ""))
    spec = next((node for node in template["nodes"] if node["key"] == key), None)
    if not spec:
        raise SetupError("Wiki template recovery has no valid pending node.")
    parent_key = spec["parent_key"]
    parent_token = (
        str(created_nodes.get(parent_key, {}).get("node_token", ""))
        if parent_key
        else ""
    )
    candidates = [
        node
        for node in unexpected
        if str(node.get("title", "")) == spec["title"]
        and str(node.get("obj_type", "")) == spec["obj_type"]
        and str(node.get("parent_node_token", "")) == parent_token
        and str(node.get("node_token", ""))
        and str(node.get("obj_token", ""))
    ]
    if len(unexpected) != 1 or len(candidates) != 1:
        raise SetupError(
            "The previous Wiki node outcome cannot be uniquely recovered; do not retry."
        )
    node = candidates[0]
    created_nodes[key] = {
        "node_token": str(node["node_token"]),
        "obj_token": str(node["obj_token"]),
    }
    receipt["created_nodes"] = created_nodes
    receipt["status"] = "created_unverified"
    receipt.pop("pending_node", None)
    receipt["recovered_unknown_write"] = True


def _finish_wiki_template(
    vault: Path,
    receipt: dict[str, Any],
    tree: list[dict[str, Any]],
    *,
    remote_writes: int,
) -> dict[str, Any]:
    template = receipt["template"]
    created_nodes = receipt["created_nodes"]
    unexpected = _validate_created_template_nodes(template, created_nodes, tree)
    if unexpected or len(created_nodes) != len(template["nodes"]):
        raise SetupError("Wiki template readback is incomplete or contains extra nodes.")
    mapping = mapping_value(vault)
    mapped_nodes = []
    for spec in template["nodes"]:
        created = created_nodes[spec["key"]]
        parent_key = spec["parent_key"]
        mapped_nodes.append(
            {
                "node_name": spec["title"],
                "node_token": created["node_token"],
                "obj_token": created["obj_token"],
                "parent_node_token": (
                    created_nodes[parent_key]["node_token"] if parent_key else ""
                ),
                "template_key": spec["key"],
                "verified": True,
            }
        )
    mapping_new = {
        "version": 2,
        "space_name": mapping["space_name"],
        "space_id": mapping["space_id"],
        "nodes": mapped_nodes,
    }
    if mapping["nodes"] and mapping["nodes"] != mapped_nodes:
        raise SetupError("The company-space mapping changed before template finalization.")
    if not mapping["nodes"]:
        atomic_write_json(vault / ".kb/mappings/feishu_nodes.json", mapping_new)
    state_path = vault / ".kb/config/setup-state.json"
    state = load_json(state_path, {})
    completed = state.get("completed_stages", [])
    if not isinstance(completed, list):
        raise SetupError("The setup state has an invalid completed-stage list.")
    if "wiki_template_verified" not in completed:
        completed.append("wiki_template_verified")
    state["completed_stages"] = completed
    state["pending_stage"] = "membership_preview"
    atomic_write_json(state_path, state)
    receipt["status"] = "verified"
    receipt["tree_hash"] = _template_tree_hash(tree)
    receipt["mapping_hash"] = canonical_hash(mapping_new)
    atomic_write_json(_wiki_template_receipt_path(vault), receipt)
    return {
        "ok": True,
        "status": "template_created_and_verified",
        "template_id": template["template_id"],
        "node_count": len(mapped_nodes),
        "remote_writes": remote_writes,
        "content_writes": 0,
        "membership_writes": 0,
        "recovered": bool(receipt.get("recovered_unknown_write")) or remote_writes == 0,
        "next_step": "预览全公司内部员工成员方案",
        "next_confirmation": EXACT_CONFIRMATION,
    }


def apply_wiki_template(
    vault: Path,
    confirmation: str,
    adapter: Any | None = None,
) -> dict[str, Any]:
    vault = vault.resolve()
    _, mapping, _ = _require_new_space_template_setup(vault)
    receipt_path = _wiki_template_receipt_path(vault)
    receipt = load_json(receipt_path, {})
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != WIKI_TEMPLATE_PREVIEW_SCHEMA
        or receipt.get("template_hash") != canonical_hash(_load_wiki_template())
        or receipt.get("template_hash") != canonical_hash(receipt.get("template"))
        or str(receipt.get("space_id", "")) != mapping["space_id"]
    ):
        raise SetupError("A current fixed Wiki template preview is required.")
    if confirmation != WIKI_TEMPLATE_CONFIRMATION:
        return {
            "ok": False,
            "status": "confirmation_required",
            "expected": WIKI_TEMPLATE_CONFIRMATION,
            "remote_writes": 0,
        }
    adapter = adapter or LarkSpaceSetupAdapter()
    try:
        principal = adapter.current_user_principal_hash()
        space = adapter.verify_space(mapping["space_id"])
        tree = adapter.list_tree(mapping["space_id"])
    except CompanyAdapterError as exc:
        raise SetupError(str(exc)) from exc
    if (
        principal != receipt.get("administrator_principal_hash")
        or space.get("space_id") != mapping["space_id"]
    ):
        raise SetupError("The administrator identity or template target space changed.")
    status = str(receipt.get("status", ""))
    if status == "verified":
        result = _finish_wiki_template(vault, receipt, tree, remote_writes=0)
        result["status"] = "already_verified"
        return result
    if status in {"executing", "outcome_unknown"}:
        _recover_pending_template_node(receipt, tree)
        atomic_write_json(receipt_path, receipt)
        status = "created_unverified"
    if status not in {"ready", "created_unverified"}:
        raise SetupError("Wiki template preview is not executable.")
    created_nodes = receipt.get("created_nodes", {})
    if not isinstance(created_nodes, dict):
        raise SetupError("Wiki template creation journal is invalid.")
    unexpected = _validate_created_template_nodes(receipt["template"], created_nodes, tree)
    if unexpected:
        raise SetupError("The target Wiki tree changed after template preview.")
    if not created_nodes and receipt.get("pre_template_tree_hash") != _template_tree_hash(tree):
        raise SetupError("The target Wiki tree changed after template preview.")
    remote_writes = 0
    for spec in receipt["template"]["nodes"]:
        key = spec["key"]
        if key in created_nodes:
            continue
        parent_key = spec["parent_key"]
        parent_token = (
            str(created_nodes.get(parent_key, {}).get("node_token", ""))
            if parent_key
            else ""
        )
        if parent_key and not parent_token:
            raise SetupError("A Wiki template parent node is not verified.")
        receipt["status"] = "executing"
        receipt["pending_node"] = {"key": key, "before_tree_hash": _template_tree_hash(tree)}
        atomic_write_json(receipt_path, receipt)
        try:
            created = adapter.create_node(
                mapping["space_id"], spec["title"], parent_token, spec["obj_type"]
            )
        except CompanyAdapterError as exc:
            receipt["status"] = "outcome_unknown"
            receipt["error"] = "remote-node-create-outcome-unknown"
            atomic_write_json(receipt_path, receipt)
            raise SetupError(
                "A Wiki template node creation outcome is unknown; do not retry blindly."
            ) from exc
        created_nodes[key] = {
            "node_token": created["node_token"],
            "obj_token": created["obj_token"],
        }
        receipt["created_nodes"] = created_nodes
        receipt["status"] = "created_unverified"
        receipt.pop("pending_node", None)
        atomic_write_json(receipt_path, receipt)
        tree.append(
            {
                **created,
                "space_id": mapping["space_id"],
                "has_child": False,
            }
        )
        remote_writes += 1
    try:
        tree = adapter.list_tree(mapping["space_id"])
    except CompanyAdapterError as exc:
        raise SetupError(f"Created Wiki template readback failed: {exc}") from exc
    return _finish_wiki_template(
        vault, receipt, tree, remote_writes=remote_writes
    )


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
    atomic_write_json(
        vault / ".kb/state/managed-members.json",
        {
            "schema": MANAGED_MEMBERS_SCHEMA,
            "space_id": mapping["space_id"],
            "strategy": plan["share_scope"]["strategy"],
            "members": plan["desired_members"],
            "roster_hash": plan["share_scope"]["roster_hash"],
        },
    )
    return {"ok": True, "membership_verified": True, "writes": 0}


def _managed_members(vault: Path, space_id: str) -> list[dict[str, Any]]:
    value = load_json(vault / ".kb/state/managed-members.json", {})
    if not value:
        return []
    if (
        not isinstance(value, dict)
        or value.get("schema") != MANAGED_MEMBERS_SCHEMA
        or str(value.get("space_id", "")) != str(space_id)
        or not isinstance(value.get("members"), list)
    ):
        raise SetupError("Managed-member state is invalid or belongs to another space.")
    return value["members"]


def preview_company_membership(vault: Path, adapter: Any | None = None) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "admin":
        raise SetupError("Company membership preview is restricted to administrators.")
    mapping = mapping_value(vault)
    space_receipt = load_json(_space_create_receipt_path(vault), {})
    state = load_json(vault / ".kb/config/setup-state.json", {})
    if (
        isinstance(space_receipt, dict)
        and space_receipt.get("schema") == SPACE_CREATE_PREVIEW_SCHEMA
        and space_receipt.get("status") == "verified"
        and str(space_receipt.get("created_space_id", "")) == mapping["space_id"]
        and "wiki_template_verified" not in state.get("completed_stages", [])
    ):
        raise SetupError(
            "Initialize and verify the fixed Wiki template before membership preview."
        )
    receipt_path = vault / ".kb/state/membership-preview.json"
    existing = load_json(receipt_path, {})
    if isinstance(existing, dict) and existing.get("status") in {"executing", "outcome_unknown"}:
        raise SetupError("A previous membership update is unresolved; do not replace its preview.")
    adapter = adapter or LarkMembershipAdapter(mapping["space_id"])
    try:
        administrator_hash = adapter.current_user_principal_hash()
        adapter.allowed_admin_principal_hashes = {administrator_hash}
        plan = adapter.prepare_membership_plan(_managed_members(vault, mapping["space_id"]))
        preview = human_preview(plan)
    except (MembershipError, CompanyAdapterError) as exc:
        raise SetupError(str(exc)) from exc
    receipt = {
        "schema": MEMBERSHIP_PREVIEW_SCHEMA,
        "status": "ready",
        "plan": plan,
        "mapping_hash": canonical_hash(mapping),
        "administrator_principal_hash": administrator_hash,
        "confirmation": EXACT_CONFIRMATION,
    }
    atomic_write_json(receipt_path, receipt)
    return {"ok": True, "status": "ready_for_confirmation", "preview": preview, "writes": 0}


def apply_company_membership(
    vault: Path,
    confirmation: str,
    adapter: Any | None = None,
) -> dict[str, Any]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("role") != "admin":
        raise SetupError("Company membership changes are restricted to administrators.")
    receipt = load_json(vault / ".kb/state/membership-preview.json", {})
    if not isinstance(receipt, dict) or receipt.get("schema") != MEMBERSHIP_PREVIEW_SCHEMA:
        raise SetupError("A valid company-membership preview is required.")
    plan = receipt.get("plan", {}) if isinstance(receipt, dict) else {}
    mapping = mapping_value(vault)
    if receipt.get("mapping_hash") != canonical_hash(mapping):
        raise SetupError("Membership preview is stale because the space mapping changed.")
    if confirmation != EXACT_CONFIRMATION:
        return {
            "ok": False,
            "authorized": False,
            "writes": 0,
            "reason": "explicit-confirmation-required",
        }
    administrator_hash = str(receipt.get("administrator_principal_hash", ""))
    adapter = adapter or LarkMembershipAdapter(
        mapping["space_id"],
        allowed_admin_principal_hashes={administrator_hash},
    )
    adapter.allowed_admin_principal_hashes = {administrator_hash}
    write_started = False
    try:
        current_hash = adapter.current_user_principal_hash()
        current_plan = adapter.prepare_membership_plan(plan.get("previously_managed", []))
        if (
            current_hash != administrator_hash
            or current_plan.get("share_scope_hash") != plan.get("share_scope_hash")
            or canonical_hash(current_plan.get("desired_members", []))
            != canonical_hash(plan.get("desired_members", []))
        ):
            raise SetupError("Membership preview is stale and must be regenerated.")
        if receipt.get("status") == "verified":
            return {"ok": True, "membership_verified": True, "writes": 0, "status": "already_verified"}
        if receipt.get("status") not in {"ready", "outcome_unknown"}:
            raise SetupError("Membership preview is not executable.")
        receipt["status"] = "executing"
        atomic_write_json(vault / ".kb/state/membership-preview.json", receipt)
        write_started = True
        result = execute_membership_change(adapter, plan, confirmation)
    except (MembershipError, CompanyAdapterError) as exc:
        if write_started:
            receipt["status"] = "outcome_unknown"
            receipt["execution"] = getattr(adapter, "last_execution", None)
            atomic_write_json(vault / ".kb/state/membership-preview.json", receipt)
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
    atomic_write_json(
        vault / ".kb/state/managed-members.json",
        {
            "schema": MANAGED_MEMBERS_SCHEMA,
            "space_id": mapping["space_id"],
            "strategy": plan["share_scope"]["strategy"],
            "members": plan["desired_members"],
            "roster_hash": plan["share_scope"]["roster_hash"],
        },
    )
    receipt["status"] = "verified"
    receipt["execution"] = result.get("execution")
    receipt["verification_hash"] = canonical_hash(verification)
    atomic_write_json(vault / ".kb/state/membership-preview.json", receipt)
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

    preview_space_parser = subparsers.add_parser("preview-space-create")
    preview_space_parser.add_argument("--vault", type=Path, required=True)
    preview_space_parser.add_argument("--name", required=True)
    preview_space_parser.add_argument("--description", default="")

    apply_space_parser = subparsers.add_parser("apply-space-create")
    apply_space_parser.add_argument("--vault", type=Path, required=True)
    apply_space_parser.add_argument("--confirmation", required=True)

    preview_template_parser = subparsers.add_parser("preview-wiki-template")
    preview_template_parser.add_argument("--vault", type=Path, required=True)

    apply_template_parser = subparsers.add_parser("apply-wiki-template")
    apply_template_parser.add_argument("--vault", type=Path, required=True)
    apply_template_parser.add_argument("--confirmation", required=True)

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
        elif args.command == "preview-space-create":
            result = preview_space_creation(
                args.vault.expanduser().resolve(),
                args.name,
                args.description,
            )
        elif args.command == "apply-space-create":
            result = apply_space_creation(
                args.vault.expanduser().resolve(),
                args.confirmation,
            )
        elif args.command == "preview-wiki-template":
            result = preview_wiki_template(args.vault.expanduser().resolve())
        elif args.command == "apply-wiki-template":
            result = apply_wiki_template(
                args.vault.expanduser().resolve(),
                args.confirmation,
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

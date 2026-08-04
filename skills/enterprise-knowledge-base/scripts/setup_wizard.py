#!/usr/bin/env python3
"""Deterministic local setup operations for the enterprise knowledge plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
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


PLUGIN_VERSION = "0.4.1"
SCHEMA = "kb-setup-state/v3"
OBSIDIAN_INTEGRATION_SCHEMA = "kb-obsidian-integration/v1"
PROJECT_SKILL_NAME = "enterprise-knowledge-base"
DEFAULT_VAULT_NAME = "知识库"
LEGACY_SKILL_NAMES = ("manage-knowledge-base", "collect-local-knowledge", "publish-enterprise-knowledge")
CLAUDIAN_PLUGIN_ID = "realclaudian"
CLAUDIAN_VERSION = "2.0.44"
CLAUDIAN_MINIMUM_CODEX_VERSION = "2.0.0"
CLAUDIAN_RELEASE_BASE = (
    "https://github.com/YishenTu/claudian/releases/download/2.0.44"
)
CLAUDIAN_ASSETS = {
    "main.js": "80a2dbb8923f3ddb5135303d60e9ad4b16f1ff03e38b77b4f53b11b0daa6e300",
    "manifest.json": "225f6e6a27954277c5a84c3b278149b1417d9fd06655604ef1027679c85c13e3",
    "styles.css": "c1e61ae89370e5f7601c9e2b2c589c3819ec8066acaa23d46fa5e473a7198bf2",
}
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
    ".obsidian",
    ".claudian",
)
ROLES = {"admin", "employee", "local"}
PUBLISH_POLICIES = {"members"}
class SetupError(RuntimeError):
    pass


def project_binding(vault: Path) -> dict[str, Any]:
    value = load_json(vault / ".kb/config/project-binding.json", {})
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
    return {"valid": valid, "project_specific": project_specific, "value": value, "resolved_root": resolved}


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


def documents_directory() -> Path:
    """Resolve the user's OS Documents folder without adding configuration."""
    if platform.system() == "Windows":
        try:
            import winreg

            key_name = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                value = str(winreg.QueryValueEx(key, "Personal")[0]).strip()
            if value:
                return Path(os.path.expandvars(value)).expanduser().resolve()
        except (ImportError, OSError, TypeError, ValueError):
            pass
    return (Path.home() / "Documents").resolve()


def default_vault_path() -> Path:
    return documents_directory() / DEFAULT_VAULT_NAME


def current_project_matches(vault: Path, current: Path | None = None) -> bool:
    target = vault.expanduser().resolve()
    current = (current or Path.cwd()).expanduser().resolve()
    return current == target or target in current.parents


def install_plan(vault: Path | None = None) -> dict[str, Any]:
    """Return a read-only, human-safe plan for Codex-led local setup."""
    target = (vault or default_vault_path()).expanduser().resolve()
    is_directory = target.is_dir()
    entries = list(target.iterdir()) if is_directory else []
    binding = project_binding(target)
    configured = bool(
        (target / "AGENTS.md").is_file()
        and (target / ".kb").is_dir()
        and binding["valid"]
    )
    legacy = bool(
        (target / "AGENTS.md").is_file()
        and (target / ".kb").is_dir()
        and all((target / ".agents/skills" / name / "SKILL.md").is_file()
                for name in LEGACY_SKILL_NAMES)
    )
    conflict = bool((target.exists() and not is_directory)
                    or (entries and not configured and not legacy))
    active = current_project_matches(target)
    return {
        "ok": not conflict,
        "mode": "codex-led-local-setup",
        "vault": str(target),
        "codex_project": str(target),
        "codex_project_name": target.name,
        "default_path": vault is None,
        "path_status": ("legacy" if legacy else "configured" if configured
                        else "conflict" if conflict else "create"),
        "legacy_skills": list(LEGACY_SKILL_NAMES) if legacy else [],
        "project_binding": binding,
        "current_codex_project_matches": active,
        "requires_codex_project_open": not active,
        "tools": tool_status(),
        "confirmation": f"安装或复用 Obsidian，并将 {target} 配置为同根的 Codex 项目和 Obsidian Vault",
        "blocked_reason": (
            "The default knowledge directory is non-empty and is not a configured Vault."
            if conflict
            else ""
        ),
    }


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
    system = platform.system()
    return {
        "platform": system,
        "python": {
            "ok": sys.version_info >= (3, 10),
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "winget": bool(shutil.which("winget")),
        "homebrew": bool(shutil.which("brew")),
        "obsidian": obsidian_installed(system),
        "node": bool(shutil.which("node")),
        "npm": bool(shutil.which("npm")),
        "lark_cli": bool(shutil.which("lark-cli")),
    }


def obsidian_installed(system: str | None = None) -> bool:
    system = system or platform.system()
    if shutil.which("obsidian") or shutil.which("Obsidian"):
        return True
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return bool(local) and (
            Path(local) / "Programs/Obsidian/Obsidian.exe"
        ).is_file()
    if system == "Darwin":
        return any(
            candidate.is_dir()
            for candidate in (
                Path("/Applications/Obsidian.app"),
                Path.home() / "Applications/Obsidian.app",
            )
        )
    return False


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
        "claudian": inspect_claudian(vault),
        "tools": tool_status(),
        "role_options": [
            "我是飞书管理员，首次为公司部署",
            "我是普通员工，加入公司已有知识库",
            "暂时只使用本地知识库",
        ],
    }


def refuse_unrelated_nonempty(vault: Path) -> None:
    if not vault.exists():
        return
    entries = list(vault.iterdir())
    if entries and not ((vault / "AGENTS.md").is_file() and (vault / ".kb").is_dir()):
        raise SetupError(
            "The selected directory is non-empty and is not a configured knowledge Vault."
        )


def initialize(vault: Path, role: str, company_name: str) -> dict[str, Any]:
    if role not in ROLES:
        raise SetupError("Unsupported setup role.")
    refuse_unrelated_nonempty(vault)
    vault.mkdir(parents=True, exist_ok=True)
    for relative in (*VISIBLE_DIRECTORIES, *SYSTEM_DIRECTORIES):
        (vault / relative).mkdir(parents=True, exist_ok=True)

    agents = vault / "AGENTS.md"
    template = template_agents()
    if not agents.exists():
        if not template.is_file():
            raise SetupError("The packaged Vault rules template is missing.")
        shutil.copyfile(template, agents)

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
    return inspect(vault)


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


def install_obsidian(confirmed: bool) -> dict[str, Any]:
    system = platform.system()
    if obsidian_installed(system):
        return {"ok": True, "already_installed": True, "platform": system}
    if not confirmed:
        raise SetupError("Obsidian installation requires explicit confirmation.")
    if system == "Windows" and shutil.which("winget"):
        command = [
            "winget",
            "install",
            "--id",
            "Obsidian.Obsidian",
            "--exact",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--disable-interactivity",
            "--silent",
        ]
    elif system == "Darwin" and shutil.which("brew"):
        command = ["brew", "install", "--cask", "obsidian"]
    else:
        return {
            "ok": False,
            "requires_browser": True,
            "url": "https://obsidian.md/download.html",
            "platform": system,
        }
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise SetupError("Obsidian installation did not complete successfully.")
    return {"ok": True, "installed": True, "platform": system}


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def find_codex_cli(explicit: str = "") -> Path:
    candidates: list[str] = []
    if explicit.strip():
        candidates.append(explicit.strip())
    if platform.system() == "Windows":
        candidates.extend(
            value for value in (shutil.which("codex.exe"), shutil.which("codex")) if value
        )
    else:
        detected = shutil.which("codex")
        if detected:
            candidates.append(detected)
        if platform.system() == "Darwin":
            candidates.extend(
                str(path)
                for path in (
                    Path("/Applications/Codex.app/Contents/Resources/codex"),
                    Path.home() / "Applications/Codex.app/Contents/Resources/codex",
                )
            )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise SetupError(
        "A local Codex CLI executable is required before Claudian can be configured."
    )


def download_claudian_asset(name: str) -> bytes:
    expected = CLAUDIAN_ASSETS.get(name)
    if not expected:
        raise SetupError("Unsupported Claudian release asset.")
    url = f"{CLAUDIAN_RELEASE_BASE}/{name}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "enterprise-knowledge-base-setup"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise SetupError("Claudian release asset exceeds the allowed size.")
    if hashlib.sha256(data).hexdigest() != expected:
        raise SetupError(f"Claudian release asset hash mismatch: {name}")
    return data


def read_claudian_manifest(plugin_dir: Path) -> dict[str, Any]:
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    value = load_json(manifest_path, {})
    if not isinstance(value, dict):
        raise SetupError("The existing Claudian manifest is invalid.")
    return value


def locate_claudian_plugin(vault: Path) -> tuple[Path, dict[str, Any]]:
    plugins_root = vault / ".obsidian/plugins"
    matches: list[tuple[Path, dict[str, Any]]] = []
    if plugins_root.is_dir():
        for candidate in sorted(plugins_root.iterdir()):
            if not candidate.is_dir():
                continue
            manifest = read_claudian_manifest(candidate)
            if manifest.get("id") == CLAUDIAN_PLUGIN_ID:
                matches.append((candidate, manifest))
    if len(matches) > 1:
        raise SetupError("Multiple Claudian installations were found in this Vault.")
    if matches:
        return matches[0]
    preferred = plugins_root / CLAUDIAN_PLUGIN_ID
    if preferred.exists() and any(preferred.iterdir()):
        raise SetupError("The Claudian plugin directory contains a different plugin.")
    return preferred, {}


def inspect_claudian(vault: Path) -> dict[str, Any]:
    """Read the existing Obsidian integration without modifying it."""
    vault = vault.expanduser().resolve()
    try:
        plugin_dir, manifest = locate_claudian_plugin(vault)
        required = tuple(CLAUDIAN_ASSETS)
        installed = bool(
            manifest.get("id") == CLAUDIAN_PLUGIN_ID
            and version_tuple(str(manifest.get("version", "")))
            >= version_tuple(CLAUDIAN_MINIMUM_CODEX_VERSION)
            and all((plugin_dir / name).is_file() for name in required)
        )
        enabled_plugins = load_json(vault / ".obsidian/community-plugins.json", [])
        enabled = bool(
            isinstance(enabled_plugins, list)
            and CLAUDIAN_PLUGIN_ID in enabled_plugins
        )
        settings = load_json(vault / ".claudian/claudian-settings.json", {})
        codex = (
            settings.get("providerConfigs", {}).get("codex", {})
            if isinstance(settings, dict)
            and isinstance(settings.get("providerConfigs", {}), dict)
            else {}
        )
        cli_paths = codex.get("cliPathsByHost", {}) if isinstance(codex, dict) else {}
        configured_paths = [
            Path(value).expanduser()
            for value in cli_paths.values()
            if isinstance(value, str) and value.strip()
        ] if isinstance(cli_paths, dict) else []
        configured_cli = next(
            (
                str(path.resolve())
                for path in configured_paths
                if path.resolve().is_file()
            ),
            "",
        )
        codex_enabled = bool(isinstance(codex, dict) and codex.get("enabled"))
        return {
            "ready": installed and enabled and codex_enabled and bool(configured_cli),
            "installed": installed,
            "enabled": enabled,
            "codex_provider_enabled": codex_enabled,
            "codex_cli_path": configured_cli,
            "version": str(manifest.get("version", "")),
            "plugin_path": (
                plugin_dir.relative_to(vault).as_posix()
                if plugin_dir.is_relative_to(vault)
                else ""
            ),
        }
    except (OSError, ValueError, TypeError, SetupError, json.JSONDecodeError) as exc:
        return {
            "ready": False,
            "installed": False,
            "enabled": False,
            "codex_provider_enabled": False,
            "codex_cli_path": "",
            "version": "",
            "plugin_path": "",
            "error": str(exc),
        }


def configure_claudian_settings(vault: Path, codex_path: Path) -> tuple[Path, bool]:
    settings_path = vault / ".claudian/claudian-settings.json"
    settings = load_json(settings_path, {})
    if not isinstance(settings, dict):
        raise SetupError("The existing Claudian settings file is invalid.")
    before = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    provider_configs = settings.get("providerConfigs", {})
    if not isinstance(provider_configs, dict):
        provider_configs = {}
    codex = provider_configs.get("codex", {})
    if not isinstance(codex, dict):
        codex = {}
    host_key = platform.node().strip() or "local-device"
    cli_paths = codex.get("cliPathsByHost", {})
    if not isinstance(cli_paths, dict):
        cli_paths = {}
    cli_paths[host_key] = str(codex_path)
    codex.update(
        {
            "enabled": True,
            "safeMode": "workspace-write",
            "cliPathsByHost": cli_paths,
        }
    )
    if platform.system() == "Windows":
        methods = codex.get("installationMethodsByHost", {})
        if not isinstance(methods, dict):
            methods = {}
        methods[host_key] = "native-windows"
        codex["installationMethodsByHost"] = methods
    provider_configs["codex"] = codex
    settings["providerConfigs"] = provider_configs
    settings["settingsProvider"] = "codex"
    changed = before != json.dumps(settings, ensure_ascii=False, sort_keys=True)
    if changed:
        atomic_write_json(settings_path, settings)
    return settings_path, changed


def install_claudian(vault: Path, confirmed: bool, codex_path: str = "") -> dict[str, Any]:
    if not confirmed:
        raise SetupError("Claudian installation requires explicit confirmation.")
    vault = vault.expanduser().resolve()
    binding = project_binding(vault)
    if not binding["valid"]:
        raise SetupError("Bind the current Codex project before installing Claudian.")
    resolved_codex = find_codex_cli(codex_path)
    plugin_dir, existing_manifest = locate_claudian_plugin(vault)

    required = tuple(CLAUDIAN_ASSETS)
    existing_version = str(existing_manifest.get("version", ""))
    reusable = bool(
        existing_manifest.get("id") == CLAUDIAN_PLUGIN_ID
        and version_tuple(existing_version)
        >= version_tuple(CLAUDIAN_MINIMUM_CODEX_VERSION)
        and all((plugin_dir / name).is_file() for name in required)
    )
    if not reusable:
        downloaded = {name: download_claudian_asset(name) for name in required}
        manifest = json.loads(downloaded["manifest.json"].decode("utf-8"))
        if (
            manifest.get("id") != CLAUDIAN_PLUGIN_ID
            or manifest.get("version") != CLAUDIAN_VERSION
        ):
            raise SetupError("The downloaded Claudian manifest does not match the pinned release.")
        for name, data in downloaded.items():
            atomic_write_bytes(plugin_dir / name, data)
        if any(
            sha256_file(plugin_dir / name) != CLAUDIAN_ASSETS[name]
            for name in required
        ):
            raise SetupError("Claudian installation read-back hash mismatch.")
        existing_version = CLAUDIAN_VERSION

    community_path = vault / ".obsidian/community-plugins.json"
    enabled = load_json(community_path, [])
    if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
        raise SetupError("Obsidian community plugin configuration is invalid.")
    if CLAUDIAN_PLUGIN_ID not in enabled:
        enabled.append(CLAUDIAN_PLUGIN_ID)
        atomic_write_json(community_path, enabled)
    settings_path, settings_changed = configure_claudian_settings(vault, resolved_codex)
    integration = {
        "schema": OBSIDIAN_INTEGRATION_SCHEMA,
        "project_root": canonical_path(vault),
        "vault_root": canonical_path(vault),
        "claudian": {
            "plugin_id": CLAUDIAN_PLUGIN_ID,
            "version": existing_version,
            "plugin_path": plugin_dir.relative_to(vault).as_posix(),
            "enabled": True,
        },
        "codex_cli_path": str(resolved_codex),
        "claudian_settings_path": str(settings_path.relative_to(vault).as_posix()),
    }
    atomic_write_json(vault / ".kb/config/obsidian-integration.json", integration)
    return {
        "ok": True,
        "installed": not reusable,
        "already_installed": reusable,
        "plugin_id": CLAUDIAN_PLUGIN_ID,
        "version": existing_version,
        "codex_cli_path": str(resolved_codex),
        "enabled": True,
        "settings_changed": settings_changed,
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


def open_obsidian(vault: Path) -> dict[str, Any]:
    if not project_binding(vault)["valid"]:
        raise SetupError("The current Codex project is not bound to this Vault.")
    uri = "obsidian://open?path=" + urllib.parse.quote(str(vault.resolve()), safe="")
    opened = webbrowser.open(uri)
    return {"ok": bool(opened), "uri_opened": bool(opened)}


def bootstrap_local(
    vault: Path | None,
    role: str,
    company_name: str,
    confirmed: bool,
    codex_path: str = "",
) -> dict[str, Any]:
    """Complete all automatable local setup after one explicit confirmation."""
    if not confirmed:
        raise SetupError("Automatic local setup requires explicit confirmation.")
    plan = install_plan(vault)
    if not plan["ok"]:
        raise SetupError(str(plan["blocked_reason"]))
    target = Path(plan["vault"])

    obsidian = install_obsidian(True)
    if not obsidian.get("ok"):
        return {
            **plan,
            "ok": False,
            "stage": "obsidian-installation-required",
            "obsidian": obsidian,
            "writes": 0,
        }

    if plan["path_status"] == "configured":
        bind_project(target, target)
    else:
        initialize(target, role, company_name)

    claudian = install_claudian(target, True, codex_path)
    obsidian_open = open_obsidian(target)
    legacy_migration = (
        retire_legacy_skills(target)
        if plan["path_status"] == "legacy"
        else {"migrated": False, "backup": "", "skills": []}
    )
    final = inspect(target)
    active = current_project_matches(target)
    complete = bool(
        final["project_binding"]["project_specific"]
        and final["claudian"]["ready"]
        and obsidian_open["ok"]
    )
    stage = ("obsidian-open-required" if not obsidian_open["ok"]
             else "open-codex-project" if not active else "local-setup-complete")
    return {
        "ok": complete,
        "mode": "codex-led-local-setup",
        "stage": stage,
        "vault": str(target),
        "codex_project": str(target),
        "codex_project_name": target.name,
        "project_specific_skill": final["project_binding"]["project_specific"],
        "obsidian": obsidian,
        "obsidian_open": obsidian_open,
        "claudian": claudian,
        "legacy_migration": legacy_migration,
        "requires_codex_project_open": not active,
        "next_user_action": (
            f"在 Codex 中打开文件夹：{target}"
            if obsidian_open["ok"] and not active
            else ""
        ),
        "role_options": final["role_options"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--vault", type=Path, required=True)

    plan_parser = subparsers.add_parser("plan-install")
    plan_parser.add_argument("--vault", type=Path)

    bootstrap_parser = subparsers.add_parser("bootstrap-local")
    bootstrap_parser.add_argument("--vault", type=Path)
    bootstrap_parser.add_argument("--role", choices=sorted(ROLES), default="local")
    bootstrap_parser.add_argument("--company-name", default="")
    bootstrap_parser.add_argument("--codex-path", default="")
    bootstrap_parser.add_argument("--yes", action="store_true")

    initialize_parser = subparsers.add_parser("initialize")
    initialize_parser.add_argument("--vault", type=Path, required=True)
    initialize_parser.add_argument("--role", choices=sorted(ROLES), required=True)
    initialize_parser.add_argument("--company-name", default="")

    bind_parser = subparsers.add_parser("bind-project")
    bind_parser.add_argument("--vault", type=Path, required=True)
    bind_parser.add_argument("--project", type=Path, required=True)

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

    install_parser = subparsers.add_parser("install-obsidian")
    install_parser.add_argument("--yes", action="store_true")

    claudian_parser = subparsers.add_parser("install-claudian")
    claudian_parser.add_argument("--vault", type=Path, required=True)
    claudian_parser.add_argument("--codex-path", default="")
    claudian_parser.add_argument("--yes", action="store_true")

    lark_parser = subparsers.add_parser("install-lark-cli")
    lark_parser.add_argument("--yes", action="store_true")

    open_parser = subparsers.add_parser("open-obsidian")
    open_parser.add_argument("--vault", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect(args.vault.expanduser().resolve())
        elif args.command == "plan-install":
            result = install_plan(
                args.vault.expanduser().resolve() if args.vault else None
            )
        elif args.command == "bootstrap-local":
            result = bootstrap_local(
                args.vault.expanduser().resolve() if args.vault else None,
                args.role,
                args.company_name,
                args.yes,
                args.codex_path,
            )
        elif args.command == "initialize":
            result = initialize(
                args.vault.expanduser().resolve(),
                args.role,
                args.company_name,
            )
        elif args.command == "bind-project":
            result = bind_project(
                args.vault.expanduser().resolve(),
                args.project.expanduser().resolve(),
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
        elif args.command == "install-obsidian":
            result = install_obsidian(args.yes)
        elif args.command == "install-claudian":
            result = install_claudian(
                args.vault.expanduser().resolve(),
                args.yes,
                args.codex_path,
            )
        elif args.command == "install-lark-cli":
            result = install_lark_cli(args.yes)
        else:
            result = open_obsidian(args.vault.expanduser().resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except (OSError, UnicodeError, json.JSONDecodeError, SetupError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

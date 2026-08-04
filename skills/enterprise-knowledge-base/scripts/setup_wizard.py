#!/usr/bin/env python3
"""Deterministic local setup operations for the enterprise knowledge plugin."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

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


PLUGIN_VERSION = "0.3.0"
SCHEMA = "kb-setup-state/v3"
ORG_SCHEMA = "kb-organization/v3"
COMPANY_SCHEMA = "kb-company-config/v3"
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
)
ROLES = {"admin", "employee", "local"}
PUBLISH_POLICIES = {"members"}
SECRET_KEYS = {
    "app_secret",
    "access_token",
    "refresh_token",
    "tenant_access_token",
    "user_access_token",
    "cookie",
    "password",
}


class SetupError(RuntimeError):
    pass


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent
    ) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, path)


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
    atomic_write_json(vault / ".kb/config/organization.json", organization)
    atomic_write_json(
        vault / ".kb/config/setup-state.json",
        {
            "schema": SCHEMA,
            "role": role,
            "completed_stages": ["vault_initialized"],
        },
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


def reject_secrets(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, candidate in value.items():
            lowered = str(key).casefold()
            if lowered in SECRET_KEYS or "secret" in lowered:
                raise SetupError(f"Company configuration contains a forbidden secret: {path}.{key}")
            reject_secrets(candidate, f"{path}.{key}")
    elif isinstance(value, list):
        for index, candidate in enumerate(value):
            reject_secrets(candidate, f"{path}[{index}]")


def validate_company(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != COMPANY_SCHEMA:
        raise SetupError("Unsupported company configuration schema.")
    reject_secrets(value)
    allowed = {
        "schema",
        "minimum_plugin_version",
        "company_name",
        "feishu_brand",
        "app_id",
        "tenant_key_hash",
        "space_name",
        "space_id",
        "nodes",
        "space_mapping_hash",
        "share_scope",
        "effective_employee_policy",
        "membership_verification",
    }
    if set(value) - allowed:
        raise SetupError("Company configuration contains unsupported or excessive data.")
    required = (
        "company_name",
        "feishu_brand",
        "app_id",
        "tenant_key_hash",
        "space_name",
        "space_id",
        "minimum_plugin_version",
    )
    if any(not str(value.get(key, "")).strip() for key in required):
        raise SetupError("Company configuration is incomplete.")
    if value["feishu_brand"] not in {"feishu", "lark"}:
        raise SetupError("Unsupported Feishu brand.")
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", str(value["app_id"])):
        raise SetupError("Invalid application ID.")
    if not re.fullmatch(r"[0-9]+", str(value["space_id"])):
        raise SetupError("Invalid knowledge-space ID.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value["tenant_key_hash"])):
        raise SetupError("Invalid tenant identity hash.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(value["minimum_plugin_version"])):
        raise SetupError("Invalid minimum plugin version.")
    if tuple(int(part) for part in str(value["minimum_plugin_version"]).split(".")) > tuple(
        int(part) for part in PLUGIN_VERSION.split(".")
    ):
        raise SetupError("This company configuration requires a newer plugin version.")
    policy = str(value.get("effective_employee_policy", ""))
    try:
        validate_policy(policy)
        share_scope = validate_share_scope(value.get("share_scope"))
    except MembershipError as exc:
        raise SetupError(str(exc)) from exc
    if policy != "members" or share_scope.get("type") != "all-employees":
        raise SetupError(
            "All internal employees must have query, collect, and publish capability."
        )
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict)
        or not str(node.get("node_name", "")).strip()
        or not str(node.get("node_token", "")).strip()
        or node.get("verified") is not True
        for node in nodes
    ):
        raise SetupError("Company node mapping is invalid or unverified.")
    mapping_hash = canonical_hash(
        {
            "space_name": value["space_name"],
            "space_id": str(value["space_id"]),
            "nodes": nodes,
        }
    )
    if value.get("space_mapping_hash") != mapping_hash:
        raise SetupError("Company space mapping hash is invalid.")
    verification = value.get("membership_verification", {})
    if (
        not isinstance(verification, dict)
        or verification.get("schema") != MEMBERSHIP_VERIFICATION_SCHEMA
        or verification.get("verified") is not True
        or str(verification.get("space_id", "")) != str(value["space_id"])
        or verification.get("share_scope_hash")
        != canonical_hash(value["share_scope"])
        or verification.get("mapping_hash") != mapping_hash
        or verification.get("effective_employee_policy") != policy
        or not str(verification.get("remote_version", "")).strip()
        or not re.fullmatch(r"[a-f0-9]{64}", str(verification.get("member_list_hash", "")))
        or verification.get("external_members") != 0
        or verification.get("employee_admin_members") != 0
    ):
        raise SetupError("Company membership verification is incomplete or stale.")
    return value


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


def mapping_value(vault: Path) -> dict[str, Any]:
    mapping = load_json(vault / ".kb/mappings/feishu_nodes.json", {})
    return {
        "space_name": mapping.get("space_name", ""),
        "space_id": str(mapping.get("space_id", "")),
        "nodes": mapping.get("nodes", []),
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
    uri = "obsidian://open?path=" + urllib.parse.quote(str(vault.resolve()), safe="")
    opened = webbrowser.open(uri)
    return {"ok": bool(opened), "uri_opened": bool(opened)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--vault", type=Path, required=True)

    initialize_parser = subparsers.add_parser("initialize")
    initialize_parser.add_argument("--vault", type=Path, required=True)
    initialize_parser.add_argument("--role", choices=sorted(ROLES), required=True)
    initialize_parser.add_argument("--company-name", default="")

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

    lark_parser = subparsers.add_parser("install-lark-cli")
    lark_parser.add_argument("--yes", action="store_true")

    open_parser = subparsers.add_parser("open-obsidian")
    open_parser.add_argument("--vault", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect(args.vault.expanduser().resolve())
        elif args.command == "initialize":
            result = initialize(
                args.vault.expanduser().resolve(),
                args.role,
                args.company_name,
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

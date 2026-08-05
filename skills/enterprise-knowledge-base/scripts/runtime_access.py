#!/usr/bin/env python3
"""Enforce configured company access before every normal knowledge action."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kb_core import (
    COMPANY_CONFIG_SCHEMA,
    ORGANIZATION_SCHEMA,
    CoreError,
    load_json,
    mapping_value,
    validate_company_config,
)
from membership_policy import (
    MEMBERSHIP_VERIFICATION_SCHEMA,
    canonical_hash,
    validate_policy,
    validate_share_scope,
)
from vault_context import discover_vault


ACTIONS = {"query", "collect", "publish", "media", "sync"}


class AccessError(RuntimeError):
    pass


def validate_company(value: Any) -> dict[str, Any]:
    try:
        return validate_company_config(
            value,
            validate_policy=validate_policy,
            validate_share_scope=validate_share_scope,
            membership_verification_schema=MEMBERSHIP_VERIFICATION_SCHEMA,
        )
    except CoreError as exc:
        raise AccessError(str(exc)) from exc


def require_full_company_policy(organization: dict[str, Any]) -> None:
    try:
        scope = validate_share_scope(organization.get("share_scope"))
    except RuntimeError as exc:
        raise AccessError(str(exc)) from exc
    if scope.get("type") != "all-employees" or organization.get("publish_policy") != "members":
        raise AccessError("Company policy must grant query, collect, and publish to all employees.")


def authorize(vault: Path, action: str) -> dict[str, Any]:
    if action not in ACTIONS:
        raise AccessError("Unsupported knowledge action.")
    organization = load_json(vault / ".kb/config/organization.json", {})
    if organization.get("schema") != ORGANIZATION_SCHEMA:
        raise AccessError("Enterprise knowledge setup is incomplete or requires migration.")
    role = organization.get("role")
    if role == "local":
        if action in {"publish", "sync"}:
            raise AccessError("Feishu publication and company sync are unavailable in local-only mode.")
        return {"ok": True, "role": "local", "action": action, "company": False}
    if role not in {"admin", "employee"}:
        raise AccessError("Unsupported enterprise knowledge role.")
    require_full_company_policy(organization)

    mapping = mapping_value(vault)
    mapping_hash = canonical_hash(mapping)
    if not mapping["space_id"] or not isinstance(mapping["nodes"], list):
        raise AccessError("Verified company space mapping is missing.")

    if role == "admin":
        verification = load_json(vault / ".kb/state/membership-verification.json", {})
        if (
            organization.get("membership_verified") is not True
            or verification.get("verified") is not True
            or verification.get("mapping_hash") != mapping_hash
            or verification.get("share_scope_hash")
            != canonical_hash(organization["share_scope"])
            or verification.get("effective_employee_policy") != "members"
        ):
            raise AccessError("Administrator membership readback is incomplete or stale.")
    else:
        try:
            company = validate_company(load_json(vault / ".kb/config/company.json", {}))
        except RuntimeError as exc:
            raise AccessError(str(exc)) from exc
        access = load_json(vault / ".kb/state/employee-access.json", {})
        if (
            company.get("schema") != COMPANY_CONFIG_SCHEMA
            or company.get("space_mapping_hash") != mapping_hash
            or organization.get("employee_access_verified") is not True
            or access.get("verified") is not True
            or access.get("space_id") != mapping["space_id"]
            or access.get("effective_employee_policy") != "members"
            or access.get("membership_hash")
            != company["membership_verification"]["member_list_hash"]
        ):
            raise AccessError("Employee identity and space membership require re-verification.")
    return {
        "ok": True,
        "role": role,
        "action": action,
        "company": True,
        "space_id": mapping["space_id"],
        "capabilities": ["query", "collect", "publish"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--action", choices=sorted(ACTIONS), required=True)
    parser.add_argument("--vault", type=Path)
    args = parser.parse_args()
    try:
        vault = discover_vault(args.vault) if args.vault else discover_vault()
        print(json.dumps(authorize(vault, args.action), ensure_ascii=False, indent=2))
        return 0
    except (AccessError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Small dependency-free primitives shared by knowledge-base scripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable


PLUGIN_VERSION = "0.7.1"
ORGANIZATION_SCHEMA = "kb-organization/v4"
COMPANY_CONFIG_SCHEMA = "kb-company-config/v4"
PROJECT_BINDING_SCHEMA = "kb-project-binding/v1"
COMPANY_SYNC_SCHEMA = "kb-company-sync/v2"
COMPANY_SYNC_SESSION_SCHEMA = "kb-company-sync-session/v1"

SECRET_KEYS = {
    "app_secret",
    "access_token",
    "refresh_token",
    "tenant_access_token",
    "user_access_token",
    "cookie",
    "password",
}


class CoreError(RuntimeError):
    pass


_MISSING = object()


def load_json(path: Path, default: Any = _MISSING) -> Any:
    """Read one JSON value; malformed existing files always fail closed."""
    if not path.is_file():
        if default is _MISSING:
            raise CoreError(f"Missing JSON file: {path}")
        return json.loads(json.dumps(default, ensure_ascii=False))
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoreError(f"Invalid JSON file: {path}") from exc


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def json_cli_output(value: Any, *, compact: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def canonical_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def mapping_value(vault: Path) -> dict[str, Any]:
    mapping = load_json(vault / ".kb/mappings/feishu_nodes.json", {})
    if not isinstance(mapping, dict):
        raise CoreError("Company node mapping must be a JSON object.")
    return {
        "space_name": mapping.get("space_name", ""),
        "space_id": str(mapping.get("space_id", "")),
        "nodes": mapping.get("nodes", []),
    }


def validate_company_mapping(vault: Path) -> dict[str, Any]:
    mapping = load_json(vault / ".kb/mappings/feishu_nodes.json", {})
    if not isinstance(mapping, dict):
        raise CoreError("Verified company space mapping is missing.")
    space_id = str(mapping.get("space_id", "")).strip()
    nodes = mapping.get("nodes")
    if not space_id or not isinstance(nodes, list):
        raise CoreError("Verified company space mapping is missing.")
    if any(
        not isinstance(item, dict)
        or item.get("verified") is not True
        or not str(item.get("node_token", "")).strip()
        for item in nodes
    ):
        raise CoreError("Company space mapping contains an unverified node.")
    return mapping


def company_access_snapshot(vault: Path) -> dict[str, str]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    if not isinstance(organization, dict):
        raise CoreError("Company organization configuration is invalid.")
    role = str(organization.get("role", ""))
    if role == "admin":
        evidence = load_json(vault / ".kb/state/membership-verification.json", {})
        membership_hash = str(evidence.get("member_list_hash", ""))
    elif role == "employee":
        evidence = load_json(vault / ".kb/state/employee-access.json", {})
        membership_hash = str(evidence.get("membership_hash", ""))
    else:
        raise CoreError(
            "Company sync requires a verified administrator or employee role."
        )
    if not membership_hash:
        raise CoreError("Verified company membership evidence is missing.")
    return {
        "role": role,
        "membership_hash": membership_hash,
        "policy_hash": canonical_hash(
            {
                "share_scope": organization.get("share_scope"),
                "publish_policy": organization.get("publish_policy"),
            }
        ),
    }


def reject_secrets(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, candidate in value.items():
            lowered = str(key).casefold()
            if lowered in SECRET_KEYS or "secret" in lowered:
                raise CoreError(
                    f"Company configuration contains a forbidden secret: {path}.{key}"
                )
            reject_secrets(candidate, f"{path}.{key}")
    elif isinstance(value, list):
        for index, candidate in enumerate(value):
            reject_secrets(candidate, f"{path}[{index}]")


def validate_company_config(
    value: Any,
    *,
    validate_policy: Callable[[str], str],
    validate_share_scope: Callable[[Any], dict[str, Any]],
    membership_verification_schema: str,
    plugin_version: str = PLUGIN_VERSION,
) -> dict[str, Any]:
    """Pure validation for the exported, secret-free company configuration."""
    if not isinstance(value, dict) or value.get("schema") != COMPANY_CONFIG_SCHEMA:
        raise CoreError("Unsupported company configuration schema.")
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
        raise CoreError("Company configuration contains unsupported or excessive data.")
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
        raise CoreError("Company configuration is incomplete.")
    if value["feishu_brand"] not in {"feishu", "lark"}:
        raise CoreError("Unsupported Feishu brand.")
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", str(value["app_id"])):
        raise CoreError("Invalid application ID.")
    if not re.fullmatch(r"[0-9]+", str(value["space_id"])):
        raise CoreError("Invalid knowledge-space ID.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value["tenant_key_hash"])):
        raise CoreError("Invalid tenant identity hash.")
    minimum = str(value["minimum_plugin_version"])
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", minimum):
        raise CoreError("Invalid minimum plugin version.")
    if tuple(map(int, minimum.split("."))) > tuple(map(int, plugin_version.split("."))):
        raise CoreError("This company configuration requires a newer plugin version.")
    policy = str(value.get("effective_employee_policy", ""))
    try:
        validate_policy(policy)
        share_scope = validate_share_scope(value.get("share_scope"))
    except RuntimeError as exc:
        raise CoreError(str(exc)) from exc
    if policy != "members" or share_scope.get("type") != "all-employees":
        raise CoreError(
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
        raise CoreError("Company node mapping is invalid or unverified.")
    mapping_hash = canonical_hash(
        {
            "space_name": value["space_name"],
            "space_id": str(value["space_id"]),
            "nodes": nodes,
        }
    )
    if value.get("space_mapping_hash") != mapping_hash:
        raise CoreError("Company space mapping hash is invalid.")
    verification = value.get("membership_verification", {})
    if (
        not isinstance(verification, dict)
        or verification.get("schema") != membership_verification_schema
        or verification.get("verified") is not True
        or str(verification.get("space_id", "")) != str(value["space_id"])
        or verification.get("share_scope_hash") != canonical_hash(value["share_scope"])
        or verification.get("mapping_hash") != mapping_hash
        or verification.get("effective_employee_policy") != policy
        or not str(verification.get("remote_version", "")).strip()
        or not re.fullmatch(
            r"[a-f0-9]{64}", str(verification.get("member_list_hash", ""))
        )
        or verification.get("external_members") != 0
        or verification.get("employee_admin_members") != 0
    ):
        raise CoreError("Company membership verification is incomplete or stale.")
    return value

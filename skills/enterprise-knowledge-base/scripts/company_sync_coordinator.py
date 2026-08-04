#!/usr/bin/env python3
"""Coordinate deterministic, foreground-only company knowledge synchronization."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from membership_policy import canonical_hash
from runtime_access import authorize
from sync_company_knowledge import (
    MIRROR_ROOT,
    STATE_SCHEMA,
    CompanySyncError,
    access_snapshot,
    atomic_write,
    load_json,
    pull,
    sha256_file,
    validate_mapping,
)
from vault_context import discover_vault


SESSION_SCHEMA = "kb-company-sync-session/v1"
QUERY_SCOPES = {"personal", "enterprise", "all"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_hash(session_id: str) -> str:
    value = session_id.strip()
    if not value:
        raise CompanySyncError("A stable current-session identifier is required.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_context(vault: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    mapping = validate_mapping(vault)
    return mapping, load_json(vault / ".kb/state/company_sync.json", {}), access_snapshot(vault)


def validate_local_state(vault: Path) -> dict[str, Any]:
    mapping, state, current_access = state_context(vault)
    if state.get("schema") != STATE_SCHEMA:
        raise CompanySyncError("Company mirror is missing or requires a full foreground sync.")
    if state.get("space_id") != mapping["space_id"]:
        raise CompanySyncError("Company mirror belongs to another space.")
    if state.get("mapping_hash") != canonical_hash(mapping):
        raise CompanySyncError("Company mapping changed; manual review is required.")
    if state.get("access_snapshot") != current_access:
        raise CompanySyncError("Company membership or policy changed; re-verification is required.")
    records = state.get("records")
    if not isinstance(records, dict):
        raise CompanySyncError("Company mirror state is invalid.")
    for record in records.values():
        if not isinstance(record, dict):
            raise CompanySyncError("Company mirror record is invalid.")
        relative = str(record.get("local_path", "")).replace("\\", "/")
        path = vault / Path(relative)
        if record.get("source") == "mirror":
            if not relative.startswith(MIRROR_ROOT.as_posix() + "/") or not path.is_file():
                raise CompanySyncError("A managed company mirror file is missing.")
            if sha256_file(path) != record.get("file_sha256"):
                raise CompanySyncError("A managed company mirror was edited locally; manual review is required.")
        elif record.get("source") == "local-authoritative":
            if not relative.startswith("20_知识/企业/") or not path.is_file():
                raise CompanySyncError("A local authoritative enterprise note is missing.")
        else:
            raise CompanySyncError("Company mirror record source is invalid.")
    return state


def write_session_receipt(vault: Path, session_id: str, state: dict[str, Any]) -> None:
    receipt = {
        "schema": SESSION_SCHEMA,
        "session_id_hash": session_hash(session_id),
        "space_id": state["space_id"],
        "tree_hash": state["tree_hash"],
        "mapping_hash": state["mapping_hash"],
        "access_snapshot": state["access_snapshot"],
        "completed_at": utc_now(),
    }
    atomic_write(
        vault / ".kb/state/company_sync_session.json",
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def initial_employee_sync(vault: Path, reader: Any | None = None) -> dict[str, Any]:
    access = authorize(vault, "sync")
    if access.get("role") != "employee":
        raise CompanySyncError("Initial employee sync is restricted to verified employee setup.")
    result = pull(vault, reader=reader, enforce_access=False)
    return {**result, "trigger": "employee-setup-complete"}


def before_company_query(
    vault: Path,
    scope: str,
    session_id: str,
    reader: Any | None = None,
) -> dict[str, Any]:
    if scope not in QUERY_SCOPES:
        raise CompanySyncError("Unsupported query scope.")
    authorize(vault, "query")
    if scope == "personal":
        return {"ok": True, "trigger": "query", "sync": "not-applicable", "remote_reads": 0}
    current_hash = session_hash(session_id)
    receipt = load_json(vault / ".kb/state/company_sync_session.json", {})
    if receipt.get("schema") == SESSION_SCHEMA and receipt.get("session_id_hash") == current_hash:
        state = validate_local_state(vault)
        if (
            receipt.get("space_id") == state.get("space_id")
            and receipt.get("tree_hash") == state.get("tree_hash")
            and receipt.get("mapping_hash") == state.get("mapping_hash")
            and receipt.get("access_snapshot") == state.get("access_snapshot")
        ):
            return {
                "ok": True,
                "trigger": "session-company-query",
                "sync": "reused-current-session",
                "remote_reads": 0,
                "tree_hash": state["tree_hash"],
            }
    result = pull(vault, reader=reader)
    state = validate_local_state(vault)
    write_session_receipt(vault, session_id, state)
    return {**result, "trigger": "session-company-query", "sync": "incremental"}


def explicit_sync(vault: Path, reader: Any | None = None) -> dict[str, Any]:
    result = pull(vault, reader=reader)
    return {**result, "trigger": "explicit-company-sync", "sync": "incremental"}


def record_publisher_convergence(vault: Path) -> dict[str, Any]:
    authorize(vault, "publish")
    mapping = validate_mapping(vault)
    current_access = access_snapshot(vault)
    state_path = vault / ".kb/state/company_sync.json"
    state = load_json(state_path, {})
    if state and (
        state.get("schema") != STATE_SCHEMA
        or state.get("space_id") != mapping["space_id"]
        or state.get("mapping_hash") != canonical_hash(mapping)
        or state.get("access_snapshot") != current_access
    ):
        raise CompanySyncError("Existing company sync evidence conflicts with current access or mapping.")
    records = dict(state.get("records", {})) if isinstance(state.get("records", {}), dict) else {}
    documents = load_json(vault / ".kb/mappings/feishu_documents.json", {}).get("documents", [])
    converged = 0
    for item in documents:
        if not isinstance(item, dict) or item.get("verified_content") is not True:
            continue
        token = str(item.get("node_token", "")).strip()
        relative = str(item.get("local_path", "")).replace("\\", "/")
        path = vault / Path(relative)
        if not token or not relative.startswith("20_知识/企业/") or not path.is_file():
            continue
        records[token] = {
            "source": "local-authoritative",
            "local_path": relative,
            "obj_token": str(item.get("obj_token", "")),
            "node_fingerprint": canonical_hash(
                {
                    "node_token": token,
                    "obj_token": item.get("obj_token", ""),
                    "remote_hash": item.get("last_synced_remote_hash", ""),
                }
            ),
            "file_sha256": sha256_file(path),
        }
        converged += 1
    if not converged:
        raise CompanySyncError("No verified published enterprise document was available for convergence.")
    tree_hash = canonical_hash(
        sorted((token, record.get("node_fingerprint", "")) for token, record in records.items())
    )
    next_state = {
        "schema": STATE_SCHEMA,
        "space_id": mapping["space_id"],
        "mapping_hash": canonical_hash(mapping),
        "access_snapshot": current_access,
        "last_sync_at": state.get("last_sync_at", ""),
        "publisher_converged_at": utc_now(),
        "freshness": "publisher-current",
        "tree_hash": tree_hash,
        "records": records,
    }
    atomic_write(
        state_path,
        (json.dumps(next_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    session_path = vault / ".kb/state/company_sync_session.json"
    receipt = load_json(session_path, {})
    if receipt.get("schema") == SESSION_SCHEMA:
        receipt.update(
            {
                "tree_hash": tree_hash,
                "mapping_hash": next_state["mapping_hash"],
                "access_snapshot": current_access,
                "completed_at": utc_now(),
            }
        )
        atomic_write(
            session_path,
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    return {"ok": True, "trigger": "verified-publication", "converged": converged, "tree_hash": tree_hash}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["initial-employee", "before-query", "explicit", "publisher-converged"],
    )
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--scope", choices=sorted(QUERY_SCOPES), default="enterprise")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    try:
        vault = discover_vault(args.vault) if args.vault else discover_vault()
        if args.command == "initial-employee":
            result = initial_employee_sync(vault)
        elif args.command == "before-query":
            if not args.session_id:
                raise CompanySyncError("--session-id is required before a company query.")
            result = before_company_query(vault, args.scope, args.session_id)
        elif args.command == "explicit":
            result = explicit_sync(vault)
        else:
            result = record_publisher_convergence(vault)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CompanySyncError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

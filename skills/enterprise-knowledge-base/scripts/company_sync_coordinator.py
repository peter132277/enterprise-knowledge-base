#!/usr/bin/env python3
"""Coordinate deterministic, foreground-only company knowledge synchronization."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kb_core import (
    COMPANY_SYNC_SCHEMA as STATE_SCHEMA,
    COMPANY_SYNC_SESSION_SCHEMA as SESSION_SCHEMA,
    CoreError,
    atomic_write_bytes as atomic_write,
    canonical_hash,
    company_access_snapshot as access_snapshot,
    load_json,
    sha256_bytes,
    sha256_file,
    validate_company_mapping as validate_mapping,
)
from runtime_access import authorize
from vault_context import discover_vault


MIRROR_ROOT = Path("20_知识/企业/共享镜像")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class CompanySyncError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_hash(session_id: str) -> str:
    value = session_id.strip()
    if not value:
        raise CompanySyncError("A stable current-session identifier is required.")
    return sha256_bytes(value.encode("utf-8"))


def safe_mirror_stem(title: str) -> str:
    """Return a portable, readable filename stem derived from a Wiki title."""
    value = unicodedata.normalize("NFC", title).strip()
    if value.casefold().endswith(".md"):
        value = value[:-3].rstrip()
    value = "".join(
        "-" if ord(character) < 32 or character in '<>:"/\\|?*' else character
        for character in value
    )
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = re.sub(r"-{2,}", "-", value)
    value = value[:120].rstrip(" .") or "未命名文档"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value


def mirror_paths(
    remote_by_token: dict[str, dict[str, Any]],
    authoritative: dict[str, str],
) -> dict[str, str]:
    """Assign stable title-based mirror paths; only duplicate titles gain a hash."""
    stems = {
        token: safe_mirror_stem(str(node.get("title", "")))
        for token, node in remote_by_token.items()
        if token not in authoritative
    }
    counts: dict[str, int] = {}
    for stem in stems.values():
        counts[stem.casefold()] = counts.get(stem.casefold(), 0) + 1
    result: dict[str, str] = {}
    used: set[str] = set()
    for token in sorted(stems):
        stem = stems[token]
        if counts[stem.casefold()] > 1:
            suffix = hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]
            stem = f"{stem} [{suffix}]"
        filename = f"{stem}.md"
        key = filename.casefold()
        if key in used:
            suffix = hashlib.sha256(token.encode("utf-8")).hexdigest()
            filename = f"{stems[token]} [{suffix}].md"
            key = filename.casefold()
        if key in used:
            raise CompanySyncError("Wiki titles cannot be mapped to unique mirror filenames.")
        used.add(key)
        result[token] = (MIRROR_ROOT / filename).as_posix()
    return result


def pull(
    vault: Path,
    reader: Any | None = None,
    enforce_access: bool = True,
) -> dict[str, Any]:
    """Run one strict foreground incremental sync."""
    vault = vault.resolve()
    if enforce_access:
        authorize(vault, "sync")
    mapping = validate_mapping(vault)
    space_id = str(mapping["space_id"])
    mapping_hash = canonical_hash(mapping)
    current_access = access_snapshot(vault)
    if reader is None:
        from feishu_company_adapter import LarkKnowledgeReader

        reader = LarkKnowledgeReader()
    nodes = reader.list_tree(space_id)
    remote_by_token = {
        str(item.get("node_token", "")): item
        for item in nodes
        if isinstance(item, dict)
    }
    if len(remote_by_token) != len(nodes):
        raise CompanySyncError("Wiki tree contains duplicate or missing node identities.")

    state_path = vault / ".kb/state/company_sync.json"
    state = load_json(
        state_path,
        {
            "schema": STATE_SCHEMA,
            "space_id": space_id,
            "mapping_hash": mapping_hash,
            "access_snapshot": current_access,
            "records": {},
        },
    )
    if state.get("schema") != STATE_SCHEMA or str(state.get("space_id", "")) != space_id:
        raise CompanySyncError("Company sync state belongs to another space.")
    if state.get("mapping_hash") != mapping_hash:
        raise CompanySyncError("Company mapping changed; manual review is required before sync.")
    if state.get("access_snapshot") != current_access:
        raise CompanySyncError("Company membership or policy changed; re-verification is required.")
    records = state.get("records", {})
    if not isinstance(records, dict):
        raise CompanySyncError("Company sync records are invalid.")
    missing = sorted(
        token
        for token, record in records.items()
        if isinstance(record, dict)
        and record.get("source") == "mirror"
        and token not in remote_by_token
    )
    if missing:
        raise CompanySyncError("Remote Wiki nodes disappeared; manual review is required.")

    authoritative = local_authoritative_nodes(vault)
    assigned_paths = mirror_paths(remote_by_token, authoritative)
    owned_mirror_paths = {
        str(record.get("local_path", "")).replace("\\", "/")
        for record in records.values()
        if isinstance(record, dict) and record.get("source") == "mirror"
    }
    planned: dict[Path, bytes] = {}
    planned_tokens: dict[Path, str] = {}
    stale_paths: set[Path] = set()
    next_records: dict[str, dict[str, Any]] = {}
    fetched_count = added_count = updated_count = unchanged_count = 0
    renamed_count = 0
    skipped_local_count = 0
    for token in sorted(remote_by_token):
        node = remote_by_token[token]
        obj_type = str(node.get("obj_type", ""))
        obj_token = str(node.get("obj_token", "")).strip()
        if obj_type != "docx" or not obj_token:
            raise CompanySyncError("The company Wiki contains an unsupported non-Docx node.")
        fingerprint = node_fingerprint(node)
        if token in authoritative:
            authoritative_path = vault / Path(authoritative[token])
            stat = authoritative_path.stat()
            next_records[token] = {
                "source": "local-authoritative",
                "local_path": authoritative[token],
                "node_fingerprint": fingerprint,
                "obj_token": obj_token,
                "file_size": stat.st_size,
                "file_mtime_ns": stat.st_mtime_ns,
            }
            skipped_local_count += 1
            continue
        relative = assigned_paths[token]
        path = vault / Path(relative)
        previous = records.get(token, {}) if isinstance(records.get(token), dict) else {}
        if previous.get("source") == "mirror":
            previous_relative = str(previous.get("local_path", "")).replace("\\", "/")
            previous_path = vault / Path(previous_relative)
            if not previous_path.is_file() or not previous.get("file_sha256"):
                raise CompanySyncError(
                    f"Managed mirror is missing or unverified: {previous_relative}"
                )
            stat = previous_path.stat()
            metadata_unchanged = (
                previous.get("file_size") == stat.st_size
                and previous.get("file_mtime_ns") == stat.st_mtime_ns
            )
            if not metadata_unchanged and sha256_file(previous_path) != previous["file_sha256"]:
                raise CompanySyncError(f"Managed mirror was edited locally: {previous_relative}")
            previous = {
                **previous,
                "file_size": stat.st_size,
                "file_mtime_ns": stat.st_mtime_ns,
            }
            if path != previous_path and path.exists() and relative not in owned_mirror_paths:
                raise CompanySyncError(f"Mirror filename conflicts with a local file: {relative}")
            if path != previous_path:
                stale_paths.add(previous_path)
        elif path.exists() and relative not in owned_mirror_paths:
            raise CompanySyncError(f"Mirror filename conflicts with a local file: {relative}")
        if (
            previous.get("source") == "mirror"
            and previous.get("node_fingerprint") == fingerprint
        ):
            previous_path = vault / Path(str(previous["local_path"]))
            if previous_path == path:
                next_records[token] = previous
                unchanged_count += 1
            else:
                data = previous_path.read_bytes()
                planned[path] = data
                planned_tokens[path] = token
                next_records[token] = {
                    **previous,
                    "local_path": relative,
                    "file_size": len(data),
                    "file_mtime_ns": 0,
                }
                renamed_count += 1
            continue
        fetched = reader.fetch_markdown(obj_token)
        data = mirror_bytes(node, fetched, space_id)
        planned[path] = data
        planned_tokens[path] = token
        next_records[token] = {
            "source": "mirror",
            "local_path": relative,
            "node_fingerprint": fingerprint,
            "obj_token": obj_token,
            "revision_id": str(fetched["revision_id"]),
            "file_sha256": sha256_bytes(data),
            "file_size": len(data),
            "file_mtime_ns": 0,
        }
        fetched_count += 1
        if previous:
            updated_count += 1
        else:
            added_count += 1

    target_paths = {
        vault / Path(str(record["local_path"]))
        for record in next_records.values()
        if record.get("source") == "mirror"
    }
    delete_paths = stale_paths - target_paths
    transaction_paths = set(planned) | delete_paths
    backups = {
        path: path.read_bytes() if path.is_file() else None
        for path in transaction_paths
    }
    state_backup = state_path.read_bytes() if state_path.is_file() else None
    next_state = {
        "schema": STATE_SCHEMA,
        "space_id": space_id,
        "mapping_hash": mapping_hash,
        "access_snapshot": current_access,
        "last_sync_at": utc_now(),
        "freshness": "current",
        "tree_hash": canonical_hash(
            sorted(
                (token, node_fingerprint(node))
                for token, node in remote_by_token.items()
            )
        ),
        "records": next_records,
    }
    try:
        for path, data in planned.items():
            atomic_write(path, data)
            stat = path.stat()
            next_records[planned_tokens[path]]["file_size"] = stat.st_size
            next_records[planned_tokens[path]]["file_mtime_ns"] = stat.st_mtime_ns
        atomic_write(
            state_path,
            (json.dumps(next_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        for path in delete_paths:
            path.unlink(missing_ok=True)
    except OSError:
        for path, data in backups.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        if state_backup is None:
            state_path.unlink(missing_ok=True)
        else:
            atomic_write(state_path, state_backup)
        raise
    return {
        "ok": True,
        "space_id": space_id,
        "remote_nodes": len(nodes),
        "fetched": fetched_count,
        "added": added_count,
        "updated": updated_count,
        "unchanged": unchanged_count,
        "renamed": renamed_count,
        "local_authoritative": skipped_local_count,
        "manual_review": 0,
        "mirror_root": MIRROR_ROOT.as_posix(),
        "tree_hash": next_state["tree_hash"],
    }


def node_fingerprint(node: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "node_token": str(node.get("node_token", "")),
            "obj_token": str(node.get("obj_token", "")),
            "obj_type": str(node.get("obj_type", "")),
            "title": str(node.get("title", "")),
            "obj_edit_time": str(node.get("obj_edit_time", "")),
            "has_child": bool(node.get("has_child", False)),
        }
    )


def mirror_bytes(node: dict[str, Any], fetched: dict[str, Any], space_id: str) -> bytes:
    node_token = str(node["node_token"])
    obj_token = str(node["obj_token"])
    title = str(node.get("title", "Untitled")).strip() or "Untitled"
    content = str(fetched["content"]).replace("\r\n", "\n").strip()
    frontmatter = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "scope: enterprise",
        "publish_to_feishu: false",
        "publication_excluded: true",
        "managed_mirror: true",
        "managed_by: enterprise-knowledge-base",
        f"feishu_space_id: {json.dumps(str(space_id))}",
        f"feishu_node_token: {json.dumps(node_token)}",
        f"feishu_obj_token: {json.dumps(obj_token)}",
        f"feishu_revision: {json.dumps(str(fetched['revision_id']))}",
        "---",
        "",
    ]
    return ("\n".join(frontmatter) + content + "\n").encode("utf-8")


def local_authoritative_nodes(vault: Path) -> dict[str, str]:
    mappings = load_json(
        vault / ".kb/mappings/feishu_documents.json",
        {"version": 2, "documents": []},
    )
    result: dict[str, str] = {}
    for item in mappings.get("documents", []):
        if not isinstance(item, dict) or item.get("verified_content") is not True:
            continue
        token = str(item.get("node_token", ""))
        relative = str(item.get("local_path", "")).replace("\\", "/")
        path = vault / Path(relative)
        if token and relative.startswith("20_知识/企业/") and path.is_file():
            result[token] = relative
    return result


def state_context(vault: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    mapping = validate_mapping(vault)
    return mapping, load_json(vault / ".kb/state/company_sync.json", {}), access_snapshot(vault)


def validate_local_state(
    vault: Path,
    *,
    verify_file_hashes: bool = False,
) -> dict[str, Any]:
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
            if (
                not relative.startswith(MIRROR_ROOT.as_posix() + "/")
                or not str(record.get("file_sha256", ""))
                or not isinstance(record.get("file_size"), int)
                or not isinstance(record.get("file_mtime_ns"), int)
            ):
                raise CompanySyncError("A managed company mirror record is invalid.")
            if verify_file_hashes:
                if not path.is_file():
                    raise CompanySyncError("A managed company mirror file is missing.")
                if sha256_file(path) != record.get("file_sha256"):
                    raise CompanySyncError("A managed company mirror was edited locally; manual review is required.")
        elif record.get("source") == "local-authoritative":
            if not relative.startswith("20_知识/企业/"):
                raise CompanySyncError("A local authoritative enterprise record is invalid.")
            if verify_file_hashes and not path.is_file():
                raise CompanySyncError("A local authoritative enterprise note is missing.")
        else:
            raise CompanySyncError("Company mirror record source is invalid.")
    return state


def credential_hash(receipt: dict[str, Any]) -> str:
    body = dict(receipt)
    body.pop("credential_sha256", None)
    return canonical_hash(body)


def validate_session_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("schema") != SESSION_SCHEMA:
        raise CompanySyncError("Company sync credential is missing or invalid.")
    recorded = str(receipt.get("credential_sha256", ""))
    if not recorded or recorded != credential_hash(receipt):
        raise CompanySyncError("Company sync credential hash is invalid.")
    if not str(receipt.get("session_id_hash", "")):
        raise CompanySyncError("Company sync credential has no task identity.")
    return receipt


def write_session_receipt(
    vault: Path,
    session_id: str,
    state: dict[str, Any] | None,
    *,
    sync_status: str,
) -> dict[str, Any]:
    receipt = {
        "schema": SESSION_SCHEMA,
        "session_id_hash": session_hash(session_id),
        "sync_status": sync_status,
        "completed_at": utc_now(),
    }
    if state is not None:
        receipt.update(
            {
                "space_id": state["space_id"],
                "tree_hash": state["tree_hash"],
                "mapping_hash": state["mapping_hash"],
                "access_snapshot": state["access_snapshot"],
            }
        )
    receipt["credential_sha256"] = credential_hash(receipt)
    atomic_write(
        vault / ".kb/state/company_sync_session.json",
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return receipt


def initial_employee_sync(vault: Path, reader: Any | None = None) -> dict[str, Any]:
    access = authorize(vault, "sync")
    if access.get("role") != "employee":
        raise CompanySyncError("Initial employee sync is restricted to verified employee setup.")
    result = pull(vault, reader=reader, enforce_access=False)
    return {**result, "trigger": "employee-setup-complete"}


def before_knowledge_query(
    vault: Path,
    session_id: str,
    reader: Any | None = None,
) -> dict[str, Any]:
    access = authorize(vault, "query")
    current_hash = session_hash(session_id)
    if access.get("company") is not True:
        receipt = load_json(vault / ".kb/state/company_sync_session.json", {})
        if isinstance(receipt, dict):
            try:
                receipt = validate_session_receipt(receipt)
            except CompanySyncError:
                receipt = {}
        if (
            receipt.get("session_id_hash") != current_hash
            or receipt.get("sync_status") != "local-only"
        ):
            receipt = write_session_receipt(
                vault,
                session_id,
                None,
                sync_status="local-only",
            )
        return {
            "ok": True,
            "trigger": "session-all-knowledge-query",
            "sync": "local-only",
            "remote_reads": 0,
            "sync_credential": receipt,
        }
    receipt = load_json(vault / ".kb/state/company_sync_session.json", {})
    try:
        receipt = validate_session_receipt(receipt)
    except CompanySyncError:
        receipt = {}
    if receipt.get("session_id_hash") == current_hash:
        state = validate_local_state(vault)
        if (
            receipt.get("space_id") == state.get("space_id")
            and receipt.get("tree_hash") == state.get("tree_hash")
            and receipt.get("mapping_hash") == state.get("mapping_hash")
            and receipt.get("access_snapshot") == state.get("access_snapshot")
        ):
            return {
                "ok": True,
                "trigger": "session-all-knowledge-query",
                "sync": "reused-current-session",
                "remote_reads": 0,
                "tree_hash": state["tree_hash"],
                "sync_credential": receipt,
            }
    result = pull(vault, reader=reader)
    state = validate_local_state(vault)
    receipt = write_session_receipt(
        vault,
        session_id,
        state,
        sync_status="foreground-current",
    )
    return {
        **result,
        "trigger": "session-all-knowledge-query",
        "sync": "incremental",
        "sync_credential": receipt,
    }


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
            "file_size": path.stat().st_size,
            "file_mtime_ns": path.stat().st_mtime_ns,
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
    try:
        receipt = validate_session_receipt(receipt)
    except CompanySyncError:
        receipt = {}
    if receipt:
        receipt.update(
            {
                "tree_hash": tree_hash,
                "mapping_hash": next_state["mapping_hash"],
                "access_snapshot": current_access,
                "completed_at": utc_now(),
            }
        )
        receipt["credential_sha256"] = credential_hash(receipt)
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
    parser.add_argument("--session-id")
    args = parser.parse_args()
    try:
        vault = discover_vault(args.vault) if args.vault else discover_vault()
        if args.command == "initial-employee":
            result = initial_employee_sync(vault)
        elif args.command == "before-query":
            if not args.session_id:
                raise CompanySyncError("--session-id is required before a knowledge query.")
            result = before_knowledge_query(vault, args.session_id)
        elif args.command == "explicit":
            result = explicit_sync(vault)
        else:
            result = record_publisher_convergence(vault)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CompanySyncError, CoreError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

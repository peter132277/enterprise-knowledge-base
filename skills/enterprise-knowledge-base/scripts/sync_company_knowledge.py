#!/usr/bin/env python3
"""Mirror the verified private Feishu Wiki into the current local Vault."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feishu_company_adapter import LarkKnowledgeReader
from membership_policy import canonical_hash
from runtime_access import authorize
from vault_context import discover_vault


STATE_SCHEMA = "kb-company-sync/v2"
MIRROR_ROOT = Path("20_知识/企业/共享镜像")


class CompanySyncError(RuntimeError):
    pass


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompanySyncError(f"Invalid local sync state: {path}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def node_fingerprint(node: dict[str, Any]) -> str:
    value = {
        key: node.get(key, "")
        for key in (
            "node_token",
            "obj_token",
            "obj_type",
            "title",
            "parent_node_token",
            "obj_create_time",
            "obj_edit_time",
            "node_create_time",
        )
    }
    return sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def mirror_bytes(node: dict[str, Any], fetched: dict[str, Any], space_id: str) -> bytes:
    title = str(node.get("title") or node.get("node_name") or "未命名知识")
    node_token = str(node["node_token"])
    obj_token = str(node["obj_token"])
    content = str(fetched["content"]).replace("\r\n", "\n").strip()
    frontmatter = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "type: company-mirror",
        "scope: enterprise",
        "status: published",
        "publish_status: published",
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


def validate_mapping(vault: Path) -> dict[str, Any]:
    mapping = load_json(vault / ".kb/mappings/feishu_nodes.json", {})
    space_id = str(mapping.get("space_id", "")).strip()
    if not space_id or not isinstance(mapping.get("nodes"), list):
        raise CompanySyncError("Verified company space mapping is missing.")
    if any(
        not isinstance(item, dict)
        or item.get("verified") is not True
        or not str(item.get("node_token", "")).strip()
        for item in mapping["nodes"]
    ):
        raise CompanySyncError("Company space mapping contains an unverified node.")
    return mapping


def access_snapshot(vault: Path) -> dict[str, str]:
    organization = load_json(vault / ".kb/config/organization.json", {})
    role = str(organization.get("role", ""))
    if role == "admin":
        evidence = load_json(vault / ".kb/state/membership-verification.json", {})
        membership_hash = str(evidence.get("member_list_hash", ""))
    elif role == "employee":
        evidence = load_json(vault / ".kb/state/employee-access.json", {})
        membership_hash = str(evidence.get("membership_hash", ""))
    else:
        raise CompanySyncError("Company sync requires a verified administrator or employee role.")
    if not membership_hash:
        raise CompanySyncError("Verified company membership evidence is missing.")
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


def pull(
    vault: Path,
    reader: Any | None = None,
    enforce_access: bool = True,
) -> dict[str, Any]:
    vault = vault.resolve()
    if enforce_access:
        authorize(vault, "sync")
    mapping = validate_mapping(vault)
    space_id = str(mapping["space_id"])
    mapping_hash = canonical_hash(mapping)
    current_access = access_snapshot(vault)
    reader = reader or LarkKnowledgeReader()
    nodes = reader.list_tree(space_id)
    remote_by_token = {
        str(item.get("node_token", "")): item for item in nodes if isinstance(item, dict)
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
    planned: dict[Path, bytes] = {}
    next_records: dict[str, dict[str, Any]] = {}
    fetched_count = 0
    added_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_local_count = 0
    for token in sorted(remote_by_token):
        node = remote_by_token[token]
        obj_type = str(node.get("obj_type", ""))
        obj_token = str(node.get("obj_token", "")).strip()
        if obj_type != "docx" or not obj_token:
            raise CompanySyncError("The company Wiki contains an unsupported non-Docx node.")
        fingerprint = node_fingerprint(node)
        if token in authoritative:
            next_records[token] = {
                "source": "local-authoritative",
                "local_path": authoritative[token],
                "node_fingerprint": fingerprint,
                "obj_token": obj_token,
            }
            skipped_local_count += 1
            continue
        relative = (MIRROR_ROOT / f"{token}.md").as_posix()
        path = vault / Path(relative)
        previous = records.get(token, {}) if isinstance(records.get(token), dict) else {}
        if path.is_file() and previous.get("file_sha256"):
            if sha256_file(path) != previous["file_sha256"]:
                raise CompanySyncError(f"Managed mirror was edited locally: {relative}")
        if (
            previous.get("source") == "mirror"
            and previous.get("node_fingerprint") == fingerprint
            and path.is_file()
        ):
            next_records[token] = previous
            unchanged_count += 1
            continue
        fetched = reader.fetch_markdown(obj_token)
        data = mirror_bytes(node, fetched, space_id)
        planned[path] = data
        next_records[token] = {
            "source": "mirror",
            "local_path": relative,
            "node_fingerprint": fingerprint,
            "obj_token": obj_token,
            "revision_id": str(fetched["revision_id"]),
            "file_sha256": sha256_bytes(data),
        }
        fetched_count += 1
        if previous:
            updated_count += 1
        else:
            added_count += 1

    backups: dict[Path, bytes | None] = {
        path: path.read_bytes() if path.is_file() else None for path in planned
    }
    state_backup = state_path.read_bytes() if state_path.is_file() else None
    next_state = {
        "schema": STATE_SCHEMA,
        "space_id": space_id,
        "mapping_hash": mapping_hash,
        "access_snapshot": current_access,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "freshness": "current",
        "tree_hash": sha256_bytes(
            json.dumps(
                sorted((token, node_fingerprint(node)) for token, node in remote_by_token.items()),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "records": next_records,
    }
    state_data = (json.dumps(next_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        for path, data in planned.items():
            atomic_write(path, data)
        atomic_write(state_path, state_data)
    except OSError:
        for path, data in backups.items():
            if data is None:
                if path.exists():
                    path.unlink()
            else:
                atomic_write(path, data)
        if state_backup is None:
            if state_path.exists():
                state_path.unlink()
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
        "local_authoritative": skipped_local_count,
        "manual_review": 0,
        "mirror_root": MIRROR_ROOT.as_posix(),
        "tree_hash": next_state["tree_hash"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pull"])
    parser.add_argument("--vault", type=Path)
    args = parser.parse_args()
    try:
        vault = discover_vault(args.vault) if args.vault else discover_vault()
        result = pull(vault)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CompanySyncError, OSError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

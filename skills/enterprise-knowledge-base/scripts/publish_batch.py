#!/usr/bin/env python3
"""Create, confirm, and journal immutable enterprise publish batches."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA = "kb-publish-batch/v1"
EXACT_CONFIRMATION = "确认批量发布"
TERMINAL_ITEM_STATES = {"success", "collision", "conflict", "failed"}
FRONTMATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|$)", re.DOTALL)
FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")


class BatchError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_in_vault(vault: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = vault / path
    path = path.resolve()
    if not within(path, vault):
        raise BatchError(f"Path escapes Vault: {value}")
    return path


def relative_posix(path: Path, vault: Path) -> str:
    return path.relative_to(vault).as_posix()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is not None:
            return copy.deepcopy(default)
        raise BatchError(f"Missing JSON file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BatchError(f"Invalid JSON file: {path}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def atomic_write_many(changes: dict[Path, bytes]) -> None:
    """Replace related local state files as one rollback-capable transaction."""
    snapshots = {
        path: (path.exists(), path.read_bytes() if path.exists() else b"")
        for path in changes
    }
    temps: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, data in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temp.write_bytes(data)
            temps[path] = temp
        for path, temp in temps.items():
            os.replace(temp, path)
            replaced.append(path)
    except Exception:
        for path in reversed(replaced):
            existed, data = snapshots[path]
            if existed:
                restore = path.with_name(f".{path.name}.{os.getpid()}.restore")
                restore.write_bytes(data)
                os.replace(restore, path)
            elif path.exists():
                path.unlink()
        raise
    finally:
        for temp in temps.values():
            if temp.exists():
                temp.unlink()


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value), ensure_ascii=False)


def update_frontmatter(text: str, fields: dict[str, Any]) -> str:
    match = FRONTMATTER.match(text)
    if not match:
        raise BatchError("Published note has no valid Frontmatter.")
    body = match.group("body")
    for field, value in fields.items():
        replacement = f"{field}: {yaml_scalar(value)}"
        pattern = re.compile(rf"(?m)^{re.escape(field)}:\s*.*$")
        if pattern.search(body):
            body = pattern.sub(replacement, body, count=1)
        else:
            body = body.rstrip() + "\n" + replacement
    return text[: match.start("body")] + body + text[match.end("body") :]


def run_helper(script: Path, arguments: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw = result.stdout.strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise BatchError(f"{script.name} returned invalid JSON") from exc
    if result.returncode != 0 or payload.get("ok") is not True:
        error = payload.get("error") or result.stderr.strip() or "unknown error"
        raise BatchError(f"{script.name}: {error}")
    return payload


def resolve_document_mapping(
    vault: Path,
    note: Path,
    mapping: dict[str, Any],
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for document in mapping.get("documents", []):
        local_path = document.get("local_path")
        if not local_path:
            continue
        try:
            mapped = resolve_in_vault(vault, local_path)
        except BatchError:
            continue
        if mapped == note:
            matches.append(document)
    if len(matches) > 1:
        raise BatchError("Multiple local Feishu document mappings exist for one note.")
    return matches[0] if matches else None


def source_snapshot(
    vault: Path,
    source_path: str,
    claimed_hash: str,
    source_file: str = "",
    processed: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    if not source_path:
        return "", claimed_hash.lower(), ""
    digest = claimed_hash.lower()
    records = processed.get("files", []) if isinstance(processed, dict) else []
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict) and str(item.get("sha256", "")).lower() == digest
        ),
        None,
    )
    managed_path = str((record or {}).get("stored_original", "")).strip() or source_path
    display_name = (
        str((record or {}).get("source_name", "")).strip()
        or Path(source_file).name
        or Path(managed_path).name
    )
    source = resolve_in_vault(vault, managed_path)
    if not source.is_file():
        raise BatchError("Preserved source file is missing.")
    actual_hash = sha256_file(source)
    if claimed_hash and actual_hash.lower() != digest:
        raise BatchError("Preserved source hash no longer matches the note.")
    return relative_posix(source, vault), actual_hash, display_name


def validated_preuploaded_source_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not any(host.endswith(suffix) for suffix in FEISHU_HOST_SUFFIXES)
        or "/file/" not in parsed.path
    ):
        raise BatchError("Pre-uploaded source URL is not a valid Feishu Drive file URL.")
    return value


def safe_mapping_snapshot(mapping: dict[str, Any] | None) -> dict[str, Any] | None:
    if mapping is None:
        return None
    keys = (
        "node_token",
        "obj_token",
        "feishu_url",
        "source_file_url",
        "source_hash",
        "source_outside_wiki_verified",
        "final_payload_hash",
        "last_synced_payload_hash",
        "last_synced_remote_hash",
    )
    return {key: mapping.get(key) for key in keys if mapping.get(key)}


def prepare_preview_items(
    vault: Path,
    scan: dict[str, Any],
    documents: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scripts = Path(__file__).resolve().parent
    blocked = list(scan.get("blocked", []))
    processed = load_json(vault / ".kb/state/processed_files.json", {"files": []})
    items: list[dict[str, Any]] = []
    for candidate in scan.get("ready", []):
        note_relative = candidate["path"]
        try:
            note = resolve_in_vault(vault, note_relative)
            prepared = run_helper(
                scripts / "prepare_publish.py",
                [
                    "--allow-pending",
                    "--vault",
                    str(vault),
                    "--note",
                    note_relative,
                ],
            )
            source_relative, source_hash, source_display_name = source_snapshot(
                vault,
                candidate.get("source_path", ""),
                candidate.get("source_hash", ""),
                candidate.get("source_file", ""),
                processed,
            )
            source_preuploaded_url = validated_preuploaded_source_url(
                candidate.get("source_feishu_file_url", "")
            )
            local_mapping = resolve_document_mapping(vault, note, documents)
            immutable = {
                "note_relative": relative_posix(note, vault),
                "note_hash": sha256_file(note),
                "source_relative": source_relative,
                "source_hash": source_hash,
                "source_display_name": source_display_name,
                "source_preuploaded_url": source_preuploaded_url,
                "title": prepared["title"],
                "scope": prepared["scope"],
                "parent_node_name": prepared["parent_node_name"],
                "parent_node_token": prepared["parent_node_token"],
                "payload_relative": Path(prepared["payload_relative"]).as_posix(),
                "payload_hash": prepared["payload_hash"],
                "payload_bytes": prepared["payload_bytes"],
                "local_intention": "update" if local_mapping else "create",
                "mapping": safe_mapping_snapshot(local_mapping),
            }
            item_id = canonical_hash(immutable)[:16]
            items.append(
                {
                    "item_id": item_id,
                    **immutable,
                    "state": "pending",
                    "attempts": 0,
                    "journal": {},
                }
            )
        except (BatchError, OSError, UnicodeError) as exc:
            blocked.append(
                {
                    "path": note_relative,
                    "title": candidate.get("title", ""),
                    "reason": str(exc),
                }
            )
    return items, blocked


def build_preview_manifest(
    vault: Path,
    items: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
    scan: dict[str, Any],
    documents_path: Path,
    *,
    created_at: str | None,
) -> tuple[str, str, dict[str, Any]]:
    created = created_at or datetime.now().astimezone().isoformat()
    nodes_path = vault / ".kb/mappings/feishu_nodes.json"
    nodes_mapping = load_json(nodes_path, {})
    space_name = str(nodes_mapping.get("space_name", "")).strip()
    space_id = str(nodes_mapping.get("space_id", "")).strip()
    if not space_name or not space_id:
        raise BatchError("Verified Feishu space mapping is incomplete.")
    immutable_batch = {
        "created_at": created,
        "space_name": space_name,
        "space_id": space_id,
        "nodes_mapping_hash": sha256_file(nodes_path),
        "documents_mapping_hash": (
            sha256_file(documents_path) if documents_path.is_file() else ""
        ),
        "items": [
            {
                key: item[key]
                for key in (
                    "item_id",
                    "note_relative",
                    "note_hash",
                    "source_relative",
                    "source_hash",
                    "source_display_name",
                    "source_preuploaded_url",
                    "title",
                    "scope",
                    "parent_node_name",
                    "parent_node_token",
                    "payload_relative",
                    "payload_hash",
                    "payload_bytes",
                    "local_intention",
                    "mapping",
                )
            }
            for item in items
        ],
        "blocked": blocked,
    }
    batch_id = f"kbp-{canonical_hash(immutable_batch)[:20]}"
    manifest = {
        "schema": SCHEMA,
        "batch_id": batch_id,
        "state": "previewed",
        **immutable_batch,
        "published_count": scan.get("published_count", 0),
        "items": items,
        "summary": {
            "ready": len(items),
            "blocked": len(blocked),
            "already_published": scan.get("published_count", 0),
            "success": 0,
            "collision": 0,
            "conflict": 0,
            "failed": 0,
            "retryable": 0,
        },
    }
    return created, batch_id, manifest


def preview_response(
    vault: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    items = manifest["items"]
    blocked = manifest["blocked"]
    return {
        "ok": True,
        "empty": False,
        "preview": {
            "message": "准备发布以下内容",
            "items": [
                {
                    "title": item["title"],
                    "destination": item["parent_node_name"],
                    "original": (
                        "无原件"
                        if not item["source_relative"]
                        else "将上传并关联"
                        if not item.get("source_preuploaded_url")
                        and not (item.get("mapping") or {}).get("source_file_url")
                        else "复用已上传原件"
                    ),
                }
                for item in items
            ],
            "blocked": [
                {"title": item.get("title", ""), "message": item.get("reason", "")}
                for item in blocked
            ],
            "confirmation": EXACT_CONFIRMATION,
        },
        "_internal": {
            "batch_id": manifest["batch_id"],
            "manifest_relative": relative_posix(manifest_path, vault),
        },
    }


def create_preview(
    vault: Path,
    *,
    created_at: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    vault = vault.resolve()
    scripts = Path(__file__).resolve().parent
    scan = run_helper(
        scripts / "scan_pending.py",
        ["--vault", str(vault)],
    )
    documents_path = vault / ".kb/mappings/feishu_documents.json"
    documents = load_json(documents_path, {"version": 2, "documents": []})
    items, blocked = prepare_preview_items(vault, scan, documents)
    if not items:
        return {
            "ok": True,
            "empty": True,
            "preview": {
                "message": "没有待批量发布的企业知识",
                "blocked": [
                    {"title": item.get("title", ""), "message": item.get("reason", "")}
                    for item in blocked
                ],
            },
        }

    created, batch_id, manifest = build_preview_manifest(
        vault,
        items,
        blocked,
        scan,
        documents_path,
        created_at=created_at,
    )
    state_dir = vault / ".kb/state"
    manifest_path = state_dir / f"publish_batch_{batch_id}.json"
    pointer_path = state_dir / "publish_batch_current.json"
    if persist:
        atomic_write_json(manifest_path, manifest)
        atomic_write_json(
            pointer_path,
            {
                "schema": SCHEMA,
                "batch_id": batch_id,
                "manifest_relative": relative_posix(manifest_path, vault),
                "created_at": created,
            },
        )
    return preview_response(vault, manifest_path, manifest)


def validate_immutable_batch(vault: Path, manifest: dict[str, Any]) -> None:
    vault = vault.resolve()
    if manifest.get("schema") != SCHEMA:
        raise BatchError("Unsupported batch manifest schema.")
    pointer = load_json(vault / ".kb/state/publish_batch_current.json")
    if pointer.get("batch_id") != manifest.get("batch_id"):
        raise BatchError("This is not the most recently previewed batch.")

    nodes_path = vault / ".kb/mappings/feishu_nodes.json"
    documents_path = vault / ".kb/mappings/feishu_documents.json"
    if sha256_file(nodes_path) != manifest.get("nodes_mapping_hash"):
        raise BatchError("Feishu destination mapping changed after preview.")
    nodes_mapping = load_json(nodes_path, {})
    if (
        str(nodes_mapping.get("space_name", "")).strip() != manifest.get("space_name")
        or str(nodes_mapping.get("space_id", "")).strip() != manifest.get("space_id")
    ):
        raise BatchError("Feishu space identity changed after preview.")
    current_documents_hash = (
        sha256_file(documents_path) if documents_path.is_file() else ""
    )
    if current_documents_hash != manifest.get("documents_mapping_hash"):
        raise BatchError("Feishu document mapping changed after preview.")

    scripts = Path(__file__).resolve().parent
    for item in manifest.get("items", []):
        if item.get("scope") != "enterprise":
            raise BatchError("Only enterprise-scoped knowledge may be confirmed for publication.")
        note = resolve_in_vault(vault, item["note_relative"])
        if not note.is_file() or sha256_file(note) != item["note_hash"]:
            raise BatchError(f"Note changed after preview: {item['note_relative']}")
        if item.get("source_relative"):
            source = resolve_in_vault(vault, item["source_relative"])
            if not source.is_file() or sha256_file(source) != item["source_hash"]:
                raise BatchError(
                    f"Source changed after preview: {item['source_relative']}"
                )
        prepared = run_helper(
            scripts / "prepare_publish.py",
            [
                "--allow-pending",
                "--vault",
                str(vault),
                "--note",
                item["note_relative"],
            ],
        )
        if prepared["payload_hash"] != item["payload_hash"]:
            raise BatchError(f"Payload changed after preview: {item['note_relative']}")
        if prepared.get("scope") != "enterprise":
            raise BatchError(f"Scope changed after preview: {item['note_relative']}")
        if prepared["parent_node_token"] != item["parent_node_token"]:
            raise BatchError(f"Target changed after preview: {item['note_relative']}")
        if (
            prepared["space_name"] != manifest.get("space_name")
            or prepared["space_id"] != manifest.get("space_id")
        ):
            raise BatchError(f"Space changed after preview: {item['note_relative']}")


def confirm_batch(
    vault: Path,
    manifest_path: Path,
    phrase: str,
    *,
    confirmed_at: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    vault = vault.resolve()
    if phrase != EXACT_CONFIRMATION:
        raise BatchError(f"Confirmation must be exactly: {EXACT_CONFIRMATION}")
    path = resolve_in_vault(vault, manifest_path)
    manifest = load_json(path)
    validate_immutable_batch(vault, manifest)
    if manifest.get("state") in {"confirmed", "executing", "completed"}:
        return {
            "ok": True,
            "batch_id": manifest["batch_id"],
            "state": manifest["state"],
            "idempotent": True,
        }
    if manifest.get("state") != "previewed":
        raise BatchError("Batch is not awaiting confirmation.")
    manifest["state"] = "confirmed"
    manifest["confirmed_at"] = (
        confirmed_at or datetime.now().astimezone().isoformat()
    )
    if persist:
        atomic_write_json(path, manifest)
    return {
        "ok": True,
        "batch_id": manifest["batch_id"],
        "state": "confirmed",
        "idempotent": False,
    }


def decide_remote_action(
    item: dict[str, Any],
    remote_children: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = [
        child for child in remote_children if child.get("title") == item.get("title")
    ]
    mapping = item.get("mapping")
    if len(matches) > 1:
        return {"action": "collision", "reason": "multiple_exact_titles"}
    if not matches:
        if mapping:
            return {"action": "conflict", "reason": "mapped_remote_missing"}
        return {"action": "create", "reason": "no_exact_title"}

    remote = matches[0]
    if not mapping:
        return {"action": "collision", "reason": "unmapped_existing_title"}
    if remote.get("node_token") != mapping.get("node_token"):
        return {"action": "collision", "reason": "mapped_token_mismatch"}

    baseline = mapping.get("last_synced_remote_hash") or mapping.get(
        "last_synced_payload_hash"
    )
    remote_hash = remote.get("content_hash")
    if baseline and remote_hash and baseline != remote_hash:
        return {"action": "conflict", "reason": "remote_manual_edit"}
    return {
        "action": "update",
        "reason": "verified_mapping",
        "requires_baseline_check": not bool(baseline and remote_hash),
    }


def retry_plan(item: dict[str, Any]) -> dict[str, Any]:
    journal = item.get("journal", {})
    mapping = item.get("mapping") or {}
    if item.get("state") == "success" and journal.get("verified") is True:
        return {"action": "skip", "reason": "already_verified"}
    return {
        "action": "retry_same_item",
        "document": (
            "reuse_existing"
            if journal.get("node_token") or mapping.get("node_token")
            else "create_once"
        ),
        "source": (
            "reuse_existing"
            if (
                journal.get("source_file_url")
                or mapping.get("source_file_url")
                or item.get("source_preuploaded_url")
            )
            else ("upload_once" if item.get("source_relative") else "none")
        ),
        "item_id": item.get("item_id"),
    }


def record_outcome(
    manifest: dict[str, Any],
    item_id: str,
    outcome: str,
    *,
    verified: bool = False,
    node_token: str = "",
    source_file_url: str = "",
    source_outside_wiki_verified: bool = False,
    final_payload_hash: str = "",
    obj_token: str = "",
    feishu_url: str = "",
    last_synced_remote_hash: str = "",
    reason: str = "",
) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(manifest)
    items = updated.get("items", [])
    item = next((candidate for candidate in items if candidate["item_id"] == item_id), None)
    if item is None:
        raise BatchError("Item does not belong to this batch.")
    if item.get("state") == "success":
        if outcome == "success" and item.get("journal", {}).get("verified") is True:
            return updated, True
        raise BatchError("A verified success cannot be replaced.")
    current_state = item.get("state")
    recover_unverified_failure = False
    if current_state in TERMINAL_ITEM_STATES:
        if current_state == outcome:
            return updated, True
        recover_unverified_failure = (
            current_state == "failed"
            and outcome == "transient-failure"
            and item.get("journal", {}).get("verified") is not True
        )
        if not recover_unverified_failure:
            raise BatchError("A terminal item outcome cannot be replaced.")
    if updated.get("state") not in {"confirmed", "executing"} and not (
        updated.get("state") == "completed" and recover_unverified_failure
    ):
        raise BatchError("Batch must be confirmed before recording outcomes.")
    if outcome == "success" and not verified:
        raise BatchError("Success requires read-back verification.")
    if outcome == "success" and not (
        node_token
        or item.get("journal", {}).get("node_token")
        or (item.get("mapping") or {}).get("node_token")
    ):
        raise BatchError("Success requires a verified document node.")
    if outcome == "success" and item.get("source_relative"):
        effective_source_url = (
            source_file_url
            or item.get("journal", {}).get("source_file_url")
            or (item.get("mapping") or {}).get("source_file_url")
        )
        effective_final_hash = (
            final_payload_hash
            or item.get("journal", {}).get("final_payload_hash")
            or (item.get("mapping") or {}).get("final_payload_hash")
        )
        effective_outside_wiki = (
            source_outside_wiki_verified
            or item.get("journal", {}).get("source_outside_wiki_verified") is True
            or (item.get("mapping") or {}).get("source_outside_wiki_verified") is True
        )
        if not effective_source_url:
            raise BatchError(
                "A source-bearing success requires a verified Feishu file URL."
            )
        if not re.fullmatch(r"[0-9a-fA-F]{64}", effective_final_hash or ""):
            raise BatchError(
                "A source-bearing success requires the final linked payload hash."
            )
        if not effective_outside_wiki:
            raise BatchError(
                "A source-bearing success requires proof that the file is outside Wiki."
            )
    if outcome not in {
        "success",
        "transient-failure",
        "collision",
        "conflict",
        "failed",
    }:
        raise BatchError("Unsupported item outcome.")

    journal = item.setdefault("journal", {})
    item["attempts"] = int(item.get("attempts", 0)) + 1
    if node_token:
        journal["node_token"] = node_token
    if source_file_url:
        journal["source_file_url"] = source_file_url
    if source_outside_wiki_verified:
        journal["source_outside_wiki_verified"] = True
    if final_payload_hash:
        journal["final_payload_hash"] = final_payload_hash.lower()
    if obj_token:
        journal["obj_token"] = obj_token
    if feishu_url:
        journal["feishu_url"] = feishu_url
    if last_synced_remote_hash:
        journal["last_synced_remote_hash"] = last_synced_remote_hash.lower()
    if reason:
        journal["reason"] = reason
    journal["verified"] = bool(verified)
    item["state"] = "retryable" if outcome == "transient-failure" else outcome

    counts = {
        "success": 0,
        "collision": 0,
        "conflict": 0,
        "failed": 0,
        "retryable": 0,
    }
    for candidate in items:
        state = candidate.get("state")
        if state in counts:
            counts[state] += 1
    updated["summary"].update(counts)
    updated["state"] = (
        "completed"
        if all(item.get("state") in TERMINAL_ITEM_STATES for item in items)
        else "executing"
    )
    return updated, False


def find_collection_log(vault: Path, source_hash: str) -> Path:
    root = vault / ".kb/logs/收录"
    matches = []
    if root.is_dir() and source_hash:
        for path in root.glob("*.md"):
            try:
                if source_hash in path.read_text(encoding="utf-8-sig"):
                    matches.append(path)
            except (OSError, UnicodeError):
                continue
    if len(matches) != 1:
        raise BatchError(
            "Verified publication requires exactly one matching local collection log."
        )
    return matches[0]


def update_collection_log(
    text: str,
    *,
    synced_at: str,
    space_name: str,
    parent: str,
    feishu_url: str,
    source_file_url: str,
) -> str:
    if feishu_url in text:
        return text
    text = re.sub(
        r"(?m)^- 飞书(?:操作|访问)：.*$",
        "- 飞书操作：已发布并完成回读验证。",
        text,
        count=1,
    )
    date = synced_at[:10]
    lines = [
        "",
        f"## {date} 飞书发布",
        "",
        f"- 正文：已发布到 `{space_name} / {parent}`，并完成回读验证。",
        f"- 飞书文档：`{feishu_url}`",
    ]
    if source_file_url:
        lines.extend(
            [
                "- 原件：已上传至调用者的飞书云盘根目录，未进入 Wiki 节点树。",
                f"- 飞书原件：`{source_file_url}`",
                "- 来源区：原文件名、下载链接与 SHA-256 均已写入并验证。",
            ]
        )
    return text.rstrip() + "\n" + "\n".join(lines) + "\n"


def verified_publication_values(
    item: dict[str, Any],
    journal: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    mapping_snapshot = item.get("mapping") or {}

    def effective(name: str) -> Any:
        return (
            journal.get(name)
            or (existing or {}).get(name)
            or mapping_snapshot.get(name)
        )

    values = {
        "node_token": str(effective("node_token") or ""),
        "obj_token": str(effective("obj_token") or ""),
        "feishu_url": str(effective("feishu_url") or ""),
        "final_payload_hash": str(
            effective("final_payload_hash") or item.get("payload_hash") or ""
        ).lower(),
        "remote_hash": str(
            effective("last_synced_remote_hash") or ""
        ).lower(),
        "source_file_url": str(effective("source_file_url") or ""),
        "source_outside_wiki": (
            journal.get("source_outside_wiki_verified") is True
            or (existing or {}).get("source_outside_wiki_verified") is True
            or mapping_snapshot.get("source_outside_wiki_verified") is True
        ),
    }
    if not (
        values["node_token"]
        and values["obj_token"]
        and values["feishu_url"]
    ):
        raise BatchError(
            "Local finalization requires node token, object token, and Feishu URL."
        )
    if not re.fullmatch(r"[0-9a-f]{64}", values["final_payload_hash"]):
        raise BatchError("Local finalization requires a valid final payload hash.")
    if not re.fullmatch(r"[0-9a-f]{64}", values["remote_hash"]):
        raise BatchError(
            "Local finalization requires the verified remote content hash."
        )
    if item.get("source_relative") and not (
        values["source_file_url"] and values["source_outside_wiki"]
    ):
        raise BatchError(
            "Source-bearing local finalization requires a verified Drive file outside Wiki."
        )
    return values


def published_note_fields(
    item: dict[str, Any],
    values: dict[str, Any],
    *,
    space_name: str,
    space_id: str,
    timestamp: str,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "status": "published",
        "updated": timestamp[:10],
        "publish_status": "published",
        "publish_to_feishu": True,
        "feishu_writer": "lark-cli",
        "feishu_space_name": space_name,
        "feishu_space_id": space_id,
        "feishu_parent_node_name": item["parent_node_name"],
        "feishu_parent_node_token": item["parent_node_token"],
        "feishu_node_token": values["node_token"],
        "feishu_obj_token": values["obj_token"],
        "feishu_url": values["feishu_url"],
        "feishu_sync_hash": values["final_payload_hash"],
        "feishu_base_payload_hash": item["payload_hash"],
        "feishu_final_payload_hash": values["final_payload_hash"],
        "feishu_last_synced_remote_hash": values["remote_hash"],
        "feishu_last_synced_at": timestamp,
    }
    if values["source_file_url"]:
        fields["source_feishu_file_url"] = values["source_file_url"]
    return fields


def upsert_document_record(
    documents: dict[str, Any],
    item: dict[str, Any],
    existing: dict[str, Any] | None,
    values: dict[str, Any],
    *,
    space_name: str,
    space_id: str,
    source_hash: str,
    timestamp: str,
) -> None:
    record = {
        "local_path": item["note_relative"],
        "local_source_hash": source_hash,
        "writer": "lark-cli",
        "space_name": space_name,
        "space_id": space_id,
        "parent_node_name": item["parent_node_name"],
        "parent_node_token": item["parent_node_token"],
        "node_token": values["node_token"],
        "obj_token": values["obj_token"],
        "obj_type": (existing or {}).get("obj_type", "docx"),
        "feishu_url": values["feishu_url"],
        "published_at": (existing or {}).get("published_at", timestamp),
        "last_synced_at": timestamp,
        "last_parent_verified_at": timestamp,
        "last_synced_hash": source_hash,
        "base_payload_hash": item["payload_hash"],
        "final_payload_hash": values["final_payload_hash"],
        "last_synced_payload_hash": values["final_payload_hash"],
        "last_synced_remote_hash": values["remote_hash"],
        "source_file_url": values["source_file_url"],
        "source_hash": source_hash,
        "source_outside_wiki_verified": values["source_outside_wiki"],
        "sync_status": "success",
        "verified_parent": True,
        "verified_content": True,
    }
    documents["version"] = 2
    documents["documents"] = [
        candidate
        for candidate in documents.get("documents", [])
        if not (
            isinstance(candidate, dict)
            and str(candidate.get("local_path", "")).replace("\\", "/")
            == item["note_relative"]
        )
    ] + [record]


def finalize_local_success(
    vault: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    item_id: str,
    *,
    synced_at: str | None = None,
) -> dict[str, Any]:
    """Atomically converge note, mapping, log, queue, and batch after verification."""
    vault = vault.resolve()
    manifest_path = resolve_in_vault(vault, manifest_path)
    item = next(
        (
            candidate
            for candidate in manifest.get("items", [])
            if candidate.get("item_id") == item_id
        ),
        None,
    )
    if not item:
        raise BatchError("Item does not belong to this batch.")
    journal = item.get("journal", {})
    if item.get("state") != "success" or journal.get("verified") is not True:
        raise BatchError("Local finalization requires a verified success.")

    documents_path = vault / ".kb/mappings/feishu_documents.json"
    documents = load_json(documents_path, {"version": 2, "documents": []})
    note = resolve_in_vault(vault, item["note_relative"])
    existing = resolve_document_mapping(vault, note, documents)
    values = verified_publication_values(item, journal, existing)

    nodes = load_json(vault / ".kb/mappings/feishu_nodes.json")
    space_name = str(nodes.get("space_name", "")).strip()
    space_id = str(nodes.get("space_id", "")).strip()
    if not space_name or not space_id:
        raise BatchError("Verified Feishu space mapping is incomplete.")
    if (
        space_name != manifest.get("space_name")
        or space_id != manifest.get("space_id")
    ):
        raise BatchError("Verified Feishu space mapping no longer matches this batch.")

    timestamp = synced_at or datetime.now().astimezone().isoformat()
    note_text = note.read_text(encoding="utf-8-sig")
    updated_note = update_frontmatter(
        note_text,
        published_note_fields(
            item,
            values,
            space_name=space_name,
            space_id=space_id,
            timestamp=timestamp,
        ),
    )

    source_hash = str(item.get("source_hash") or "").lower()
    log_path = find_collection_log(vault, source_hash)
    updated_log = update_collection_log(
        log_path.read_text(encoding="utf-8-sig"),
        synced_at=timestamp,
        space_name=space_name,
        parent=item["parent_node_name"],
        feishu_url=values["feishu_url"],
        source_file_url=values["source_file_url"],
    )

    upsert_document_record(
        documents,
        item,
        existing,
        values,
        space_name=space_name,
        space_id=space_id,
        source_hash=source_hash,
        timestamp=timestamp,
    )

    queue_path = vault / ".kb/state/publish_queue.json"
    queue = load_json(queue_path, {"version": 2, "items": []})
    queue["items"] = [
        candidate
        for candidate in queue.get("items", [])
        if str(candidate.get("local_path", "")).replace("\\", "/")
        != item["note_relative"]
    ]

    atomic_write_many(
        {
            note: updated_note.encode("utf-8"),
            log_path: updated_log.encode("utf-8"),
            documents_path: json_bytes(documents),
            queue_path: json_bytes(queue),
            manifest_path: json_bytes(manifest),
        }
    )
    return {
        "note": item["note_relative"],
        "log": relative_posix(log_path, vault),
        "queue_removed": True,
        "mapping_upserted": True,
    }


def print_error(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=".", help="Vault root; defaults to cwd")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("preview")

    confirm = subparsers.add_parser("confirm")
    confirm.add_argument("--batch", required=True)
    confirm.add_argument("--phrase", required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--batch", required=True)
    record.add_argument("--item-id", required=True)
    record.add_argument(
        "--outcome",
        required=True,
        choices=(
            "success",
            "transient-failure",
            "collision",
            "conflict",
            "failed",
        ),
    )
    record.add_argument("--verified", action="store_true")
    record.add_argument("--node-token", default="")
    record.add_argument("--source-file-url", default="")
    record.add_argument("--source-outside-wiki-verified", action="store_true")
    record.add_argument("--final-payload-hash", default="")
    record.add_argument("--obj-token", default="")
    record.add_argument("--feishu-url", default="")
    record.add_argument("--last-synced-remote-hash", default="")
    record.add_argument("--synced-at", default="")
    record.add_argument("--reason", default="")

    args = parser.parse_args()
    vault = Path(args.vault).resolve()
    try:
        from runtime_access import authorize

        authorize(vault, "publish")
        if args.command == "preview":
            result = create_preview(vault)
        elif args.command == "confirm":
            result = confirm_batch(vault, Path(args.batch), args.phrase)
        else:
            manifest_path = resolve_in_vault(vault, args.batch)
            manifest = load_json(manifest_path)
            updated, idempotent = record_outcome(
                manifest,
                args.item_id,
                args.outcome,
                verified=args.verified,
                node_token=args.node_token,
                source_file_url=args.source_file_url,
                source_outside_wiki_verified=args.source_outside_wiki_verified,
                final_payload_hash=args.final_payload_hash,
                obj_token=args.obj_token,
                feishu_url=args.feishu_url,
                last_synced_remote_hash=args.last_synced_remote_hash,
                reason=args.reason,
            )
            local_finalized = None
            if args.outcome == "success":
                local_finalized = finalize_local_success(
                    vault,
                    manifest_path,
                    updated,
                    args.item_id,
                    synced_at=args.synced_at or None,
                )
            elif not idempotent:
                atomic_write_json(manifest_path, updated)
            result = {
                "ok": True,
                "batch_id": updated["batch_id"],
                "state": updated["state"],
                "item_id": args.item_id,
                "outcome": args.outcome,
                "idempotent": idempotent,
                "summary": updated["summary"],
                "local_finalized": local_finalized,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (BatchError, OSError, UnicodeError) as exc:
        print_error(str(exc))
        raise SystemExit(2)


if __name__ == "__main__":
    main()

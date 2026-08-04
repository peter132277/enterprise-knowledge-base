#!/usr/bin/env python3
"""Audit and safely reconcile the local enterprise publication state."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


FRONTMATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|$)", re.DOTALL)


class StateError(RuntimeError):
    pass


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"Invalid JSON file: {path}") from exc


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def scalar(frontmatter: str, field: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(field)}:\s*(.*?)\s*$",
        frontmatter,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def read_note(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise StateError(f"Unreadable note: {path}") from exc
    match = FRONTMATTER.match(text)
    if not match:
        raise StateError(f"Missing Frontmatter: {path}")
    return text, match.group("body")


def set_frontmatter_field(text: str, field: str, value: str) -> str:
    match = FRONTMATTER.match(text)
    if not match:
        raise StateError("Cannot update a note without Frontmatter.")
    body = match.group("body")
    replacement = f"{field}: {value}"
    pattern = re.compile(rf"(?m)^{re.escape(field)}:\s*.*$")
    if pattern.search(body):
        body = pattern.sub(replacement, body, count=1)
    else:
        body = body.rstrip() + "\n" + replacement
    return text[: match.start("body")] + body + text[match.end("body") :]


def relative(path: Path, vault: Path) -> str:
    return path.relative_to(vault).as_posix()


def mapping_index(documents: list[Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            errors.append({"path": "", "reason": "mapping_not_object"})
            continue
        path = str(document.get("local_path", "")).replace("\\", "/")
        if not path:
            errors.append({"path": "", "reason": "mapping_missing_local_path"})
            continue
        grouped.setdefault(path, []).append(document)
    result: dict[str, dict[str, Any]] = {}
    for path, matches in grouped.items():
        if len(matches) > 1:
            errors.append({"path": path, "reason": "duplicate_mapping"})
        else:
            result[path] = matches[0]
    return result, errors


def scan_publication_notes(
    vault: Path,
    queue_paths: dict[str, int],
    mapping_by_path: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
    repairable: list[dict[str, str]],
    counts: dict[str, int],
) -> dict[str, dict[str, str]]:
    notes: dict[str, dict[str, str]] = {}
    knowledge_root = vault / "20_知识"
    if knowledge_root.is_dir():
        for path in sorted(knowledge_root.rglob("*.md")):
            counts["knowledge_notes"] += 1
            rel = relative(path, vault)
            try:
                _, frontmatter = read_note(path)
            except StateError as exc:
                errors.append({"path": rel, "reason": str(exc)})
                continue
            fields = {
                name: scalar(frontmatter, name)
                for name in (
                    "type",
                    "scope",
                    "status",
                    "review_status",
                    "publish_status",
                    "publish_to_feishu",
                    "sensitivity",
                    "feishu_url",
                )
            }
            notes[rel] = fields
            controlled = (
                rel.startswith("20_知识/企业/")
                and fields["scope"] == "enterprise"
                and fields["type"] == "source-note"
            )
            if not controlled:
                counts["not_applicable"] += 1
                continue

            counts["publication_source_notes"] += 1
            status = fields["status"]
            publish_status = fields["publish_status"]
            mapping = mapping_by_path.get(rel)
            in_queue = queue_paths.get(rel, 0)

            if status == "published" or publish_status == "published":
                counts["published"] += 1
                if status != "published":
                    errors.append({"path": rel, "reason": "published_status_mismatch"})
                if publish_status != "published":
                    errors.append(
                        {"path": rel, "reason": "published_publish_status_mismatch"}
                    )
                    if (
                        status == "published"
                        and fields["publish_to_feishu"].lower() == "true"
                        and mapping
                        and mapping.get("sync_status") == "success"
                        and mapping.get("verified_content") is True
                        and mapping.get("feishu_url")
                    ):
                        repairable.append(
                            {"path": rel, "action": "set_publish_status_published"}
                        )
                if fields["publish_to_feishu"].lower() != "true":
                    errors.append({"path": rel, "reason": "published_flag_false"})
                if in_queue:
                    errors.append({"path": rel, "reason": "published_still_queued"})
                    repairable.append({"path": rel, "action": "remove_from_queue"})
                if not mapping:
                    errors.append({"path": rel, "reason": "published_mapping_missing"})
                else:
                    if mapping.get("sync_status") != "success":
                        errors.append({"path": rel, "reason": "mapping_not_success"})
                    if mapping.get("verified_content") is not True:
                        errors.append({"path": rel, "reason": "mapping_not_verified"})
                    if (
                        fields["feishu_url"]
                        and mapping.get("feishu_url") != fields["feishu_url"]
                    ):
                        errors.append({"path": rel, "reason": "feishu_url_mismatch"})
            elif status == "ready-to-publish" and publish_status == "pending":
                counts["pending"] += 1
                if in_queue != 1:
                    errors.append(
                        {
                            "path": rel,
                            "reason": (
                                "pending_queue_missing"
                                if in_queue == 0
                                else "pending_queue_duplicate"
                            ),
                        }
                    )
            else:
                counts["blocked"] += 1
    return notes


def audit_queue_paths(
    queue_paths: dict[str, int],
    notes: dict[str, dict[str, str]],
    errors: list[dict[str, Any]],
) -> None:
    for path, count in sorted(queue_paths.items()):
        fields = notes.get(path)
        if count > 1:
            errors.append({"path": path, "reason": "duplicate_queue_item"})
        if not fields:
            errors.append({"path": path, "reason": "queue_note_missing"})
        elif not (
            fields["type"] == "source-note"
            and fields["scope"] == "enterprise"
            and
            fields["status"] == "ready-to-publish"
            and fields["publish_status"] == "pending"
        ):
            errors.append({"path": path, "reason": "queue_note_not_pending"})


def audit_mapping_paths(
    mapping_by_path: dict[str, dict[str, Any]],
    notes: dict[str, dict[str, str]],
    errors: list[dict[str, Any]],
) -> None:
    for path in sorted(mapping_by_path):
        if path.startswith("20_知识/个人/"):
            errors.append({"path": path, "reason": "personal_note_must_not_be_mapped"})
        elif path.startswith("20_知识/") and path not in notes:
            errors.append({"path": path, "reason": "mapped_note_missing"})


def audit_completed_batches(
    vault: Path,
    notes: dict[str, dict[str, str]],
    errors: list[dict[str, Any]],
) -> None:
    for batch_path in sorted(
        (vault / ".kb/state").glob("publish_batch_kbp-*.json")
    ):
        batch = load_json(batch_path, {})
        for item in batch.get("items", []):
            if not isinstance(item, dict) or item.get("state") != "success":
                continue
            path = str(item.get("note_relative", "")).replace("\\", "/")
            fields = notes.get(path)
            if not fields or fields.get("status") != "published":
                errors.append(
                    {
                        "path": path,
                        "reason": "successful_batch_not_locally_finalized",
                        "batch": batch.get("batch_id", ""),
                    }
                )


def unique_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique


def audit(vault: Path) -> dict[str, Any]:
    vault = vault.resolve()
    queue = load_json(
        vault / ".kb/state/publish_queue.json",
        {"version": 2, "items": []},
    )
    mappings = load_json(
        vault / ".kb/mappings/feishu_documents.json",
        {"version": 2, "documents": []},
    )
    queue_items = [item for item in queue.get("items", []) if isinstance(item, dict)]
    queue_paths: dict[str, int] = {}
    for item in queue_items:
        path = str(item.get("local_path", "")).replace("\\", "/")
        queue_paths[path] = queue_paths.get(path, 0) + 1

    mapping_by_path, errors = mapping_index(mappings.get("documents", []))
    repairable: list[dict[str, str]] = []
    counts = {
        "knowledge_notes": 0,
        "publication_source_notes": 0,
        "pending": 0,
        "published": 0,
        "blocked": 0,
        "not_applicable": 0,
    }
    notes = scan_publication_notes(
        vault,
        queue_paths,
        mapping_by_path,
        errors,
        repairable,
        counts,
    )
    audit_queue_paths(queue_paths, notes, errors)
    audit_mapping_paths(mapping_by_path, notes, errors)
    audit_completed_batches(vault, notes, errors)
    unique_errors = unique_records(errors)
    unique_repairs = unique_records(repairable)

    return {
        "ok": not unique_errors,
        **counts,
        "queue_records": len(queue_items),
        "mapping_records": len(mappings.get("documents", [])),
        "errors": unique_errors,
        "repairable": unique_repairs,
    }


def atomic_write_many(changes: dict[Path, bytes]) -> None:
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


def reconcile(vault: Path, *, write: bool = False) -> dict[str, Any]:
    vault = vault.resolve()
    before = audit(vault)
    repairs = before["repairable"]
    if not repairs:
        return {
            "ok": before["ok"],
            "write": write,
            "changed": 0,
            "backup": "",
            "repairable": [],
            "audit": before,
        }

    queue_path = vault / ".kb/state/publish_queue.json"
    queue = load_json(queue_path, {"version": 2, "items": []})
    changes: dict[Path, bytes] = {}
    changed_paths: set[Path] = set()
    remove_from_queue = {
        repair["path"]
        for repair in repairs
        if repair["action"] == "remove_from_queue"
    }
    for repair in repairs:
        if repair["action"] != "set_publish_status_published":
            continue
        path = vault / Path(repair["path"])
        text, _ = read_note(path)
        updated = set_frontmatter_field(text, "publish_status", "published")
        changes[path] = updated.encode("utf-8")
        changed_paths.add(path)

    if remove_from_queue:
        queue["items"] = [
            item
            for item in queue.get("items", [])
            if str(item.get("local_path", "")).replace("\\", "/")
            not in remove_from_queue
        ]
        changes[queue_path] = json_bytes(queue)
        changed_paths.add(queue_path)

    backup_relative = ""
    if write:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = vault / ".kb/backups" / f"publish-state-reconcile-{stamp}"
        for path in sorted(changed_paths):
            target = backup / path.relative_to(vault)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        atomic_write_many(changes)
        backup_relative = relative(backup, vault)

    after = audit(vault) if write else before
    return {
        "ok": after["ok"] if write else True,
        "write": write,
        "changed": len(changes) if write else 0,
        "backup": backup_relative,
        "repairable": repairs,
        "audit": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path("."))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit")
    repair = subparsers.add_parser("repair")
    repair.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            audit(args.vault)
            if args.command == "audit"
            else reconcile(args.vault, write=args.write)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    except (StateError, OSError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

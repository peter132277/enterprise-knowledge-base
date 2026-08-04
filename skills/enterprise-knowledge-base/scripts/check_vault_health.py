#!/usr/bin/env python3
"""Check minimal layout, source integrity, state paths and visible Wiki links."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


VISIBLE_DIRS = {"00_收件箱", "10_来源", "20_知识", "30_导航", "90_归档"}
CONTENT_DIRS = ("10_来源", "20_知识", "30_导航")
EXTERNAL_CORPUS_PREFIXES = ("10_来源/提取/网页/",)
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
COLLECTION_HEADING = re.compile(r"^##\s+(\d{3})｜(.+?)\s*$", re.MULTILINE)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_markdown_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def check_web_corpora(vault: Path) -> dict[str, Any]:
    state_path = vault / ".kb" / "state" / "web_corpora.json"
    if not state_path.is_file():
        return {
            "count": 0,
            "files": 0,
            "markdown": 0,
            "attachments": 0,
            "empty_markdown": 0,
            "errors": [],
        }
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "count": 0,
            "files": 0,
            "markdown": 0,
            "attachments": 0,
            "empty_markdown": 0,
            "errors": [{"corpus": "", "field": "state", "reason": str(exc)}],
        }

    items = state.get("items", [])
    errors: list[dict[str, str]] = []
    total_files = 0
    total_markdown = 0
    total_attachments = 0
    empty_markdown = 0
    if not isinstance(items, list):
        items = []
        errors.append({"corpus": "", "field": "items", "reason": "not-list"})
    for item in items:
        if not isinstance(item, dict):
            errors.append({"corpus": "", "field": "item", "reason": "not-object"})
            continue
        corpus = str(item.get("corpus_root", ""))
        root = vault / Path(corpus)
        archive = vault / Path(str(item.get("archive", "")))
        landing = vault / Path(str(item.get("landing", "")))
        entries = item.get("entries", [])
        if not corpus or not root.is_dir():
            errors.append({"corpus": corpus, "field": "corpus_root", "reason": "missing"})
        if not archive.is_file():
            errors.append({"corpus": corpus, "field": "archive", "reason": "missing"})
        elif sha256(archive) != str(item.get("archive_sha256", "")):
            errors.append({"corpus": corpus, "field": "archive", "reason": "hash"})
        if not landing.is_file():
            errors.append({"corpus": corpus, "field": "landing", "reason": "missing"})
        if not isinstance(entries, list):
            errors.append({"corpus": corpus, "field": "entries", "reason": "not-list"})
            continue

        tree = hashlib.sha256()
        observed_markdown = 0
        observed_attachments = 0
        registered_paths: set[str] = set()
        for entry in sorted(
            (entry for entry in entries if isinstance(entry, dict)),
            key=lambda value: str(value.get("path", "")),
        ):
            relative = str(entry.get("path", "")).replace("\\", "/").strip("/")
            if relative:
                registered_paths.add(relative)
            expected_hash = str(entry.get("sha256", ""))
            path = root / Path(relative)
            if not relative or ".." in Path(relative).parts or not path.is_file():
                errors.append(
                    {"corpus": corpus, "field": relative or "entry", "reason": "missing"}
                )
                continue
            actual_hash = sha256(path)
            accepted_hash = expected_hash
            if actual_hash != expected_hash:
                normalized_expected = str(entry.get("normalized_sha256", ""))
                adapted_expected = str(entry.get("adapted_sha256", ""))
                adapted_normalized = str(entry.get("adapted_normalized_sha256", ""))
                if actual_hash == adapted_expected:
                    pass
                elif (
                    path.suffix.lower() in {".md", ".markdown"}
                    and adapted_normalized
                    and normalized_markdown_sha256(path) == adapted_normalized
                ):
                    pass
                elif (
                    path.suffix.lower() not in {".md", ".markdown"}
                    or not normalized_expected
                    or normalized_markdown_sha256(path) != normalized_expected
                ):
                    errors.append({"corpus": corpus, "field": relative, "reason": "hash"})
                    accepted_hash = actual_hash
            tree.update(relative.encode("utf-8"))
            tree.update(b"\0")
            try:
                tree.update(bytes.fromhex(accepted_hash))
            except ValueError:
                errors.append({"corpus": corpus, "field": relative, "reason": "invalid-hash"})
            tree.update(b"\0")
            if path.suffix.lower() in {".md", ".markdown"}:
                observed_markdown += 1
                if path.stat().st_size == 0:
                    empty_markdown += 1
                    errors.append({"corpus": corpus, "field": relative, "reason": "empty"})
            else:
                observed_attachments += 1

        if root.is_dir():
            actual_paths = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            }
            for relative in sorted(actual_paths - registered_paths):
                path = root / Path(relative)
                reason = "untracked-file"
                if path.suffix.lower() in {".md", ".markdown"} and path.stat().st_size == 0:
                    empty_markdown += 1
                    reason = "untracked-empty-markdown"
                errors.append({"corpus": corpus, "field": relative, "reason": reason})

        observed_files = observed_markdown + observed_attachments
        total_files += observed_files
        total_markdown += observed_markdown
        total_attachments += observed_attachments
        expected_counts = {
            "file_count": observed_files,
            "markdown_count": observed_markdown,
            "attachment_count": observed_attachments,
        }
        for field, observed in expected_counts.items():
            if item.get(field) != observed:
                errors.append({"corpus": corpus, "field": field, "reason": "count"})
        if tree.hexdigest() != str(item.get("content_tree_sha256", "")):
            errors.append({"corpus": corpus, "field": "content_tree_sha256", "reason": "hash"})

    return {
        "count": len(items),
        "files": total_files,
        "markdown": total_markdown,
        "attachments": total_attachments,
        "empty_markdown": empty_markdown,
        "errors": errors,
    }


def check_document_collections(vault: Path) -> dict[str, Any]:
    state_path = vault / ".kb" / "state" / "document_collections.json"
    empty = {"count": 0, "chunks": 0, "items": 0, "errors": []}
    if not state_path.is_file():
        return empty
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            **empty,
            "errors": [{"collection": "", "field": "state", "reason": str(exc)}],
        }

    records = state.get("items", [])
    if not isinstance(records, list):
        return {
            **empty,
            "errors": [
                {"collection": "", "field": "items", "reason": "not-list"}
            ],
        }

    errors: list[dict[str, str]] = []
    observed_chunks = 0
    observed_items = 0
    for record in records:
        if not isinstance(record, dict):
            errors.append(
                {"collection": "", "field": "item", "reason": "not-object"}
            )
            continue
        collection = str(record.get("source_sha256", ""))
        paths_and_hashes = (
            ("evidence", "evidence_sha256"),
            ("landing", "landing_sha256"),
            ("index", "index_sha256"),
        )
        for field, hash_field in paths_and_hashes:
            relative = str(record.get(field, ""))
            path = vault / Path(relative)
            if not relative or ".." in Path(relative).parts or not path.is_file():
                errors.append(
                    {"collection": collection, "field": field, "reason": "missing"}
                )
            elif sha256(path) != str(record.get(hash_field, "")):
                errors.append(
                    {"collection": collection, "field": field, "reason": "hash"}
                )

        chunk_root = str(record.get("chunk_root", "")).replace("\\", "/").strip("/")
        root = vault / Path(chunk_root)
        if (
            not chunk_root.startswith("10_来源/提取/")
            or ".." in Path(chunk_root).parts
            or not root.is_dir()
        ):
            errors.append(
                {
                    "collection": collection,
                    "field": "chunk_root",
                    "reason": "missing",
                }
            )

        chunks = record.get("chunks", [])
        if not isinstance(chunks, list):
            errors.append(
                {"collection": collection, "field": "chunks", "reason": "not-list"}
            )
            continue
        numbers: list[str] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                errors.append(
                    {"collection": collection, "field": "chunk", "reason": "not-object"}
                )
                continue
            relative = str(chunk.get("path", "")).replace("\\", "/").strip("/")
            path = vault / Path(relative)
            if (
                not relative.startswith(chunk_root.rstrip("/") + "/")
                or ".." in Path(relative).parts
                or not path.is_file()
            ):
                errors.append(
                    {
                        "collection": collection,
                        "field": relative or "chunk",
                        "reason": "missing",
                    }
                )
                continue
            if sha256(path) != str(chunk.get("sha256", "")):
                errors.append(
                    {"collection": collection, "field": relative, "reason": "hash"}
                )
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                errors.append(
                    {
                        "collection": collection,
                        "field": relative,
                        "reason": str(exc),
                    }
                )
                continue
            chunk_numbers = [
                match.group(1) for match in COLLECTION_HEADING.finditer(text)
            ]
            numbers.extend(chunk_numbers)
            if (
                len(chunk_numbers) != chunk.get("item_count")
                or not chunk_numbers
                or chunk_numbers[0] != str(chunk.get("start", ""))
                or chunk_numbers[-1] != str(chunk.get("end", ""))
            ):
                errors.append(
                    {
                        "collection": collection,
                        "field": relative,
                        "reason": "item-range",
                    }
                )

        expected_count = record.get("native_unit_count")
        expected_numbers = (
            [f"{value:03d}" for value in range(1, expected_count + 1)]
            if isinstance(expected_count, int) and expected_count > 0
            else []
        )
        if numbers != expected_numbers:
            errors.append(
                {
                    "collection": collection,
                    "field": "native_unit_count",
                    "reason": "sequence",
                }
            )
        if record.get("chunk_count") != len(chunks):
            errors.append(
                {
                    "collection": collection,
                    "field": "chunk_count",
                    "reason": "count",
                }
            )
        observed_chunks += len(chunks)
        observed_items += len(numbers)

    return {
        "count": len(records),
        "chunks": observed_chunks,
        "items": observed_items,
        "errors": errors,
    }


def check_publish_state(vault: Path) -> dict[str, Any]:
    script = Path(__file__).resolve().with_name("publish_state.py")
    if not script.is_file():
        return {
            "ok": False,
            "errors": [{"path": "", "reason": "publish_state_script_missing"}],
        }
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), "--vault", str(vault), "audit"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "errors": [{"path": "", "reason": "publish_state_invalid_output"}],
        }
    if result.returncode not in {0, 2}:
        return {
            "ok": False,
            "errors": [
                {
                    "path": "",
                    "reason": payload.get("error", "publish_state_failed"),
                }
            ],
        }
    return payload


def resolve_link(vault: Path, files: list[Path], target: str) -> list[Path]:
    target = target.split("|", 1)[0].split("#", 1)[0].strip()
    if not target:
        return []
    if "/" in target or "\\" in target:
        candidate = vault / Path(target.replace("\\", "/"))
        if candidate.is_file():
            return [candidate]
        if not candidate.suffix and candidate.with_suffix(".md").is_file():
            return [candidate.with_suffix(".md")]
        return []
    if Path(target).suffix:
        return [path for path in files if path.name == target]
    return [path for path in files if path.stem == target]


def check(vault: Path) -> dict[str, Any]:
    vault = vault.resolve()
    visible = {path.name for path in vault.iterdir() if path.is_dir() and not path.name.startswith(".")}
    missing_visible = sorted(VISIBLE_DIRS - visible)
    extra_visible = sorted(visible - VISIBLE_DIRS)

    all_files: list[Path] = []
    for directory in CONTENT_DIRS:
        root = vault / directory
        if root.is_dir():
            all_files.extend(path for path in root.rglob("*") if path.is_file())
    markdown = [path for path in all_files if path.suffix.lower() == ".md"]
    empty_markdown_files = sorted(
        path.relative_to(vault).as_posix()
        for path in markdown
        if path.stat().st_size == 0
    )
    incoming = {path.resolve(): 0 for path in markdown}
    broken: list[dict[str, str]] = []
    ambiguous: list[dict[str, Any]] = []
    link_count = 0
    skipped_external_corpus_files = 0
    skipped_external_corpus_links = 0
    for note in markdown:
        text = note.read_text(encoding="utf-8-sig")
        relative_note = note.relative_to(vault).as_posix()
        if any(relative_note.startswith(prefix) for prefix in EXTERNAL_CORPUS_PREFIXES):
            skipped_external_corpus_files += 1
            skipped_external_corpus_links += len(WIKI_LINK.findall(text))
            continue
        for match in WIKI_LINK.finditer(text):
            link_count += 1
            target = match.group(1)
            matches = resolve_link(vault, all_files, target)
            if len(matches) == 1:
                resolved = matches[0].resolve()
                if resolved in incoming:
                    incoming[resolved] += 1
            elif not matches:
                broken.append({"source": note.relative_to(vault).as_posix(), "target": target})
            else:
                ambiguous.append(
                    {
                        "source": note.relative_to(vault).as_posix(),
                        "target": target,
                        "matches": len(matches),
                    }
                )

    knowledge_root = (vault / "20_知识").resolve()
    orphan_notes = sorted(
        path.relative_to(vault).as_posix()
        for path in markdown
        if path.resolve().is_relative_to(knowledge_root) and incoming[path.resolve()] == 0
    )

    state_path = vault / ".kb" / "state" / "processed_files.json"
    state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.is_file() else {}
    state_errors: list[dict[str, str]] = []
    for item in state.get("files", []):
        digest = str(item.get("sha256", "")).lower()
        for field in ("stored_original", "extraction", "source_note"):
            rel = str(item.get(field, ""))
            path = vault / Path(rel)
            if not rel or not path.is_file():
                state_errors.append({"source_hash": digest, "field": field, "reason": "missing"})
        structure_index = str(item.get("structure_index", ""))
        if structure_index and not (vault / Path(structure_index)).is_file():
            state_errors.append(
                {
                    "source_hash": digest,
                    "field": "structure_index",
                    "reason": "missing",
                }
            )
        evidence_bundle = str(item.get("evidence_bundle", ""))
        if evidence_bundle and not (vault / Path(evidence_bundle)).is_file():
            state_errors.append(
                {
                    "source_hash": digest,
                    "field": "evidence_bundle",
                    "reason": "missing",
                }
            )
        chunk_root = str(item.get("chunk_root", ""))
        if chunk_root and not (vault / Path(chunk_root)).is_dir():
            state_errors.append(
                {
                    "source_hash": digest,
                    "field": "chunk_root",
                    "reason": "missing",
                }
            )
        original = vault / Path(str(item.get("stored_original", "")))
        if original.is_file() and sha256(original) != digest:
            state_errors.append({"source_hash": digest, "field": "stored_original", "reason": "hash"})

    queue_path = vault / ".kb" / "state" / "publish_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8-sig")) if queue_path.is_file() else {}
    queue_errors = [
        item.get("local_path", "")
        for item in queue.get("items", [])
        if not (vault / Path(str(item.get("local_path", "")))).is_file()
    ]
    web_corpora = check_web_corpora(vault)
    document_collections = check_document_collections(vault)
    publish_state = check_publish_state(vault)

    ok = not any(
        (
            missing_visible,
            extra_visible,
            empty_markdown_files,
            broken,
            ambiguous,
            orphan_notes,
            state_errors,
            queue_errors,
            web_corpora["errors"],
            document_collections["errors"],
            publish_state["errors"],
        )
    )
    return {
        "ok": ok,
        "visible_directories": sorted(visible),
        "missing_visible_directories": missing_visible,
        "extra_visible_directories": extra_visible,
        "markdown_files": len(markdown),
        "empty_markdown_files": empty_markdown_files,
        "wiki_links": link_count,
        "skipped_external_corpus_files": skipped_external_corpus_files,
        "skipped_external_corpus_links": skipped_external_corpus_links,
        "broken_links": broken,
        "ambiguous_links": ambiguous,
        "orphan_knowledge_notes": orphan_notes,
        "processed_records": len(state.get("files", [])),
        "state_errors": state_errors,
        "pending_records": len(queue.get("items", [])),
        "queue_errors": queue_errors,
        "publication_source_notes": publish_state.get(
            "publication_source_notes", 0
        ),
        "publication_pending": publish_state.get("pending", 0),
        "publication_published": publish_state.get("published", 0),
        "publication_not_applicable": publish_state.get("not_applicable", 0),
        "publish_state_errors": publish_state["errors"],
        "web_corpus_count": web_corpora["count"],
        "web_corpus_files": web_corpora["files"],
        "web_corpus_markdown": web_corpora["markdown"],
        "web_corpus_attachments": web_corpora["attachments"],
        "web_corpus_empty_markdown": web_corpora["empty_markdown"],
        "web_corpus_errors": web_corpora["errors"],
        "document_collection_count": document_collections["count"],
        "document_collection_chunks": document_collections["chunks"],
        "document_collection_items": document_collections["items"],
        "document_collection_errors": document_collections["errors"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args()
    result = check(args.vault)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Split a large structured Markdown extraction into verified Obsidian volumes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


HEADING = re.compile(r"^##\s+(\d{3})｜(.+?)\s*$", re.MULTILINE)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class CollectionError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_relative(value: str, prefixes: tuple[str, ...]) -> str:
    relative = value.replace("\\", "/").strip("/")
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise CollectionError(f"Unsafe relative path: {value}")
    if not any(
        relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    ):
        raise CollectionError(f"Path is outside managed roots: {value}")
    return relative


def parse_entries(text: str, expected_count: int) -> tuple[str, list[dict[str, str]]]:
    matches = list(HEADING.finditer(text))
    if len(matches) != expected_count:
        raise CollectionError(
            f"Entry count mismatch: expected {expected_count}, found {len(matches)}"
        )
    expected_numbers = [f"{value:03d}" for value in range(1, expected_count + 1)]
    actual_numbers = [match.group(1) for match in matches]
    if actual_numbers != expected_numbers:
        raise CollectionError("Entry numbering is not continuous from 001")

    entries: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        number = match.group(1)
        title = match.group(2).strip()
        entries.append(
            {
                "number": number,
                "title": title,
                "heading": f"{number}｜{title}",
                "body": text[match.start() : end].rstrip() + "\n",
            }
        )
    if len({entry["heading"] for entry in entries}) != len(entries):
        raise CollectionError("Duplicate entry headings found")
    return text[: matches[0].start()].rstrip() + "\n", entries


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def wiki_display(value: str) -> str:
    return value.replace("|", "｜").replace("]", "）")


def wiki_heading(value: str) -> str:
    return value.replace("|", "\\|")


def build_chunk(
    title: str,
    source_sha256: str,
    evidence_sha256: str,
    entries: list[dict[str, str]],
    chunk_number: int,
    chunk_count: int,
) -> str:
    start = entries[0]["number"]
    end = entries[-1]["number"]
    today = datetime.now().astimezone().date().isoformat()
    frontmatter = [
        "---",
        f"title: {yaml_quote(f'{title}｜{start}-{end}')}",
        "type: source-extraction-volume",
        "scope: enterprise",
        "capture_mode: standalone-collection",
        'native_unit_type: "transcript"',
        f"native_unit_count: {len(entries)}",
        f"chunk_number: {chunk_number}",
        f"chunk_count: {chunk_count}",
        f'item_range: "{start}-{end}"',
        f'source_id: "sha256:{source_sha256}"',
        f'evidence_sha256: "{evidence_sha256}"',
        f"updated: {today}",
        "---",
        "",
        f"# {title}｜{start}-{end}",
        "",
        (
            f"> 本卷是完整来源证据的可读分卷，共 {len(entries)} 条；"
            "内容按原文顺序保留，不代表事实、数据或营销承诺已核验。"
        ),
        "",
    ]
    return "\n".join(frontmatter) + "".join(entry["body"] for entry in entries)


def build_landing(
    title: str,
    source_sha256: str,
    evidence_relative: str,
    evidence_sha256: str,
    index_relative: str,
    chunks: list[dict[str, Any]],
    expected_count: int,
) -> str:
    today = datetime.now().astimezone().date().isoformat()
    index_target = Path(index_relative).with_suffix("").as_posix()
    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        "type: source-extraction-landing",
        "scope: enterprise",
        "capture_mode: standalone-collection",
        'native_unit_type: "transcript"',
        f"native_unit_count: {expected_count}",
        f"chunk_count: {len(chunks)}",
        f'source_id: "sha256:{source_sha256}"',
        f'evidence_sha256: "{evidence_sha256}"',
        f"updated: {today}",
        "---",
        "",
        f"# {title}",
        "",
        f"- 条目总数：{expected_count}",
        f"- 活跃分卷：{len(chunks)}",
        f"- 条目索引：[[{index_target}|打开 425 条逐字稿索引]]",
        f"- 完整证据：`{evidence_relative}`（隐藏校验层，不作为日常阅读入口）",
        "",
        "## 分卷",
        "",
    ]
    for chunk in chunks:
        target = Path(str(chunk["path"])).with_suffix("").as_posix()
        lines.append(
            f"- [[{target}|{chunk['start']}-{chunk['end']}]]"
            f"（{chunk['item_count']} 条）"
        )
    lines.extend(
        [
            "",
            "## 使用说明",
            "",
            "- 日常浏览与检索使用本页、条目索引或对应分卷。",
            "- 完整正文只在来源核验、重新分卷或完整性检查时读取。",
            "- 可复用方法继续沉淀在 `20_知识`；不为每条逐字稿机械创建知识笔记。",
            "",
        ]
    )
    return "\n".join(lines)


def build_index(
    title: str,
    source_sha256: str,
    evidence_sha256: str,
    landing_relative: str,
    entries: list[dict[str, str]],
    chunk_paths: dict[str, str],
) -> str:
    today = datetime.now().astimezone().date().isoformat()
    landing_target = Path(landing_relative).with_suffix("").as_posix()
    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        "type: source-structure-index",
        "scope: enterprise",
        "capture_mode: standalone-collection",
        'native_unit_type: "transcript"',
        f"native_unit_count: {len(entries)}",
        f'source_id: "sha256:{source_sha256}"',
        f'evidence_sha256: "{evidence_sha256}"',
        f"updated: {today}",
        "---",
        "",
        f"# {title}",
        "",
        f"- 分卷入口：[[{landing_target}|打开分卷目录]]",
        f"- 条目数量：{len(entries)}",
        "- 每个条目直接链接到对应分卷，不加载完整巨型 Markdown。",
        "",
    ]
    for offset in range(0, len(entries), 50):
        group = entries[offset : offset + 50]
        lines.extend([f"## {group[0]['number']}–{group[-1]['number']}", ""])
        for entry in group:
            target = Path(chunk_paths[entry["number"]]).with_suffix("").as_posix()
            lines.append(
                f"- [[{target}#{wiki_heading(entry['heading'])}|"
                f"{wiki_display(entry['heading'])}]]"
            )
        lines.append("")
    return "\n".join(lines)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "items": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Invalid document collection state: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("items", []), list):
        raise CollectionError("Invalid document collection state shape")
    state["version"] = 1
    return state


def run(args: argparse.Namespace) -> dict[str, Any]:
    vault = args.vault.resolve()
    source_sha256 = args.source_sha256.lower()
    if not HASH_RE.fullmatch(source_sha256):
        raise CollectionError("source_sha256 must be 64 lowercase hex characters")
    if args.expected_count < 1 or args.chunk_size < 1:
        raise CollectionError("expected_count and chunk_size must be positive")

    landing_relative = normalize_relative(args.extraction, ("10_来源/提取",))
    index_relative = normalize_relative(args.index, ("10_来源/提取",))
    chunk_root_relative = normalize_relative(args.chunk_root, ("10_来源/提取",))
    evidence_relative = normalize_relative(args.evidence, (".kb/evidence",))
    landing = vault / Path(landing_relative)
    index_path = vault / Path(index_relative)
    chunk_root = vault / Path(chunk_root_relative)
    evidence = vault / Path(evidence_relative)
    state_path = vault / ".kb" / "state" / "document_collections.json"

    full_source = landing
    if evidence.is_file():
        try:
            parse_entries(landing.read_text(encoding="utf-8-sig"), args.expected_count)
        except (CollectionError, OSError, UnicodeError):
            full_source = evidence
    if not full_source.is_file():
        raise CollectionError(f"Complete extraction not found: {full_source}")
    full_text = full_source.read_text(encoding="utf-8-sig")
    _, entries = parse_entries(full_text, args.expected_count)
    full_data = full_text.encode("utf-8")
    evidence_hash = sha256_bytes(full_data)

    chunk_count = (len(entries) + args.chunk_size - 1) // args.chunk_size
    chunk_files: dict[str, bytes] = {}
    chunk_paths: dict[str, str] = {}
    chunks: list[dict[str, Any]] = []
    for offset in range(0, len(entries), args.chunk_size):
        group = entries[offset : offset + args.chunk_size]
        start = group[0]["number"]
        end = group[-1]["number"]
        relative = f"{chunk_root_relative}/{start}-{end}.md"
        data = build_chunk(
            args.title,
            source_sha256,
            evidence_hash,
            group,
            len(chunks) + 1,
            chunk_count,
        ).encode("utf-8")
        chunk_files[relative] = data
        for entry in group:
            chunk_paths[entry["number"]] = relative
        chunks.append(
            {
                "path": relative,
                "start": start,
                "end": end,
                "item_count": len(group),
                "sha256": sha256_bytes(data),
                "size_bytes": len(data),
            }
        )

    landing_data = build_landing(
        args.title,
        source_sha256,
        evidence_relative,
        evidence_hash,
        index_relative,
        chunks,
        args.expected_count,
    ).encode("utf-8")
    index_data = build_index(
        f"{args.title}｜条目索引",
        source_sha256,
        evidence_hash,
        landing_relative,
        entries,
        chunk_paths,
    ).encode("utf-8")

    record = {
        "source_sha256": source_sha256,
        "title": args.title,
        "capture_mode": "standalone-collection",
        "native_unit_type": args.native_unit_type,
        "native_unit_count": args.expected_count,
        "chunk_size": args.chunk_size,
        "chunk_count": len(chunks),
        "landing": landing_relative,
        "landing_sha256": sha256_bytes(landing_data),
        "index": index_relative,
        "index_sha256": sha256_bytes(index_data),
        "evidence": evidence_relative,
        "evidence_sha256": evidence_hash,
        "chunk_root": chunk_root_relative,
        "chunks": chunks,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    state = load_state(state_path)
    state["items"] = [
        item
        for item in state.get("items", [])
        if not isinstance(item, dict)
        or str(item.get("source_sha256", "")).lower() != source_sha256
    ]
    state["items"].append(record)
    state_data = (
        json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    result: dict[str, Any] = {
        "ok": True,
        "mode": "split-document-collection",
        "write": args.write,
        "source_sha256": source_sha256,
        "evidence": evidence_relative,
        "evidence_sha256": evidence_hash,
        "landing": landing_relative,
        "index": index_relative,
        "chunk_root": chunk_root_relative,
        "chunk_count": len(chunks),
        "item_count": len(entries),
        "chunks": chunks,
    }
    if not args.write:
        return result

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup_root = vault / ".kb" / "backups" / f"document-collection-{stamp}"
    backup_targets = [landing, index_path, state_path]
    if chunk_root.exists():
        backup_targets.append(chunk_root)
    for target in backup_targets:
        if not target.exists():
            continue
        relative = target.relative_to(vault)
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            shutil.copytree(target, destination)
        else:
            shutil.copy2(target, destination)

    try:
        if evidence.exists() and sha256_file(evidence) != evidence_hash:
            raise CollectionError("Existing canonical evidence hash differs")
        if not evidence.exists():
            write_atomic(evidence, full_data)

        staged_root = vault / ".kb" / "temp" / f"document-collection-{stamp}"
        if staged_root.exists():
            raise CollectionError(f"Staging path already exists: {staged_root}")
        for relative, data in chunk_files.items():
            staged = staged_root / Path(relative).relative_to(Path(chunk_root_relative))
            write_atomic(staged, data)
        if chunk_root.exists():
            shutil.rmtree(chunk_root)
        chunk_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_root, chunk_root)

        write_atomic(landing, landing_data)
        write_atomic(index_path, index_data)
        write_atomic(state_path, state_data)
    except Exception:
        for target in (landing, index_path, state_path):
            backup = backup_root / target.relative_to(vault)
            if backup.is_file():
                shutil.copy2(backup, target)
        chunk_backup = backup_root / chunk_root.relative_to(vault)
        if chunk_backup.is_dir():
            if chunk_root.exists():
                shutil.rmtree(chunk_root)
            shutil.copytree(chunk_backup, chunk_root)
        raise

    if sha256_file(evidence) != evidence_hash:
        raise CollectionError("Canonical evidence verification failed")
    if sha256_file(landing) != record["landing_sha256"]:
        raise CollectionError("Landing verification failed")
    if sha256_file(index_path) != record["index_sha256"]:
        raise CollectionError("Index verification failed")
    for chunk in chunks:
        if sha256_file(vault / Path(chunk["path"])) != chunk["sha256"]:
            raise CollectionError(f"Chunk verification failed: {chunk['path']}")
    result["backup"] = backup_root.relative_to(vault).as_posix()
    result["state"] = state_path.relative_to(vault).as_posix()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--extraction", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--chunk-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--native-unit-type", default="item")
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--chunk-size", type=int, default=50)
    parser.add_argument("--write", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except CollectionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

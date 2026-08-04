#!/usr/bin/env python3
"""Expand a captured documentation archive into an Obsidian-browsable source corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


class CorpusError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_markdown_sha256(data: bytes) -> str:
    text = data.decode("utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


def normalize_hash(value: str) -> str:
    candidate = value.lower().removeprefix("sha256:")
    if len(candidate) != 64 or any(ch not in "0123456789abcdef" for ch in candidate):
        raise CorpusError(f"Invalid SHA-256: {value}")
    return candidate


def normalize_relative(value: str, prefix: str, suffix: str | None = None) -> str:
    relative = value.replace("\\", "/").strip("/")
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise CorpusError(f"Unsafe relative path: {value}")
    if relative != prefix and not relative.startswith(prefix.rstrip("/") + "/"):
        raise CorpusError(f"Path must be under {prefix}: {value}")
    if suffix and not relative.lower().endswith(suffix.lower()):
        raise CorpusError(f"Path must end in {suffix}: {value}")
    return relative


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusError(f"Invalid JSON state: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusError(f"JSON state must be an object: {path}")
    return value


def safe_archive_entries(
    archive: zipfile.ZipFile, source_prefix: str
) -> list[tuple[zipfile.ZipInfo, str, bytes, str]]:
    prefix = source_prefix.replace("\\", "/").strip("/")
    prefix_with_slash = prefix + "/"
    entries: list[tuple[zipfile.ZipInfo, str, bytes, str]] = []
    casefolded: dict[str, str] = {}
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or not name.startswith(prefix_with_slash):
            continue
        relative = name[len(prefix_with_slash) :]
        pure = PurePosixPath(relative)
        unix_mode = info.external_attr >> 16
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or ":" in pure.parts[0]
            or stat.S_ISLNK(unix_mode)
        ):
            raise CorpusError(f"Unsafe archive entry: {info.filename}")
        folded = relative.casefold()
        if folded in casefolded:
            raise CorpusError(
                f"Case-insensitive archive collision: {casefolded[folded]} and {relative}"
            )
        casefolded[folded] = relative
        data = archive.read(info)
        if relative.lower().endswith((".md", ".markdown")):
            if not data:
                raise CorpusError(f"Empty Markdown document: {info.filename}")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise CorpusError(f"Invalid UTF-8 Markdown: {info.filename}: {exc}") from exc
            if "\ufffd" in text:
                raise CorpusError(f"Replacement character found in Markdown: {info.filename}")
        entries.append((info, relative, data, sha256_bytes(data)))
    if not entries:
        raise CorpusError(f"No files found under archive prefix: {source_prefix}")
    entries.sort(key=lambda item: item[1])
    return entries


def tree_digest(entries: list[tuple[zipfile.ZipInfo, str, bytes, str]]) -> str:
    digest = hashlib.sha256()
    for _, relative, _, file_hash in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_hash))
        digest.update(b"\0")
    return digest.hexdigest()


def landing_markdown(
    title: str,
    archive_relative: str,
    archive_hash: str,
    target_relative: str,
    source_prefix: str,
    tree_hash: str,
    entries: list[tuple[zipfile.ZipInfo, str, bytes, str]],
) -> str:
    markdown_paths = [
        relative
        for _, relative, _, _ in entries
        if relative.lower().endswith((".md", ".markdown"))
    ]
    attachment_count = len(entries) - len(markdown_paths)
    grouped: dict[str, list[str]] = {}
    for relative in markdown_paths:
        parts = PurePosixPath(relative).parts
        group = parts[0] if len(parts) > 1 else "首页与支持"
        grouped.setdefault(group, []).append(relative)

    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "type: external-corpus-index",
        "scope: personal",
        "status: reference",
        f"updated: {datetime.now().astimezone().date().isoformat()}",
        f"source_archive: {json.dumps(archive_relative, ensure_ascii=False)}",
        f"source_hash: {json.dumps(archive_hash)}",
        f"content_tree_sha256: {json.dumps(tree_hash)}",
        f"corpus_root: {json.dumps(target_relative, ensure_ascii=False)}",
        f"source_prefix: {json.dumps(source_prefix, ensure_ascii=False)}",
        f"document_count: {len(markdown_paths)}",
        f"attachment_count: {attachment_count}",
        "---",
        "",
        f"# {title}",
        "",
        "官方文档已按原目录展开；每篇文档均可独立打开，图片、音频和视频附件保留在同一语料目录中。",
        "",
        "## 快速入口",
        "",
    ]
    root_documents = [
        relative for relative in markdown_paths if len(PurePosixPath(relative).parts) == 1
    ]
    for relative in root_documents:
        target = f"{target_relative}/{PurePosixPath(relative).with_suffix('').as_posix()}"
        lines.append(f"- [[{target}|{PurePosixPath(relative).stem}]]")
    lines.extend(["", "## 全部文档", ""])
    for group in sorted(grouped, key=lambda value: (value == "首页与支持", value.casefold())):
        lines.extend([f"### {group}", ""])
        for relative in grouped[group]:
            target = f"{target_relative}/{PurePosixPath(relative).with_suffix('').as_posix()}"
            display = PurePosixPath(relative).with_suffix("").as_posix()
            if group != "首页与支持" and display.startswith(group + "/"):
                display = display[len(group) + 1 :]
            lines.append(f"- [[{target}|{display}]]")
        lines.append("")
    lines.extend(
        [
            "## 完整性",
            "",
            f"- 文档：{len(markdown_paths)} 篇，空文档：0 篇",
            f"- 附件：{attachment_count} 个",
            f"- 总文件：{len(entries)} 个",
            f"- 内容树 SHA-256：`{tree_hash}`",
            f"- 原始 ZIP：`{archive_relative}`",
            "",
            "> 这些文件是外部官方文档的确定性快照，保留原文和原链接；本页仅提供稳定的本地入口。",
            "",
        ]
    )
    return "\n".join(lines)


def corpus_item_is_current(vault: Path, item: dict[str, Any]) -> bool:
    root = vault / Path(str(item.get("corpus_root", "")))
    landing = vault / Path(str(item.get("landing", "")))
    entries = item.get("entries", [])
    if not root.is_dir() or not landing.is_file() or not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        relative = str(entry.get("path", ""))
        expected = str(entry.get("sha256", ""))
        path = root / Path(relative)
        if not path.is_file():
            return False
        actual = sha256_file(path)
        if actual != expected:
            normalized_expected = str(entry.get("normalized_sha256", ""))
            if (
                path.suffix.lower() not in {".md", ".markdown"}
                or not normalized_expected
                or normalized_markdown_sha256(path.read_bytes()) != normalized_expected
            ):
                return False
    return True


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def expand(args: argparse.Namespace) -> dict[str, Any]:
    vault = args.vault.resolve()
    archive_relative = normalize_relative(args.archive, "10_来源/原件/网页", ".zip")
    target_relative = normalize_relative(args.target, "10_来源/提取/网页")
    landing_relative = normalize_relative(args.landing, "10_来源/提取/网页", ".md")
    archive_path = vault / Path(archive_relative)
    target_path = vault / Path(target_relative)
    landing_path = vault / Path(landing_relative)
    if not archive_path.is_file():
        raise CorpusError(f"Archive not found: {archive_path}")
    if landing_path.resolve().is_relative_to(target_path.resolve()):
        raise CorpusError("Landing page must be outside the corpus directory")

    archive_hash = sha256_file(archive_path)
    expected_archive = normalize_hash(args.archive_sha256)
    if archive_hash != expected_archive:
        raise CorpusError(
            f"Archive hash mismatch: expected {expected_archive}, actual {archive_hash}"
        )

    with zipfile.ZipFile(archive_path) as archive:
        entries = safe_archive_entries(archive, args.source_prefix)
    markdown_count = sum(
        1 for _, relative, _, _ in entries if relative.lower().endswith((".md", ".markdown"))
    )
    tree_hash = tree_digest(entries)
    if args.expected_tree_sha256:
        expected_tree = normalize_hash(args.expected_tree_sha256)
        if tree_hash != expected_tree:
            raise CorpusError(
                f"Content tree hash mismatch: expected {expected_tree}, actual {tree_hash}"
            )

    state_path = vault / ".kb" / "state" / "web_corpora.json"
    state = load_json(state_path, {"version": 1, "items": []})
    items = state.get("items", [])
    if not isinstance(items, list):
        raise CorpusError(f"Invalid web corpus state: {state_path}")
    existing = next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and (
                item.get("archive_sha256") == archive_hash
                or item.get("corpus_root") == target_relative
            )
        ),
        None,
    )
    if existing and corpus_item_is_current(vault, existing):
        return {
            "ok": True,
            "mode": "expand-web-corpus",
            "write": args.write,
            "already_current": True,
            "archive_sha256": archive_hash,
            "content_tree_sha256": tree_hash,
            "corpus_root": target_relative,
            "landing": landing_relative,
            "file_count": len(entries),
            "markdown_count": markdown_count,
            "attachment_count": len(entries) - markdown_count,
        }
    if target_path.exists() and not args.replace:
        raise CorpusError(
            f"Corpus target already exists without a valid manifest; use --replace: {target_path}"
        )

    result = {
        "ok": True,
        "mode": "expand-web-corpus",
        "write": args.write,
        "already_current": False,
        "archive_sha256": archive_hash,
        "content_tree_sha256": tree_hash,
        "corpus_root": target_relative,
        "landing": landing_relative,
        "file_count": len(entries),
        "markdown_count": markdown_count,
        "attachment_count": len(entries) - markdown_count,
        "empty_markdown_count": 0,
    }
    if not args.write:
        return result

    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%dT%H%M%S%z")
    temp_parent = vault / ".kb" / "temp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix="web-corpus-", dir=temp_parent))
    stage_root = stage_parent / "corpus"
    backup_root = vault / ".kb" / "backups" / f"web-corpus-{stamp}"
    moved_existing = False
    installed_target = False
    try:
        stage_root.mkdir()
        for _, relative, data, _ in entries:
            output = stage_root / Path(relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        for _, relative, _, expected in entries:
            actual = sha256_file(stage_root / Path(relative))
            if actual != expected:
                raise CorpusError(f"Staged file hash mismatch: {relative}")

        if target_path.exists():
            backup_target = backup_root / "previous-corpus"
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target_path), str(backup_target))
            moved_existing = True
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage_root), str(target_path))
        installed_target = True

        landing = landing_markdown(
            args.title,
            archive_relative,
            archive_hash,
            target_relative,
            args.source_prefix,
            tree_hash,
            entries,
        )
        if landing_path.exists():
            backup_landing = backup_root / "previous-landing.md"
            backup_landing.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(landing_path, backup_landing)
        landing_path.parent.mkdir(parents=True, exist_ok=True)
        landing_temp = landing_path.with_name(landing_path.name + ".tmp")
        landing_temp.write_text(landing, encoding="utf-8", newline="\n")
        os.replace(landing_temp, landing_path)

        item = {
            "title": args.title,
            "archive": archive_relative,
            "archive_sha256": archive_hash,
            "source_prefix": args.source_prefix,
            "content_tree_sha256": tree_hash,
            "corpus_root": target_relative,
            "landing": landing_relative,
            "file_count": len(entries),
            "markdown_count": markdown_count,
            "attachment_count": len(entries) - markdown_count,
            "expanded_at": now.isoformat(),
            "entries": [
                {
                    "path": relative,
                    "sha256": file_hash,
                    "normalized_sha256": (
                        normalized_markdown_sha256(data)
                        if relative.lower().endswith((".md", ".markdown"))
                        else None
                    ),
                    "size_bytes": len(data),
                }
                for _, relative, data, file_hash in entries
            ],
        }
        new_items = [
            value
            for value in items
            if not (
                isinstance(value, dict)
                and (
                    value.get("archive_sha256") == archive_hash
                    or value.get("corpus_root") == target_relative
                )
            )
        ]
        new_items.append(item)
        if state_path.exists():
            backup_state = backup_root / "web_corpora.json"
            backup_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state_path, backup_state)
        atomic_json_write(state_path, {"version": 1, "items": new_items})
        result["backup_root"] = backup_root.relative_to(vault).as_posix()
        result["state_path"] = state_path.relative_to(vault).as_posix()
        return result
    except Exception:
        if installed_target and target_path.exists():
            shutil.rmtree(target_path)
        backup_target = backup_root / "previous-corpus"
        if moved_existing and backup_target.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_target), str(target_path))
        raise
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--source-prefix", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--landing", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--expected-tree-sha256")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--replace", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(expand(args), ensure_ascii=False, indent=2))
        return 0
    except CorpusError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main())

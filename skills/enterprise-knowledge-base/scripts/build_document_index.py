#!/usr/bin/env python3
"""Build a lightweight Obsidian index for a large structured document extraction."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from kb_core import sha256_bytes, sha256_file


HEADING = re.compile(r"^##\s+(\d{3})｜(.+?)\s*$", re.MULTILINE)


class IndexError(RuntimeError):
    pass


def normalize_relative(value: str, prefix: str) -> str:
    relative = value.replace("\\", "/").strip("/")
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise IndexError(f"Unsafe relative path: {value}")
    if relative != prefix and not relative.startswith(prefix.rstrip("/") + "/"):
        raise IndexError(f"Path must be under {prefix}: {value}")
    return relative


def build_index(
    title: str,
    source_hash: str,
    extraction_relative: str,
    extraction_hash: str,
    entries: list[tuple[str, str]],
) -> str:
    extraction_target = Path(extraction_relative).with_suffix("").as_posix()
    today = datetime.now().astimezone().date().isoformat()
    lines = [
        "---",
        f'title: "{title}"',
        "type: source-structure-index",
        "scope: enterprise",
        "capture_mode: standalone-collection",
        'native_unit_type: "transcript"',
        f"native_unit_count: {len(entries)}",
        f'source_id: "sha256:{source_hash}"',
        f'extraction_sha256: "{extraction_hash}"',
        f"updated: {today}",
        "---",
        "",
        f"# {title}",
        "",
        f"- 完整正文：[[{extraction_target}|打开完整提取]]",
        f"- 条目数量：{len(entries)}",
        "- 收录原则：完整正文保留在来源层；本页只提供条目导航，不把每条逐字稿复制为知识笔记。",
        "",
    ]
    for start in range(0, len(entries), 50):
        chunk = entries[start : start + 50]
        lines.extend(
            [
                f"## {chunk[0][0]}–{chunk[-1][0]}",
                "",
            ]
        )
        for number, entry_title in chunk:
            heading = f"{number}｜{entry_title}"
            display = heading.replace("|", "｜").replace("]", "）")
            target_heading = heading.replace("|", "\\|")
            lines.append(
                f"- [[{extraction_target}#{target_heading}|{display}]]"
            )
        lines.append("")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, object]:
    vault = args.vault.resolve()
    extraction_relative = normalize_relative(args.extraction, "10_来源/提取")
    output_relative = normalize_relative(args.output, "10_来源/提取")
    extraction = vault / Path(extraction_relative)
    output = vault / Path(output_relative)
    if not extraction.is_file():
        raise IndexError(f"Extraction not found: {extraction}")
    if output.suffix.lower() != ".md":
        raise IndexError("Output must be Markdown")
    text = extraction.read_text(encoding="utf-8-sig")
    entries = [(match.group(1), match.group(2).strip()) for match in HEADING.finditer(text)]
    if len(entries) != args.expected_count:
        raise IndexError(
            f"Entry count mismatch: expected {args.expected_count}, found {len(entries)}"
        )
    expected_numbers = [f"{value:03d}" for value in range(1, args.expected_count + 1)]
    actual_numbers = [number for number, _ in entries]
    if actual_numbers != expected_numbers:
        raise IndexError("Entry numbering is not continuous")
    if len({f"{number}｜{title}" for number, title in entries}) != len(entries):
        raise IndexError("Duplicate entry headings found")

    extraction_hash = sha256_file(extraction)
    index_text = build_index(
        args.title,
        args.source_sha256,
        extraction_relative,
        extraction_hash,
        entries,
    )
    index_data = index_text.encode("utf-8")
    result: dict[str, object] = {
        "ok": True,
        "mode": "build-document-index",
        "write": args.write,
        "capture_mode": "standalone-collection",
        "entry_count": len(entries),
        "extraction": extraction_relative,
        "extraction_sha256": extraction_hash,
        "output": output_relative,
        "output_sha256": sha256_bytes(index_data),
        "output_bytes": len(index_data),
    }
    if not args.write:
        return result

    backup = None
    if output.exists():
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        backup = (
            vault
            / ".kb"
            / "backups"
            / f"document-index-{stamp}"
            / output.name
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, backup)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(index_data)
        os.replace(temp_name, output)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    if sha256_file(output) != result["output_sha256"]:
        raise IndexError("Written index hash mismatch")
    if backup is not None:
        result["backup"] = backup.relative_to(vault).as_posix()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--extraction", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--write", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except IndexError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

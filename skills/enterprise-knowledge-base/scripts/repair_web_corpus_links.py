#!/usr/bin/env python3
"""Repair deterministic Wiki-link errors in an expanded web corpus."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from kb_core import (
    CoreError,
    atomic_write_json as atomic_json_write,
    load_json as core_load_json,
    sha256_bytes,
    sha256_file,
)


WIKI_LINK = re.compile(r"(?<!\\)(!?)\[\[([^\]]+)\]\]")
FENCE = re.compile(r"^\s*(?:>\s*)*(?:```|~~~)")
MARKDOWN_EXTENSIONS = {".md", ".markdown"}


class RepairError(RuntimeError):
    pass


def normalized_markdown_sha256(data: bytes) -> str:
    text = data.decode("utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = core_load_json(path)
    except CoreError as exc:
        raise RepairError(str(exc)) from exc
    if not isinstance(value, dict):
        raise RepairError(f"JSON root must be an object: {path}")
    return value


def split_target(raw: str) -> tuple[str, str, str]:
    separator = ""
    alias = ""
    target_and_header = raw
    match = re.search(r"(\\?\|)", raw)
    if match:
        separator = match.group(1)
        target_and_header = raw[: match.start()]
        alias = raw[match.end() :]
    return target_and_header.strip(), separator, alias


def target_without_header(target_and_header: str) -> tuple[str, str]:
    if "#" not in target_and_header:
        return target_and_header.strip(), ""
    target, header = target_and_header.split("#", 1)
    return target.strip(), "#" + header


def inline_code_ranges(line: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index, char in enumerate(line):
        if char != "`":
            continue
        if start is None:
            start = index
        else:
            ranges.append((start, index + 1))
            start = None
    if start is not None:
        ranges.append((start, len(line)))
    return ranges


def in_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def frontmatter_aliases(text: str) -> list[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    result: list[str] = []
    in_aliases = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if re.match(r"^aliases:\s*$", line):
            in_aliases = True
            continue
        if in_aliases:
            match = re.match(r"^\s+-\s+(.*?)\s*$", line)
            if match:
                result.append(match.group(1).strip(" \"'"))
                continue
            if line and not line.startswith((" ", "\t")):
                in_aliases = False
    return result


def build_lookup(
    root: Path,
    ignored: set[str] | None = None,
) -> tuple[list[Path], dict[str, list[str]]]:
    ignored = ignored or set()
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in ignored
    ]
    lookup: dict[str, list[str]] = defaultdict(list)
    for path in files:
        relative = path.relative_to(root).as_posix()
        pure = PurePosixPath(relative)
        values = {relative, pure.name, pure.stem, pure.with_suffix("").as_posix()}
        if path.suffix.lower() in MARKDOWN_EXTENSIONS:
            values.update(frontmatter_aliases(path.read_text(encoding="utf-8-sig")))
        for value in values:
            if relative not in lookup[value.casefold()]:
                lookup[value.casefold()].append(relative)
    return files, lookup


def resolve_target(
    vault: Path,
    root: Path,
    source: Path,
    target: str,
    lookup: dict[str, list[str]],
    ignored: set[str] | None = None,
) -> list[str]:
    ignored = ignored or set()
    corpus_relative = root.relative_to(vault).as_posix()
    normalized = target.replace("\\", "/").strip("/")
    if normalized.startswith(corpus_relative + "/"):
        normalized = normalized[len(corpus_relative) + 1 :]
    variants = [normalized]
    if not PurePosixPath(normalized).suffix:
        variants.append(normalized + ".md")
    candidates: list[str] = []
    source_relative = source.relative_to(root).as_posix()
    for variant in variants:
        for candidate in (
            PurePosixPath(variant).as_posix(),
            (PurePosixPath(source_relative).parent / PurePosixPath(variant)).as_posix(),
        ):
            if (
                candidate not in ignored
                and (root / Path(candidate)).is_file()
                and candidate not in candidates
            ):
                candidates.append(candidate)
    if candidates:
        return candidates
    return list(dict.fromkeys(lookup.get(normalized.casefold(), [])))


def entry_accepts_file(entry: dict[str, Any], path: Path) -> bool:
    actual = sha256_file(path)
    if actual == str(entry.get("sha256", "")):
        return True
    if actual == str(entry.get("adapted_sha256", "")):
        return True
    adapted_normalized = str(entry.get("adapted_normalized_sha256", ""))
    if (
        path.suffix.lower() in MARKDOWN_EXTENSIONS
        and adapted_normalized
        and normalized_markdown_sha256(path.read_bytes()) == adapted_normalized
    ):
        return True
    expected_normalized = str(entry.get("normalized_sha256", ""))
    return (
        path.suffix.lower() in MARKDOWN_EXTENSIONS
        and bool(expected_normalized)
        and normalized_markdown_sha256(path.read_bytes()) == expected_normalized
    )


def scan_and_repair(
    vault: Path,
    root: Path,
    rules: dict[str, Any],
    ignored: set[str] | None = None,
) -> tuple[dict[Path, bytes], list[dict[str, Any]], list[dict[str, Any]], int]:
    ignored = ignored or set()
    _, lookup = build_lookup(root, ignored)
    direct = {
        str(key): str(value)
        for key, value in dict(rules.get("direct_targets", {})).items()
    }
    context_rules: dict[tuple[str, str, int], str] = {}
    for item in rules.get("context_targets", []):
        if not isinstance(item, dict):
            continue
        context_rules[
            (str(item["source"]), str(item["target"]), int(item["occurrence"]))
        ] = str(item["replacement"])
    exceptions = {
        (str(item["source"]), str(item["target"])): str(item.get("reason", ""))
        for item in rules.get("intentional_unresolved", [])
        if isinstance(item, dict)
    }
    planned: dict[Path, bytes] = {}
    repairs: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    exception_count = 0
    corpus_relative = root.relative_to(vault).as_posix()
    normalize_all = bool(rules.get("normalize_all_resolved", False))

    for path in sorted(root.rglob("*.md")):
        source_relative = path.relative_to(root).as_posix()
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig")
        lines = text.splitlines(keepends=True)
        fenced = False
        occurrence_counts: dict[str, int] = defaultdict(int)
        changed = False
        output_lines: list[str] = []
        for line_number, line in enumerate(lines, start=1):
            if FENCE.match(line):
                fenced = not fenced
                output_lines.append(line)
                continue
            if fenced:
                output_lines.append(line)
                continue
            code_ranges = inline_code_ranges(line)
            replacements: list[tuple[int, int, str]] = []
            for match in WIKI_LINK.finditer(line):
                if in_ranges(match.start(), code_ranges):
                    continue
                target_and_header, separator, alias = split_target(match.group(2))
                target, header = target_without_header(target_and_header)
                if not target:
                    continue
                occurrence_counts[target] += 1
                candidates = resolve_target(
                    vault, root, path, target, lookup, ignored
                )
                if len(candidates) == 1:
                    candidate_suffix = PurePosixPath(candidates[0]).suffix.lower()
                    needs_attachment_extension = (
                        candidate_suffix not in MARKDOWN_EXTENSIONS
                        and not PurePosixPath(target).suffix
                    )
                    if (
                        not normalize_all
                        or (
                            target.replace("\\", "/").strip("/").startswith(
                                corpus_relative + "/"
                            )
                            and not needs_attachment_extension
                        )
                    ):
                        continue
                    replacement = candidates[0]
                else:
                    replacement = context_rules.get(
                        (source_relative, target, occurrence_counts[target])
                    )
                    if replacement is None:
                        replacement = direct.get(target)
                if replacement is not None:
                    destination = root / Path(replacement)
                    if not destination.suffix and destination.with_suffix(".md").is_file():
                        destination = destination.with_suffix(".md")
                    if not destination.is_file():
                        raise RepairError(
                            f"Repair target does not exist: {source_relative}:{line_number}: "
                            f"{target} -> {replacement}"
                        )
                    repaired_relative = destination.relative_to(root).as_posix()
                    repaired_path = PurePosixPath(repaired_relative)
                    link_path = (
                        repaired_path.with_suffix("")
                        if destination.suffix.lower() in MARKDOWN_EXTENSIONS
                        else repaired_path
                    )
                    full_target = f"{corpus_relative}/{link_path.as_posix()}"
                    new_body = full_target + header
                    if separator:
                        new_body += separator + alias
                    new_link = f"{match.group(1)}[[{new_body}]]"
                    replacements.append((match.start(), match.end(), new_link))
                    repairs.append(
                        {
                            "source": source_relative,
                            "line": line_number,
                            "target": target,
                            "replacement": repaired_relative,
                        }
                    )
                elif (source_relative, target) in exceptions:
                    exception_count += 1
                else:
                    remaining.append(
                        {
                            "source": source_relative,
                            "line": line_number,
                            "target": target,
                            "kind": "ambiguous" if candidates else "unresolved",
                            "candidates": candidates,
                        }
                    )
            if replacements:
                for start, end, value in reversed(replacements):
                    line = line[:start] + value + line[end:]
                changed = True
            output_lines.append(line)
        if changed:
            newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
            new_text = "".join(output_lines)
            if newline == "\r\n":
                new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
            planned[path] = new_text.encode("utf-8")

    return planned, repairs, remaining, exception_count


def run(args: argparse.Namespace) -> dict[str, Any]:
    vault = args.vault.resolve()
    rules_path = args.rules.resolve()
    rules = load_object(rules_path)
    corpus_relative = str(rules.get("corpus_root", "")).replace("\\", "/").strip("/")
    if not corpus_relative.startswith("10_来源/提取/网页/"):
        raise RepairError(f"Unsafe corpus root: {corpus_relative}")
    root = vault / Path(corpus_relative)
    if not root.is_dir():
        raise RepairError(f"Corpus root not found: {root}")

    state_path = vault / ".kb" / "state" / "web_corpora.json"
    state = load_object(state_path)
    items = state.get("items", [])
    if not isinstance(items, list):
        raise RepairError("Invalid web corpus state items")
    item = next(
        (
            value
            for value in items
            if isinstance(value, dict) and value.get("corpus_root") == corpus_relative
        ),
        None,
    )
    if item is None:
        raise RepairError(f"Corpus is not registered: {corpus_relative}")
    entries = item.get("entries", [])
    if not isinstance(entries, list):
        raise RepairError("Invalid corpus entries")
    entry_by_path = {
        str(entry.get("path", "")): entry
        for entry in entries
        if isinstance(entry, dict)
    }
    for relative, entry in entry_by_path.items():
        path = root / Path(relative)
        if not path.is_file() or not entry_accepts_file(entry, path):
            raise RepairError(f"Corpus file differs from source or recorded adaptation: {relative}")
    state_refresh = [
        (relative, entry)
        for relative, entry in entry_by_path.items()
        if str(entry.get("adapted_sha256", ""))
        and not str(entry.get("adapted_normalized_sha256", ""))
        and (root / Path(relative)).suffix.lower() in MARKDOWN_EXTENSIONS
    ]

    corpus_stubs: list[Path] = []
    ignored_corpus_paths: set[str] = set()
    for value in rules.get("empty_corpus_stubs", []):
        relative = str(value).replace("\\", "/").strip("/")
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in entry_by_path
        ):
            raise RepairError(f"Unsafe or managed corpus stub: {value}")
        path = root / candidate
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size != 0:
            raise RepairError(f"Corpus cleanup target is not an empty file: {path}")
        corpus_stubs.append(path)
        ignored_corpus_paths.add(relative)
    if bool(rules.get("auto_empty_attachment_shadow_stubs", False)):
        registered_attachment_stems: dict[str, list[str]] = defaultdict(list)
        for relative in entry_by_path:
            pure = PurePosixPath(relative)
            if pure.suffix.lower() in MARKDOWN_EXTENSIONS:
                continue
            registered_attachment_stems[pure.with_suffix("").as_posix()].append(
                relative
            )
        for path in root.rglob("*.md"):
            relative = path.relative_to(root).as_posix()
            if (
                relative in entry_by_path
                or relative in ignored_corpus_paths
                or path.stat().st_size != 0
            ):
                continue
            stem = PurePosixPath(relative).with_suffix("").as_posix()
            if len(registered_attachment_stems.get(stem, [])) != 1:
                continue
            corpus_stubs.append(path)
            ignored_corpus_paths.add(relative)

    planned, repairs, remaining, exception_count = scan_and_repair(
        vault, root, rules, ignored_corpus_paths
    )
    if remaining:
        raise RepairError(
            "Unmapped link errors remain: "
            + json.dumps(remaining[:10], ensure_ascii=False)
        )

    inbox = vault / "00_收件箱"
    inbox_stubs: list[Path] = []
    for name in rules.get("empty_inbox_stubs", []):
        path = inbox / str(name)
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size != 0:
            raise RepairError(f"Inbox cleanup target is not an empty file: {path}")
        inbox_stubs.append(path)
    for generated in rules.get("generated_inbox_stubs", []):
        if not isinstance(generated, dict):
            continue
        path = inbox / str(generated.get("name", ""))
        if not path.exists():
            continue
        expected = str(generated.get("sha256", "")).lower()
        if not path.is_file() or sha256_file(path) != expected:
            raise RepairError(f"Generated inbox stub differs from reviewed hash: {path}")
        inbox_stubs.append(path)
    stubs = corpus_stubs + inbox_stubs

    rule_hash = sha256_file(rules_path)
    result = {
        "ok": True,
        "mode": "repair-web-corpus-links",
        "write": args.write,
        "corpus_root": corpus_relative,
        "rules_sha256": rule_hash,
        "files_changed": len(planned),
        "links_repaired": len(repairs),
        "empty_stubs_to_remove": len(stubs),
        "intentional_example_links": exception_count,
        "unmapped_link_errors": 0,
        "state_entries_to_refresh": len(state_refresh),
        "stubs": [
            (
                path.relative_to(root).as_posix()
                if path.is_relative_to(root)
                else path.relative_to(inbox).as_posix()
            )
            for path in stubs
        ],
    }
    if getattr(args, "details", False):
        result["repairs"] = repairs
    if not args.write:
        return result

    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%dT%H%M%S%z")
    backup_root = vault / ".kb" / "backups" / f"link-repair-{stamp}"
    backup_root.mkdir(parents=True)
    changed_paths = list(planned)
    try:
        for path in changed_paths:
            backup = backup_root / "corpus" / path.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        state_backup = backup_root / "web_corpora.json"
        shutil.copy2(state_path, state_backup)
        for path in corpus_stubs:
            backup = backup_root / "corpus-stubs" / path.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(backup))
        for path in inbox_stubs:
            backup = backup_root / "inbox" / path.relative_to(inbox)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(backup))

        for path, data in planned.items():
            temp = path.with_name(path.name + ".link-repair.tmp")
            temp.write_bytes(data)
            os.replace(temp, path)

        for path in changed_paths:
            relative = path.relative_to(root).as_posix()
            entry = entry_by_path[relative]
            entry["adapted_sha256"] = sha256_file(path)
            entry["adapted_normalized_sha256"] = normalized_markdown_sha256(
                path.read_bytes()
            )
            entry["adapted_at"] = now.isoformat()
        for relative, entry in state_refresh:
            path = root / Path(relative)
            entry["adapted_normalized_sha256"] = normalized_markdown_sha256(
                path.read_bytes()
            )
        if repairs or stubs:
            item["link_repair"] = {
                "schema_version": 1,
                "applied_at": now.isoformat(),
                "rules": rules_path.relative_to(vault).as_posix(),
                "rules_sha256": rule_hash,
                "files_changed": len(planned),
                "links_repaired": len(repairs),
                "empty_stubs_removed": len(stubs),
                "intentional_example_links": exception_count,
            }
        elif state_refresh and isinstance(item.get("link_repair"), dict):
            item["link_repair"]["state_refreshed_at"] = now.isoformat()
        atomic_json_write(state_path, state)
    except Exception:
        for path in changed_paths:
            backup = backup_root / "corpus" / path.relative_to(root)
            if backup.is_file():
                shutil.copy2(backup, path)
        for path in corpus_stubs:
            backup = backup_root / "corpus-stubs" / path.relative_to(root)
            if backup.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(path))
        for path in inbox_stubs:
            backup = backup_root / "inbox" / path.relative_to(inbox)
            if backup.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(path))
        state_backup = backup_root / "web_corpora.json"
        if state_backup.is_file():
            shutil.copy2(state_backup, state_path)
        raise

    result["backup_root"] = backup_root.relative_to(vault).as_posix()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--details", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except RepairError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

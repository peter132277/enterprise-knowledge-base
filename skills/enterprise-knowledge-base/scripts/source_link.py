#!/usr/bin/env python3
"""Inject and verify a preserved-original link in a Feishu publish payload."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kb_core import sha256_bytes


SOURCE_HEADING_RE = re.compile(
    r"(?m)^##[ \t]+(?:来源|来源与可追溯性)[ \t]*$"
)
NEXT_H2_RE = re.compile(r"(?m)^##[ \t]+\S")
ORIGINAL_SUBHEADING_RE = re.compile(r"^###[ \t]+原文件[ \t]*$")
ORIGINAL_METADATA_RE = re.compile(
    r"^[ \t]*-[ \t]*(?:原始文件|原始位置|原件|飞书原件|原文件|文件校验|文件[ \t]+SHA-256)"
    r"[ \t]*(?:：|:).*$"
)
FEISHU_FILE_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
TABLE_SEPARATOR_RE = re.compile(r"^\|(?:[ \t]*:?-+:?[ \t]*\|)+$")


class SourceLinkError(RuntimeError):
    pass


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_in_vault(vault: Path, value: str | Path) -> Path:
    vault = vault.resolve()
    path = Path(value)
    if not path.is_absolute():
        path = vault / path
    path = path.resolve()
    if not within(path, vault):
        raise SourceLinkError(f"Path escapes Vault: {value}")
    return path


def validate_source_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise SourceLinkError("Source URL must use HTTPS.")
    if not any(host.endswith(suffix) for suffix in FEISHU_FILE_HOST_SUFFIXES):
        raise SourceLinkError("Source URL must be a Feishu or Lark file URL.")
    if not re.fullmatch(r"/file/[^/?#]+/?", parsed.path):
        raise SourceLinkError("Source URL must point to a Feishu Drive file.")
    return value.strip()


def source_section_bounds(markdown: str) -> tuple[int, int] | None:
    match = SOURCE_HEADING_RE.search(markdown)
    if not match:
        return None
    next_heading = NEXT_H2_RE.search(markdown, match.end())
    return match.start(), next_heading.start() if next_heading else len(markdown)


def clean_source_section(section: str) -> str:
    lines = normalize_newlines(section).split("\n")
    cleaned: list[str] = []
    skipping_original = False
    for line in lines:
        if ORIGINAL_SUBHEADING_RE.match(line):
            skipping_original = True
            continue
        if skipping_original:
            if re.match(r"^###[ \t]+\S", line):
                skipping_original = False
            else:
                continue
        if ORIGINAL_METADATA_RE.match(line):
            continue
        cleaned.append(line.rstrip())

    compact: list[str] = []
    blank = False
    for line in cleaned:
        if not line:
            if blank:
                continue
            blank = True
        else:
            blank = False
        compact.append(line)
    return "\n".join(compact).strip()


def original_link_block(source_name: str, source_url: str, source_hash: str) -> str:
    safe_name = source_name.replace("[", r"\[").replace("]", r"\]")
    return (
        "### 原文件\n\n"
        f"- [下载原文件：{safe_name}]({source_url})\n"
        f"- 文件 SHA-256：`{source_hash}`"
    )


def inject_source_link(
    markdown: str,
    *,
    source_name: str,
    source_url: str,
    source_hash: str,
) -> str:
    source_url = validate_source_url(source_url)
    text = normalize_newlines(markdown).strip()
    bounds = source_section_bounds(text)
    link_block = original_link_block(source_name, source_url, source_hash)

    if bounds is None:
        return f"{text}\n\n## 来源\n\n{link_block}\n"

    start, end = bounds
    section = clean_source_section(text[start:end])
    replacement = f"{section}\n\n{link_block}\n"
    prefix = text[:start].rstrip()
    suffix = text[end:].lstrip()
    result = f"{prefix}\n\n{replacement}"
    if suffix:
        result += f"\n{suffix}"
    return result.rstrip() + "\n"


def without_source_section(markdown: str) -> str:
    text = normalize_newlines(markdown).strip()
    bounds = source_section_bounds(text)
    if bounds is not None:
        start, end = bounds
        text = f"{text[:start].rstrip()}\n\n{text[end:].lstrip()}".strip()
    lines = text.split("\n")
    if lines and re.match(r"^#[ \t]+\S", lines[0]):
        lines = lines[1:]
    return "\n".join(line.rstrip() for line in lines).strip()


def normalize_markdown_roundtrip(markdown: str) -> str:
    """Normalize syntax-only changes made by Feishu's Markdown exporter."""
    lines = normalize_newlines(markdown).split("\n")
    normalized: list[str] = []
    task_item = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+")
    self_link = re.compile(r"\[(https?://[^\]\s]+)\]\(\1\)")
    for index, line in enumerate(lines):
        if not line.strip():
            previous = next(
                (candidate for candidate in reversed(normalized) if candidate.strip()),
                "",
            )
            following = next(
                (candidate for candidate in lines[index + 1 :] if candidate.strip()),
                "",
            )
            if task_item.match(previous) and task_item.match(following):
                continue
        if TABLE_SEPARATOR_RE.fullmatch(line):
            columns = line.count("|") - 1
            line = "|" + "|".join("---" for _ in range(columns)) + "|"
        line = self_link.sub(lambda match: f"<{match.group(1)}>", line)
        normalized.append(line.rstrip())
    return "\n".join(normalized).strip()


def compare_ignoring_source(expected: str, remote: str) -> dict[str, Any]:
    expected_core = normalize_markdown_roundtrip(without_source_section(expected))
    remote_core = normalize_markdown_roundtrip(without_source_section(remote))
    return {
        "match": expected_core == remote_core,
        "expected_core_hash": sha256_bytes(expected_core.encode("utf-8")),
        "remote_core_hash": sha256_bytes(remote_core.encode("utf-8")),
    }


def verify_source_link(
    markdown: str,
    *,
    source_name: str,
    source_url: str,
) -> dict[str, Any]:
    validate_source_url(source_url)
    text = normalize_newlines(markdown)
    bounds = source_section_bounds(text)
    if bounds is None:
        return {"verified": False, "reason": "missing_source_section"}
    section = text[bounds[0] : bounds[1]]
    name_present = source_name in section or source_name.replace("[", r"\[").replace(
        "]", r"\]"
    ) in section
    url_present = source_url in section
    return {
        "verified": bool(name_present and url_present),
        "name_present": name_present,
        "url_present": url_present,
    }


def extract_remote_markdown(value: Any) -> str:
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            return data["content"]
        for key in ("content", "markdown", "body"):
            if isinstance(value.get(key), str):
                return value[key]
        for nested in value.values():
            try:
                return extract_remote_markdown(nested)
            except SourceLinkError:
                continue
    elif isinstance(value, list):
        for nested in value:
            try:
                return extract_remote_markdown(nested)
            except SourceLinkError:
                continue
    raise SourceLinkError("Remote JSON does not contain Markdown content.")


def load_remote_json(path_value: str) -> str:
    raw = sys.stdin.read() if path_value == "-" else Path(path_value).read_text(
        encoding="utf-8"
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceLinkError("Remote input is not valid JSON.") from exc
    return extract_remote_markdown(payload)


def print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inject = subparsers.add_parser("inject")
    inject.add_argument("--payload", required=True)
    inject.add_argument("--source", required=True)
    inject.add_argument("--source-name", default="")
    inject.add_argument("--source-url", required=True)
    inject.add_argument("--output", default="")

    compare = subparsers.add_parser("compare")
    compare.add_argument("--payload", required=True)
    compare.add_argument("--remote-json", default="-")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--source", required=True)
    verify.add_argument("--source-name", default="")
    verify.add_argument("--source-url", required=True)
    verify.add_argument("--remote-json", default="-")

    args = parser.parse_args()
    vault = Path(args.vault).resolve()
    try:
        if args.command == "inject":
            payload = resolve_in_vault(vault, args.payload)
            source = resolve_in_vault(vault, args.source)
            if not payload.is_file() or not source.is_file():
                raise SourceLinkError("Payload or preserved source file is missing.")
            output = (
                resolve_in_vault(vault, args.output)
                if args.output
                else payload.with_name(f"{payload.stem}.with-source-link.md")
            )
            if not within(output, vault):
                raise SourceLinkError("Output path escapes Vault.")
            source_hash = sha256_bytes(source.read_bytes())
            result = inject_source_link(
                payload.read_text(encoding="utf-8"),
                source_name=args.source_name or source.name,
                source_url=args.source_url,
                source_hash=source_hash,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(result, encoding="utf-8", newline="\n")
            print_json(
                {
                    "ok": True,
                    "output_relative": output.resolve().relative_to(vault.resolve()).as_posix(),
                    "final_payload_hash": sha256_bytes(output.read_bytes()),
                    "source_hash": source_hash,
                    "source_file_url": validate_source_url(args.source_url),
                }
            )
        elif args.command == "compare":
            payload = resolve_in_vault(vault, args.payload)
            result = compare_ignoring_source(
                payload.read_text(encoding="utf-8"),
                load_remote_json(args.remote_json),
            )
            print_json({"ok": result["match"], **result})
            if not result["match"]:
                raise SystemExit(3)
        else:
            source = resolve_in_vault(vault, args.source)
            result = verify_source_link(
                load_remote_json(args.remote_json),
                source_name=args.source_name or source.name,
                source_url=args.source_url,
            )
            print_json({"ok": result["verified"], **result})
            if not result["verified"]:
                raise SystemExit(3)
    except (OSError, UnicodeError, SourceLinkError) as exc:
        print_json({"ok": False, "error": str(exc)})
        raise SystemExit(2)


if __name__ == "__main__":
    main()

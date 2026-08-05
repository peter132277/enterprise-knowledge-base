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
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([^`]*)$")
HEADING_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]+(.+?)#*[ \t]*$")
LIST_RE = re.compile(
    r"^([ \t]*)([-+*]|\d+[.)])[ \t]+(?:\[([ xX])\][ \t]+)?(.*)$"
)
BLOCKQUOTE_RE = re.compile(r"^[ \t]*(>+)[ \t]?(.*)$")
HTML_RE = re.compile(r"<\/?[A-Za-z!][^>]*>")


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


def _split_table_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells)


def _inline_semantics(value: str) -> tuple[str, bool]:
    if HTML_RE.search(value):
        return "", False
    text = value
    protected: list[str] = []

    def protect(kind: str, body: str) -> str:
        protected.append(f"{kind}:{body}")
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(
        r"`([^`\n]+)`",
        lambda match: protect("code", match.group(1)),
        text,
    )
    text = re.sub(
        r"!\[([^\]]*)\]\((https?://[^)\s]+)(?:[ \t]+[\"'][^\"']*[\"'])?\)",
        lambda match: protect("image", f"{match.group(1)}|{match.group(2)}"),
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)(?:[ \t]+[\"'][^\"']*[\"'])?\)",
        lambda match: protect("link", f"{match.group(1)}|{match.group(2)}"),
        text,
    )
    text = re.sub(
        r"<(https?://[^>\s]+)>",
        lambda match: protect("link", f"{match.group(1)}|{match.group(1)}"),
        text,
    )
    text = re.sub(r"(?<!\\)(\*\*|__|~~)", "", text)
    text = re.sub(r"(?<![\\\w])([*_])|([*_])(?!\w)", "", text)
    text = re.sub(r"\\([\\`*_[\]{}()#+.!|>~-])", r"\1", text)
    text = re.sub(r"[ \t\n]+", " ", text).strip()
    text = re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", text)
    for index, item in enumerate(protected):
        text = text.replace(f"\x00{index}\x00", f"<{item}>")
    return text, True


def semantic_markdown_model(markdown: str) -> dict[str, Any]:
    """Build a conservative block model for format-independent readback checks."""
    lines = normalize_markdown_roundtrip(without_source_section(markdown)).split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            language = fence.group(2).strip().casefold()
            body: list[str] = []
            index += 1
            while index < len(lines) and not re.match(
                rf"^[ \t]*{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$",
                lines[index],
            ):
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                return {"supported": False, "reason": "unclosed-code-fence"}
            blocks.append(
                {
                    "type": "code",
                    "language": language,
                    "content": "\n".join(body),
                }
            )
            index += 1
            continue
        heading = HEADING_RE.match(line)
        if heading:
            text, supported = _inline_semantics(heading.group(2))
            if not supported:
                return {"supported": False, "reason": "html-in-heading"}
            blocks.append(
                {"type": "heading", "level": len(heading.group(1)), "text": text}
            )
            index += 1
            continue
        if index + 1 < len(lines) and re.fullmatch(r"[ \t]*(=+|-+)[ \t]*", lines[index + 1]):
            text, supported = _inline_semantics(line)
            if not supported:
                return {"supported": False, "reason": "html-in-heading"}
            blocks.append(
                {
                    "type": "heading",
                    "level": 1 if "=" in lines[index + 1] else 2,
                    "text": text,
                }
            )
            index += 2
            continue
        if index + 1 < len(lines) and "|" in line and _is_table_separator(lines[index + 1]):
            rows = [_split_table_row(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_table_row(lines[index]))
                index += 1
            normalized_rows: list[list[str]] = []
            width = len(rows[0])
            for row in rows:
                if len(row) != width:
                    return {"supported": False, "reason": "ragged-table"}
                normalized_row: list[str] = []
                for cell in row:
                    text, supported = _inline_semantics(cell)
                    if not supported:
                        return {"supported": False, "reason": "html-in-table"}
                    normalized_row.append(text)
                normalized_rows.append(normalized_row)
            blocks.append({"type": "table", "rows": normalized_rows})
            continue
        item = LIST_RE.match(line)
        if item:
            text, supported = _inline_semantics(item.group(4))
            if not supported:
                return {"supported": False, "reason": "html-in-list"}
            blocks.append(
                {
                    "type": "list-item",
                    "depth": len(item.group(1).expandtabs(4)) // 2,
                    "ordered": item.group(2)[0].isdigit(),
                    "checked": (
                        None
                        if item.group(3) is None
                        else item.group(3).casefold() == "x"
                    ),
                    "text": text,
                }
            )
            index += 1
            continue
        quote = BLOCKQUOTE_RE.match(line)
        if quote:
            text, supported = _inline_semantics(quote.group(2))
            if not supported:
                return {"supported": False, "reason": "html-in-blockquote"}
            blocks.append(
                {"type": "blockquote", "depth": len(quote.group(1)), "text": text}
            )
            index += 1
            continue
        if re.fullmatch(r"[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*", line):
            blocks.append({"type": "thematic-break"})
            index += 1
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index]
            if (
                FENCE_RE.match(candidate)
                or HEADING_RE.match(candidate)
                or LIST_RE.match(candidate)
                or BLOCKQUOTE_RE.match(candidate)
                or (
                    index + 1 < len(lines)
                    and "|" in candidate
                    and _is_table_separator(lines[index + 1])
                )
            ):
                break
            paragraph.append(candidate)
            index += 1
        text, supported = _inline_semantics("\n".join(paragraph))
        if not supported:
            return {"supported": False, "reason": "raw-html"}
        blocks.append({"type": "paragraph", "text": text})
    encoded = json.dumps(
        blocks,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "supported": True,
        "blocks": blocks,
        "hash": sha256_bytes(encoded),
    }


def compare_ignoring_source(expected: str, remote: str) -> dict[str, Any]:
    expected_core = normalize_markdown_roundtrip(without_source_section(expected))
    remote_core = normalize_markdown_roundtrip(without_source_section(remote))
    strict_match = expected_core == remote_core
    expected_semantic = semantic_markdown_model(expected)
    remote_semantic = semantic_markdown_model(remote)
    semantic_supported = bool(
        expected_semantic.get("supported") and remote_semantic.get("supported")
    )
    semantic_match = bool(
        semantic_supported
        and expected_semantic.get("blocks") == remote_semantic.get("blocks")
    )
    if strict_match:
        classification = "strict-match"
    elif semantic_match:
        classification = "format-only"
    elif not semantic_supported:
        classification = "unsupported-structure"
    else:
        classification = "semantic-content-difference"
    return {
        "match": strict_match or semantic_match,
        "strict_match": strict_match,
        "semantic_match": semantic_match,
        "semantic_supported": semantic_supported,
        "classification": classification,
        "expected_core_hash": sha256_bytes(expected_core.encode("utf-8")),
        "remote_core_hash": sha256_bytes(remote_core.encode("utf-8")),
        "expected_semantic_hash": str(expected_semantic.get("hash", "")),
        "remote_semantic_hash": str(remote_semantic.get("hash", "")),
        "unsupported_reason": str(
            expected_semantic.get("reason") or remote_semantic.get("reason") or ""
        ),
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

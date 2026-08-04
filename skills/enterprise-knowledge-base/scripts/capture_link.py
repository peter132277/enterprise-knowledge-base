#!/usr/bin/env python3
"""Capture a web page or GitHub subtree into deterministic local evidence."""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
}
MARKDOWN_LINK_RE = re.compile(r"^\s*\[[^\]]+\]\((https?://[^)]+)\)\s*$")
MAX_SINGLE_BYTES = 20 * 1024 * 1024


class CaptureError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unwrap_markdown_link(value: str) -> str:
    match = MARKDOWN_LINK_RE.match(value)
    return match.group(1) if match else value.strip()


def canonicalize_url(value: str) -> str:
    raw = unwrap_markdown_link(value)
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise CaptureError(f"Unsupported URL: {value}")
    if parsed.username or parsed.password:
        raise CaptureError("URLs with embedded credentials are not allowed")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=")
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [
        (key, val)
        for key, val in query_pairs
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    query = urllib.parse.urlencode(sorted(filtered))
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if host in {"localhost", "localhost.localdomain"}:
        raise CaptureError("Local addresses are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
    except socket.gaierror as exc:
        raise CaptureError(f"Cannot resolve URL host: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise CaptureError(f"Non-public URL address is not allowed: {address}")


def run(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CaptureError(f"Command failed ({command[0]}): {detail}")
    return completed.stdout.strip()


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "link-capture"


def write_if_changed(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    path.write_bytes(data)


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "section", "article", "header", "footer", "br", "li"}:
            self.parts.append("\n")
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "title":
            self.in_title = False
        if not self.skip_depth and tag in {"p", "div", "section", "article", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = html.unescape(data)
        if self.in_title:
            self.title += text
        self.parts.append(text)

    def markdown(self) -> str:
        text = "".join(self.parts).replace("\r\n", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def fetch(url: str, timeout: int) -> tuple[bytes, str, str, str]:
    validate_public_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Codex-Knowledge-Capture/1.0",
            "Accept": "text/html,application/xhtml+xml,text/markdown,text/plain,application/json,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            data = response.read(MAX_SINGLE_BYTES + 1)
            final_url = response.geturl()
    except (OSError, urllib.error.URLError) as exc:
        raise CaptureError(f"Failed to fetch URL: {url}: {exc}") from exc
    if len(data) > MAX_SINGLE_BYTES:
        raise CaptureError(f"Single page exceeds {MAX_SINGLE_BYTES} bytes")
    return data, content_type, charset, final_url


def capture_single(args: argparse.Namespace) -> dict[str, Any]:
    canonical = canonicalize_url(args.url)
    data, content_type, charset, final_url = fetch(canonical, args.timeout)
    suffix = {
        "text/html": ".html",
        "text/markdown": ".md",
        "text/plain": ".txt",
        "application/json": ".json",
    }.get(content_type, ".bin")
    name = safe_name(args.name or urllib.parse.urlsplit(canonical).hostname or "web")
    output_dir = args.output_dir.resolve()
    raw_path = output_dir / f"{name}{suffix}"
    extraction_path = output_dir / f"{name}.extracted.md"
    metadata_path = output_dir / f"{name}.capture.json"
    write_if_changed(raw_path, data)

    decoded = data.decode(charset, errors="replace")
    if "\ufffd" in decoded:
        raise CaptureError("Fetched page contains undecodable text")
    title = ""
    if content_type == "text/html":
        parser = HTMLTextExtractor()
        parser.feed(decoded)
        extracted = parser.markdown()
        title = parser.title.strip()
    else:
        extracted = decoded.strip() + "\n"
    extraction = (
        f"# {title or canonical}\n\n"
        f"- 来源：{canonical}\n"
        f"- 最终地址：{canonicalize_url(final_url)}\n"
        f"- 抓取时间：{datetime.now(timezone.utc).isoformat()}\n"
        f"- Content-Type：{content_type}\n\n"
        f"---\n\n{extracted}"
    )
    write_if_changed(extraction_path, extraction.encode("utf-8"))
    result = {
        "ok": True,
        "mode": "single",
        "input_url": args.url,
        "canonical_url": canonical,
        "final_url": canonicalize_url(final_url),
        "content_type": content_type,
        "raw_path": str(raw_path),
        "raw_sha256": sha256_file(raw_path),
        "extraction_path": str(extraction_path),
        "extraction_sha256": sha256_file(extraction_path),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    write_if_changed(
        metadata_path,
        (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    result["metadata_path"] = str(metadata_path)
    return result


def deterministic_zip(files: list[Path], source_root: Path, prefix: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix()):
            relative = path.relative_to(source_root).as_posix()
            info = zipfile.ZipInfo(f"{prefix.rstrip('/')}/{relative}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def tree_digest(files: list[Path], source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def build_merged_markdown(
    files: list[Path],
    source_root: Path,
    repo: str,
    ref: str,
    commit: str,
    commit_date: str,
    subpath: str,
    source_urls: list[str],
) -> str:
    markdown_files = [path for path in files if path.suffix.lower() in {".md", ".markdown"}]
    lines = [
        "# Obsidian 官方帮助：简体中文完整文本",
        "",
        f"- 规范仓库：https://github.com/{repo}",
        f"- 分支：`{ref}`",
        f"- 提交：`{commit}`",
        f"- 提交时间：`{commit_date}`",
        f"- 收录目录：`{subpath}`",
        f"- Markdown 文档：{len(markdown_files)}",
        f"- 全部文件：{len(files)}",
        "- 输入链接：",
    ]
    lines.extend(f"  - {url}" for url in source_urls)
    lines.extend(["", "---", ""])
    for path in sorted(markdown_files, key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix()
        text = path.read_text(encoding="utf-8-sig")
        lines.extend(
            [
                f"<!-- source-file: {subpath.rstrip('/')}/{relative} -->",
                "",
                f"## 来源文件：`{relative}`",
                "",
                text.rstrip(),
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def capture_github_subtree(args: argparse.Namespace) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise CaptureError("--repo must use owner/repository format")
    subpath = args.subpath.strip("/\\")
    if not subpath or ".." in Path(subpath).parts:
        raise CaptureError("--subpath must be a safe repository directory")
    source_urls = [canonicalize_url(value) for value in args.source_url]
    name = safe_name(args.name or f"{args.repo.replace('/', '-')}-{subpath}")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{name}.zip"
    extraction_path = output_dir / f"{name}.extracted.md"
    metadata_path = output_dir / f"{name}.capture.json"

    temp_root = Path(tempfile.mkdtemp(prefix="link-capture-"))
    repo_dir = temp_root / "repo"
    try:
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                args.ref,
                f"https://github.com/{args.repo}.git",
                str(repo_dir),
            ]
        )
        run(["git", "sparse-checkout", "set", "--no-cone", subpath], cwd=repo_dir)
        commit = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        commit_date = run(["git", "show", "-s", "--format=%cI", "HEAD"], cwd=repo_dir)
        source_root = repo_dir / subpath
        if not source_root.is_dir():
            raise CaptureError(f"Repository subpath not found: {subpath}")
        files = [path for path in source_root.rglob("*") if path.is_file()]
        if not files:
            raise CaptureError(f"Repository subpath is empty: {subpath}")
        markdown_files = [path for path in files if path.suffix.lower() in {".md", ".markdown"}]
        if not markdown_files:
            raise CaptureError(f"Repository subpath has no Markdown documents: {subpath}")

        deterministic_zip(files, source_root, subpath, archive_path)
        extraction = build_merged_markdown(
            files,
            source_root,
            args.repo,
            args.ref,
            commit,
            commit_date,
            subpath,
            source_urls,
        )
        write_if_changed(extraction_path, extraction.encode("utf-8"))
        result = {
            "ok": True,
            "mode": "github-subtree",
            "repo": args.repo,
            "ref": args.ref,
            "commit": commit,
            "commit_date": commit_date,
            "subpath": subpath,
            "source_urls": source_urls,
            "file_count": len(files),
            "markdown_count": len(markdown_files),
            "attachment_count": len(files) - len(markdown_files),
            "content_tree_sha256": tree_digest(files, source_root),
            "archive_path": str(archive_path),
            "archive_sha256": sha256_file(archive_path),
            "archive_bytes": archive_path.stat().st_size,
            "extraction_path": str(extraction_path),
            "extraction_sha256": sha256_file(extraction_path),
            "extraction_characters": len(extraction),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        write_if_changed(
            metadata_path,
            (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        result["metadata_path"] = str(metadata_path)
        return result
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("single")
    single.add_argument("--url", required=True)
    single.add_argument("--output-dir", required=True, type=Path)
    single.add_argument("--name")
    single.add_argument("--timeout", type=int, default=30)

    subtree = subparsers.add_parser("github-subtree")
    subtree.add_argument("--repo", required=True)
    subtree.add_argument("--ref", required=True)
    subtree.add_argument("--subpath", required=True)
    subtree.add_argument("--source-url", action="append", default=[])
    subtree.add_argument("--output-dir", required=True, type=Path)
    subtree.add_argument("--name")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = capture_single(args) if args.command == "single" else capture_github_subtree(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CaptureError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

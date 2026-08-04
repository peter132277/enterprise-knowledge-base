#!/usr/bin/env python3
"""Prepare one reviewed Obsidian enterprise note for Feishu publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


BLOCKED_SENSITIVITY = {"restricted", "personal-sensitive"}
ALLOWED_SCOPE = {"enterprise"}


def fail(message: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def split_frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not match:
        fail("笔记缺少有效的 YAML Frontmatter。")
    return match.group(1), text[match.end() :]


def scalar(frontmatter: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$", frontmatter)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def as_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1", "on"}


def normalize_heading(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def convert_obsidian(body: str, title: str) -> str:
    if re.search(r"!\[\[", body):
        fail("正文包含 Obsidian 本地附件嵌入；请先上传附件并替换引用。")
    if re.search(r"(?mi)^```dataview\b", body):
        fail("正文包含 Dataview 查询；请先静态化查询结果。")
    if re.search(r"!\[[^\]]*\]\((?!https?://)[^)]+\)", body):
        fail("正文包含本地 Markdown 图片；请先上传附件并替换引用。")

    lines = body.lstrip("\r\n").splitlines()
    if lines and lines[0].startswith("# "):
        first = lines[0][2:].strip()
        if normalize_heading(first) == normalize_heading(title):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines.pop(0)
    body = "\n".join(lines).strip() + "\n"

    def replace_link(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        if "|" in target:
            return target.split("|", 1)[1].strip()
        if "#" in target:
            target = target.split("#", 1)[0]
        return Path(target).stem

    return re.sub(r"\[\[([^\]]+)\]\]", replace_link, body)


def check_secrets(text: str) -> None:
    patterns = [
        r"(?i)\bsk-[a-z0-9_-]{20,}\b",
        r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|app[_ -]?secret)\s*[:=]\s*[\"']?[a-z0-9._-]{12,}",
        r"(?i)\b(?:password|passwd)\s*[:=]\s*[\"']?\S{8,}",
    ]
    if any(re.search(pattern, text) for pattern in patterns):
        fail("检测到疑似凭证或密钥，已阻止发布。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--note", required=True, help="Enterprise note path")
    parser.add_argument("--vault", default=".", help="Vault root; defaults to cwd")
    parser.add_argument(
        "--allow-pending",
        action="store_true",
        help="Allow a reviewed ready-to-publish note with publish_status pending",
    )
    args = parser.parse_args()

    vault = Path(args.vault).resolve()
    note = Path(args.note)
    if not note.is_absolute():
        note = vault / note
    note = note.resolve()

    if not within(note, vault):
        fail("笔记位于 Vault 之外。")
    if not note.is_file():
        fail("找不到待发布笔记。")

    text = note.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)

    title = scalar(frontmatter, "title")
    status = scalar(frontmatter, "status").lower()
    scope = scalar(frontmatter, "scope").lower()
    sensitivity = scalar(frontmatter, "sensitivity").lower()
    review_status = scalar(frontmatter, "review_status").lower()
    publish_status = scalar(frontmatter, "publish_status").lower()
    publish = as_bool(scalar(frontmatter, "publish_to_feishu"))
    note_type = scalar(frontmatter, "type").lower()
    managed_mirror = as_bool(scalar(frontmatter, "managed_mirror"))
    publication_excluded = as_bool(scalar(frontmatter, "publication_excluded"))
    parent_name = scalar(frontmatter, "feishu_parent_node_name")
    writer = scalar(frontmatter, "feishu_writer") or "lark-cli"

    if not title:
        fail("缺少 title。")
    if scope not in ALLOWED_SCOPE:
        fail("Only explicitly enterprise-scoped knowledge may be published.")
    if note_type == "company-mirror" or managed_mirror or publication_excluded:
        fail("Managed company mirror content is permanently excluded from publication.")
    if sensitivity in BLOCKED_SENSITIVITY:
        fail(f"敏感度 {sensitivity} 不允许发布到普通企业知识空间。")
    if review_status != "reviewed":
        fail("review_status 必须为 reviewed。")
    pending_allowed = (
        args.allow_pending
        and status == "ready-to-publish"
        and publish_status == "pending"
    )
    if not publish and not pending_allowed:
        fail("笔记未被批准进入发布准备。")
    if not parent_name:
        fail("缺少 feishu_parent_node_name。")
    if writer != "lark-cli":
        fail("该笔记的写入通道不是 lark-cli，不能自动切换。")

    mapping_path = vault / ".kb" / "mappings" / "feishu_nodes.json"
    if not mapping_path.is_file():
        fail("缺少飞书节点映射。")
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    space_name = str(mapping.get("space_name", "")).strip()
    space_id = str(mapping.get("space_id", "")).strip()
    if not space_name or not space_id:
        fail("飞书空间映射缺少已验证的空间名称或空间 ID。")
    matches = [
        node
        for node in mapping.get("nodes", [])
        if node.get("node_name") == parent_name and node.get("verified") is True
    ]
    if len(matches) != 1:
        fail("目标父节点不存在、未验证或映射不唯一。")
    parent = matches[0]

    payload = convert_obsidian(body, title)
    check_secrets(payload)
    if "\ufffd" in payload:
        fail("正文包含 Unicode 替换字符，可能存在编码损坏。")

    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .") or "enterprise-note"
    temp_dir = vault / ".kb" / "temp" / "feishu-publish"
    temp_dir.mkdir(parents=True, exist_ok=True)
    payload_path = temp_dir / f"{safe_name}-{payload_hash[:12]}.md"
    payload_path.write_text(payload, encoding="utf-8", newline="\n")

    result = {
        "ok": True,
        "note_path": str(note),
        "note_relative": str(note.relative_to(vault)),
        "title": title,
        "status": status,
        "scope": scope,
        "sensitivity": sensitivity,
        "review_status": review_status,
        "publish_status": publish_status,
        "writer": writer,
        "space_name": space_name,
        "space_id": space_id,
        "parent_node_name": parent_name,
        "parent_node_token": parent.get("node_token"),
        "payload_path": str(payload_path),
        "payload_relative": str(payload_path.relative_to(vault)),
        "payload_hash": payload_hash,
        "payload_bytes": len(payload.encode("utf-8")),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

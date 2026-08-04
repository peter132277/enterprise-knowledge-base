#!/usr/bin/env python3
"""Find enterprise notes eligible for the explicit batch-publish workflow."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def split_frontmatter(text: str) -> str | None:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    return match.group(1) if match else None


def scalar(frontmatter: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$", frontmatter)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=".", help="Vault root; defaults to cwd")
    args = parser.parse_args()

    vault = Path(args.vault).resolve()
    root = vault / "20_知识/企业"
    ready: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    published = 0

    if not root.is_dir():
        print(
            json.dumps(
                {
                    "ok": True,
                    "ready_count": 0,
                    "blocked_count": 0,
                    "published_count": 0,
                    "ready": [],
                    "blocked": [],
                },
                ensure_ascii=False,
            )
        )
        return

    for note in sorted(root.rglob("*.md")):
        try:
            text = note.read_text(encoding="utf-8")
        except UnicodeError:
            blocked.append({"path": str(note.relative_to(vault)), "reason": "encoding"})
            continue
        frontmatter = split_frontmatter(text)
        if frontmatter is None:
            continue

        status = scalar(frontmatter, "status").lower()
        publish_status = scalar(frontmatter, "publish_status").lower()
        scope = scalar(frontmatter, "scope").lower()
        note_type = scalar(frontmatter, "type").lower()
        managed_mirror = scalar(frontmatter, "managed_mirror").lower()
        publication_excluded = scalar(frontmatter, "publication_excluded").lower()
        review_status = scalar(frontmatter, "review_status").lower()
        sensitivity = scalar(frontmatter, "sensitivity").lower()

        if (
            scope != "enterprise"
            or note_type == "company-mirror"
            or managed_mirror == "true"
            or publication_excluded == "true"
        ):
            continue

        if status == "published" or publish_status == "published":
            published += 1
            continue
        if status != "ready-to-publish" or publish_status != "pending":
            continue

        title = scalar(frontmatter, "title")
        parent = scalar(frontmatter, "feishu_parent_node_name")
        reason = ""
        if review_status != "reviewed":
            reason = "review"
        elif sensitivity in {"restricted", "personal-sensitive"}:
            reason = "sensitivity"
        elif not title:
            reason = "title"
        elif not parent:
            reason = "destination"

        item = {
            "path": str(note.relative_to(vault)),
            "title": title,
            "parent": parent,
            "sensitivity": sensitivity,
            "source_path": scalar(frontmatter, "source_path"),
            "source_file": scalar(frontmatter, "source_file"),
            "source_hash": scalar(frontmatter, "source_hash"),
            "source_feishu_file_url": scalar(
                frontmatter, "source_feishu_file_url"
            ),
        }
        if reason:
            item["reason"] = reason
            blocked.append(item)
        else:
            ready.append(item)

    result = {
        "ok": True,
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "published_count": published,
        "ready": ready,
        "blocked": blocked,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate the project-local knowledge Vault after setup, copy, or move."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from query_current_vault import PROJECT_VAULT, find_obsidian_cli


PATH_KEYS = {
    "root",
    "inbox",
    "original_sources",
    "extracted_content",
    "personal_database",
    "enterprise_candidates",
    "atomic_notes",
    "indexes",
    "feishu_publish",
    "skills",
    "attachments",
    "logs",
    "archive",
    "config_file_path",
    "default_local_upload_directory",
    "default_local_publish_directory",
    "local_directory",
    "local_directories",
    "local_path",
    "source_path",
    "larkCliPath",
    "executable",
}
REQUIRED_LAYOUT = (
    "AGENTS.md",
    ".kb",
    "00_收件箱",
    "10_来源",
    "20_知识",
    "30_导航",
    "90_归档",
)


def is_machine_absolute(value: str) -> bool:
    text = value.strip().strip("\"'")
    if not text or "://" in text:
        return False
    return Path(text).is_absolute() or PureWindowsPath(text).is_absolute()


def escapes_vault(vault: Path, value: str) -> bool:
    text = value.strip().strip("\"'")
    if not text or "://" in text or is_machine_absolute(text):
        return False
    try:
        (vault / Path(text.replace("\\", "/"))).resolve().relative_to(vault)
        return False
    except ValueError:
        return True


def iter_json_paths(value: Any, parent_key: str = "") -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in PATH_KEYS:
                if isinstance(child, str):
                    paths.append((key, child))
                elif isinstance(child, list):
                    paths.extend(
                        (key, item) for item in child if isinstance(item, str)
                    )
            paths.extend(iter_json_paths(child, key))
    elif isinstance(value, list):
        for child in value:
            paths.extend(iter_json_paths(child, parent_key))
    return paths


def yaml_path_values(path: Path) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_]+):\s*(.*?)\s*$", line)
        if not match or match.group(1) not in PATH_KEYS:
            continue
        value = match.group(2).strip().strip("\"'")
        if value:
            values.append((match.group(1), value.replace("\\\\", "\\")))
    return values


def frontmatter_source_paths(vault: Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    knowledge = vault / "20_知识"
    if not knowledge.is_dir():
        return results
    for note in knowledge.rglob("*.md"):
        try:
            text = note.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if not text.startswith("---"):
            continue
        closing = text.find("\n---", 3)
        if closing < 0:
            continue
        match = re.search(r"(?m)^source_path:\s*(.*?)\s*$", text[: closing + 4])
        if match:
            value = match.group(1).strip().strip("\"'").replace("\\\\", "\\")
            results.append((note.relative_to(vault).as_posix(), value))
    return results


def active_path_values(vault: Path) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    yaml_files = (
        vault / ".kb/config/system.yaml",
        vault / ".kb/config/feishu.yaml",
        vault / ".kb/config/feishu-plugin.yaml",
    )
    for path in yaml_files:
        for key, value in yaml_path_values(path):
            found.append((path.relative_to(vault).as_posix(), key, value))

    json_files = (
        vault / ".kb/mappings/feishu_nodes.json",
        vault / ".kb/mappings/feishu_documents.json",
        vault / ".kb/state/processed_files.json",
        vault / ".obsidian/plugins/feishu-lark-cli-sync/data.json",
    )
    for path in json_files:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for key, value in iter_json_paths(payload):
            found.append((path.relative_to(vault).as_posix(), key, value))

    for relative, value in frontmatter_source_paths(vault):
        found.append((relative, "source_path", value))
    return found


def run_checks(
    vault: Path,
    mode: str,
    *,
    tool_lookup: Callable[[str], str | None] = shutil.which,
    obsidian_lookup: Callable[[], Path | None] = find_obsidian_cli,
) -> dict[str, Any]:
    vault = vault.resolve()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: list[dict[str, Any]] = []

    for relative in REQUIRED_LAYOUT:
        exists = (vault / relative).exists()
        checks.append({"name": f"layout:{relative}", "ok": exists})
        if not exists:
            errors.append({"code": "missing-layout", "path": relative})

    python_ok = sys.version_info >= (3, 10)
    checks.append(
        {
            "name": "python",
            "ok": python_ok,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
        }
    )
    if not python_ok:
        errors.append({"code": "python-version", "required": ">=3.10"})

    absolute_values = [
        {"file": file, "field": key, "value": value}
        for file, key, value in active_path_values(vault)
        if is_machine_absolute(value)
    ]
    checks.append(
        {
            "name": "portable-active-paths",
            "ok": not absolute_values,
            "count": len(absolute_values),
        }
    )
    for item in absolute_values:
        errors.append(
            {
                "code": "machine-absolute-path",
                "path": item["file"],
                "field": item["field"],
            }
        )
    escaping_values = [
        {"file": file, "field": key}
        for file, key, value in active_path_values(vault)
        if escapes_vault(vault, value)
    ]
    checks.append(
        {
            "name": "active-path-containment",
            "ok": not escaping_values,
            "count": len(escaping_values),
        }
    )
    for item in escaping_values:
        errors.append(
            {
                "code": "path-escapes-vault",
                "path": item["file"],
                "field": item["field"],
            }
        )

    rg_path = tool_lookup("rg")
    obsidian_path = obsidian_lookup()
    if mode in {"all", "query"}:
        checks.append(
            {
                "name": "query-backend",
                "ok": True,
                "obsidian_cli": bool(obsidian_path),
                "rg": bool(rg_path),
                "python_fallback": True,
            }
        )
        if not obsidian_path:
            warnings.append({"code": "obsidian-cli-unavailable", "fallback": "rg"})
        if not rg_path:
            warnings.append({"code": "rg-unavailable", "fallback": "python"})

    if mode in {"all", "publish"}:
        lark_path = tool_lookup("lark-cli")
        checks.append({"name": "lark-cli", "ok": bool(lark_path)})
        if not lark_path:
            errors.append({"code": "lark-cli-unavailable"})
        nodes = vault / ".kb/mappings/feishu_nodes.json"
        documents = vault / ".kb/mappings/feishu_documents.json"
        for name, path in (("feishu-nodes", nodes), ("feishu-documents", documents)):
            valid = False
            if path.is_file():
                try:
                    valid = isinstance(
                        json.loads(path.read_text(encoding="utf-8")),
                        dict,
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    valid = False
            checks.append({"name": name, "ok": valid})
            if not valid:
                errors.append({"code": "invalid-mapping", "path": str(path.name)})

    return {
        "ok": not errors,
        "schema": "kb-environment-check/v1",
        "vault": str(vault),
        "mode": mode,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("all", "query", "collect", "publish"),
        default="all",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = run_checks(PROJECT_VAULT, args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

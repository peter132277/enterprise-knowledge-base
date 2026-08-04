#!/usr/bin/env python3
"""Build and verify the deterministic marketplace ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = (".codex-plugin", "skills", "README.md", "INSTALL.md", "INSTALL_REQUIREMENTS.md")
FORBIDDEN_PARTS = {"tests", "__pycache__", ".pytest_cache", ".github"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"}
MAX_ZIP_BYTES = 250 * 1024


def included_files() -> list[Path]:
    files: list[Path] = []
    for name in TOP_LEVEL:
        path = ROOT / name
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            relative = candidate.relative_to(ROOT)
            if not candidate.is_file():
                continue
            if FORBIDDEN_PARTS.intersection(relative.parts):
                continue
            if candidate.suffix.lower() in FORBIDDEN_SUFFIXES:
                continue
            files.append(candidate)
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())


def build(output: Path) -> dict[str, Any]:
    files = included_files()
    skill_files = [
        path for path in files if path.name == "SKILL.md" and "skills" in path.parts
    ]
    if len(skill_files) != 1:
        raise RuntimeError("Distribution must contain exactly one visible Skill")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            manifest[relative] = hashlib.sha256(data).hexdigest()
    size = output.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise RuntimeError(f"Distribution exceeds 250KB: {size} bytes")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if names != sorted(names) or set(names) != set(manifest):
            raise RuntimeError("Distribution order or file set is invalid")
        for name, expected in manifest.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise RuntimeError(f"Distribution hash mismatch: {name}")
    return {
        "ok": True,
        "output": str(output),
        "bytes": size,
        "files": len(manifest),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "manifest": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

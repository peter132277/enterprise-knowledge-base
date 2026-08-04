#!/usr/bin/env python3
"""Discover a configured knowledge Vault without trusting the plugin path."""

from __future__ import annotations

import os
from pathlib import Path


class VaultContextError(RuntimeError):
    pass


def is_vault(path: Path) -> bool:
    return (path / "AGENTS.md").is_file() and (path / ".kb").is_dir()


def validate_vault(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not is_vault(resolved):
        raise VaultContextError(
            f"Not a configured enterprise knowledge Vault: {resolved}"
        )
    return resolved


def discover_vault(explicit: str | Path | None = None) -> Path:
    if explicit:
        return validate_vault(Path(explicit))

    configured = os.environ.get("KB_VAULT_ROOT", "").strip()
    if configured:
        return validate_vault(Path(configured))

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if is_vault(candidate):
            return candidate
    raise VaultContextError(
        "No configured enterprise knowledge Vault was found from the current project."
    )

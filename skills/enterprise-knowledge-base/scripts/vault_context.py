#!/usr/bin/env python3
"""Discover a configured knowledge Vault without trusting the plugin path."""

from __future__ import annotations

import os
from pathlib import Path

from kb_core import PROJECT_BINDING_SCHEMA, CoreError, canonical_path, load_json


class VaultContextError(RuntimeError):
    pass


def is_vault(path: Path) -> bool:
    return (path / "AGENTS.md").is_file() and (path / ".kb").is_dir()


def validate_project_binding(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    binding_path = resolved / ".kb/config/project-binding.json"
    try:
        binding = load_json(binding_path)
    except CoreError as exc:
        raise VaultContextError(
            f"The current project is not bound to an enterprise knowledge Vault: {resolved}"
        ) from exc
    expected = canonical_path(resolved)
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != PROJECT_BINDING_SCHEMA
        or binding.get("binding_mode") != "same-root"
        or binding.get("project_root") != expected
        or binding.get("vault_root") != expected
    ):
        raise VaultContextError(
            f"The Codex project and knowledge Vault binding is invalid: {resolved}"
        )
    return resolved


def validate_vault(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not is_vault(resolved):
        raise VaultContextError(
            f"Not a configured enterprise knowledge Vault: {resolved}"
        )
    return validate_project_binding(resolved)


def current_project_vault() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if is_vault(candidate):
            return validate_project_binding(candidate)
    raise VaultContextError(
        "No bound enterprise knowledge Vault was found from the current Codex project."
    )


def discover_vault(explicit: str | Path | None = None) -> Path:
    current = current_project_vault()
    configured = os.environ.get("KB_VAULT_ROOT", "").strip()
    if configured and canonical_path(Path(configured)) != canonical_path(current):
        raise VaultContextError(
            "KB_VAULT_ROOT cannot redirect knowledge operations outside the bound Codex project."
        )
    if explicit and canonical_path(Path(explicit)) != canonical_path(current):
        raise VaultContextError(
            "Knowledge operations are restricted to the Vault bound to the current Codex project."
        )
    return current

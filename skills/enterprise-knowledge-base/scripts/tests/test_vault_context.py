import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_context import VaultContextError, discover_vault


def make_vault(path: Path) -> None:
    (path / ".kb").mkdir(parents=True)
    (path / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    binding = path / ".kb/config/project-binding.json"
    binding.parent.mkdir(parents=True)
    resolved = os.path.normcase(str(path.resolve()))
    binding.write_text(
        json.dumps(
            {
                "schema": "kb-project-binding/v1",
                "binding_mode": "same-root",
                "project_root": resolved,
                "vault_root": resolved,
            }
        ),
        encoding="utf-8",
    )


class VaultContextTests(unittest.TestCase):
    def test_explicit_path_must_equal_current_bound_project(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            make_vault(vault)
            with patch("pathlib.Path.cwd", return_value=vault):
                self.assertEqual(discover_vault(vault), vault.resolve())

    def test_environment_cannot_redirect_outside_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / "current"
            other = Path(folder) / "other"
            make_vault(current)
            make_vault(other)
            with patch("pathlib.Path.cwd", return_value=current), patch.dict(
                os.environ, {"KB_VAULT_ROOT": str(other)}
            ):
                with self.assertRaises(VaultContextError):
                    discover_vault()

    def test_nearest_project_ancestor_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            nested = vault / "work/inside"
            make_vault(vault)
            nested.mkdir(parents=True)
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=nested):
                self.assertEqual(discover_vault(), vault.resolve())

    def test_explicit_other_bound_vault_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / "current"
            other = Path(folder) / "other"
            make_vault(current)
            make_vault(other)
            with patch("pathlib.Path.cwd", return_value=current):
                with self.assertRaises(VaultContextError):
                    discover_vault(other)

    def test_missing_binding_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            (vault / ".kb").mkdir(parents=True)
            (vault / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            with patch("pathlib.Path.cwd", return_value=vault):
                with self.assertRaises(VaultContextError):
                    discover_vault()

    def test_unconfigured_location_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=Path(folder)):
                with self.assertRaises(VaultContextError):
                    discover_vault()


if __name__ == "__main__":
    unittest.main()

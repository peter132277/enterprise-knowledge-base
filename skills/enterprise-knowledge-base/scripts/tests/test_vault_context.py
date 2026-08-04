import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_context import VaultContextError, discover_vault


def make_vault(path: Path) -> None:
    (path / ".kb").mkdir(parents=True)
    (path / "AGENTS.md").write_text("# rules\n", encoding="utf-8")


class VaultContextTests(unittest.TestCase):
    def test_explicit_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            make_vault(vault)
            self.assertEqual(discover_vault(vault), vault.resolve())

    def test_environment_supports_windows_and_macos_style_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "portable vault"
            make_vault(vault)
            with patch.dict(os.environ, {"KB_VAULT_ROOT": str(vault)}):
                self.assertEqual(discover_vault(), vault.resolve())

    def test_nearest_project_ancestor_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            nested = vault / "work/inside"
            make_vault(vault)
            nested.mkdir(parents=True)
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=nested):
                self.assertEqual(discover_vault(), vault.resolve())

    def test_unconfigured_location_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=Path(folder)):
                with self.assertRaises(VaultContextError):
                    discover_vault()


if __name__ == "__main__":
    unittest.main()

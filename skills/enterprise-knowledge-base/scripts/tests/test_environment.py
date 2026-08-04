import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "check_environment.py"
SPEC = importlib.util.spec_from_file_location("check_environment", SCRIPT)
ENV = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ENV)


class EnvironmentTests(unittest.TestCase):
    def make_vault(self, root: Path, absolute: bool = False) -> None:
        for relative in (
            ".kb/config",
            ".kb/mappings",
            ".kb/state",
            "00_收件箱",
            "10_来源/原件",
            "20_知识/企业",
            "30_导航",
            "90_归档",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        configured = (
            r"C:\old-vault\10_来源\原件"
            if absolute
            else "10_来源/原件"
        )
        (root / ".kb/config/system.yaml").write_text(
            f'vault:\n  root: "."\ndirectories:\n  original_sources: "{configured}"\n',
            encoding="utf-8",
        )
        (root / ".kb/config/feishu.yaml").write_text(
            'sync:\n  default_local_publish_directory: ".kb/publish"\n',
            encoding="utf-8",
        )
        (root / ".kb/config/feishu-plugin.yaml").write_text(
            'plugin:\n  config_file_path: ".obsidian/plugins/plugin/data.json"\n',
            encoding="utf-8",
        )
        (root / ".kb/mappings/feishu_nodes.json").write_text(
            '{"nodes": [{"local_directory": "20_知识/企业"}]}\n',
            encoding="utf-8",
        )
        (root / ".kb/mappings/feishu_documents.json").write_text(
            '{"documents": []}\n',
            encoding="utf-8",
        )
        (root / ".kb/state/processed_files.json").write_text(
            '{"files": [{"source_path": "10_来源/原件/source.txt"}]}\n',
            encoding="utf-8",
        )

    def test_project_root_is_resolved_at_runtime_not_import_time(self) -> None:
        self.assertFalse(hasattr(ENV, "PROJECT_VAULT"))
        self.assertTrue(callable(ENV.discover_vault))

    def test_portable_fixture_passes_all_modes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = ENV.run_checks(
                vault,
                "all",
                tool_lookup=lambda name: f"/tools/{name}",
                obsidian_lookup=lambda: Path("/tools/obsidian"),
            )
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(result["errors"], [])

    def test_machine_absolute_active_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault, absolute=True)
            result = ENV.run_checks(
                vault,
                "collect",
                tool_lookup=lambda name: f"/tools/{name}",
                obsidian_lookup=lambda: None,
            )
            self.assertFalse(result["ok"])
            self.assertTrue(
                any(
                    item["code"] == "machine-absolute-path"
                    for item in result["errors"]
                )
            )

    def test_parent_traversal_active_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            (vault / ".kb/config/feishu.yaml").write_text(
                'sync:\n  default_local_publish_directory: "../outside"\n',
                encoding="utf-8",
            )
            result = ENV.run_checks(
                vault,
                "collect",
                tool_lookup=lambda name: f"/tools/{name}",
                obsidian_lookup=lambda: None,
            )
            self.assertFalse(result["ok"])
            self.assertTrue(
                any(
                    item["code"] == "path-escapes-vault"
                    for item in result["errors"]
                )
            )

    def test_missing_optional_query_tools_only_warns(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = ENV.run_checks(
                vault,
                "query",
                tool_lookup=lambda name: None,
                obsidian_lookup=lambda: None,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["warnings"]), 2)


if __name__ == "__main__":
    unittest.main()

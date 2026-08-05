import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "tests/build_distribution.py"
SPEC = importlib.util.spec_from_file_location("build_distribution", SCRIPT)
PACKAGE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(PACKAGE)


class DistributionPackageTests(unittest.TestCase):
    def test_distribution_is_small_deterministic_and_excludes_dev_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.zip"
            second = Path(folder) / "second.zip"
            one = PACKAGE.build(first)
            two = PACKAGE.build(second)
            self.assertEqual(one["sha256"], two["sha256"])
            self.assertLessEqual(one["bytes"], 250 * 1024)
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
            self.assertEqual(sum(name.endswith("/SKILL.md") for name in names), 1)
            self.assertFalse(any("/tests/" in name for name in names))
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertFalse(any(name.startswith(".github/") for name in names))
            self.assertFalse(any(name.endswith((".db", ".sqlite", ".pyc")) for name in names))
            self.assertFalse(any(name == "README.md" or name.startswith("docs/") for name in names))
            self.assertEqual(
                {name.split("/", 1)[0] for name in names},
                {".codex-plugin", "assets", "skills"},
            )
            self.assertIn("assets/vault-template/AGENTS.md", names)
            self.assertIn(
                "skills/enterprise-knowledge-base/assets/wiki-template.json", names
            )

    def test_extracted_distribution_can_initialize_a_new_vault(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "plugin.zip"
            extracted = root / "plugin"
            vault = root / "知识库"
            vault.mkdir()
            PACKAGE.build(package)
            with zipfile.ZipFile(package) as archive:
                archive.extractall(extracted)
            setup = extracted / "skills/enterprise-knowledge-base/scripts/setup_wizard.py"
            run = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(setup),
                    "initialize-current-project",
                    "--role",
                    "local",
                    "--yes",
                ],
                cwd=vault,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            result = json.loads(run.stdout)
            self.assertTrue(result["configured"])
            self.assertTrue((vault / "AGENTS.md").is_file())
            self.assertTrue(result["project_binding"]["project_specific"])


if __name__ == "__main__":
    unittest.main()

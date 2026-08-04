import importlib.util
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


if __name__ == "__main__":
    unittest.main()

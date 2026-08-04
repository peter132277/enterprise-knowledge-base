import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT = Path(__file__).resolve().parents[1] / "capture_link.py"
SPEC = importlib.util.spec_from_file_location("capture_link", SCRIPT)
CAPTURE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(CAPTURE)


class CaptureLinkTests(unittest.TestCase):
    def test_canonicalize_removes_tracking_and_fragment(self) -> None:
        value = (
            "[docs](https://github.com/obsidianmd/obsidian-help"
            "?utm_source=chatgpt.com&x=1#readme)"
        )
        self.assertEqual(
            CAPTURE.canonicalize_url(value),
            "https://github.com/obsidianmd/obsidian-help?x=1",
        )

    def test_deterministic_zip_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            root.mkdir()
            (root / "b.md").write_text("B", encoding="utf-8")
            (root / "a.md").write_text("A", encoding="utf-8")
            files = [root / "b.md", root / "a.md"]
            first = Path(folder) / "first.zip"
            second = Path(folder) / "second.zip"
            CAPTURE.deterministic_zip(files, root, "zh", first)
            CAPTURE.deterministic_zip(list(reversed(files)), root, "zh", second)
            self.assertEqual(CAPTURE.sha256_file(first), CAPTURE.sha256_file(second))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["zh/a.md", "zh/b.md"])

    def test_merged_markdown_preserves_source_markers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "首页.md").write_text("# 首页\n正文", encoding="utf-8")
            merged = CAPTURE.build_merged_markdown(
                [root / "首页.md"],
                root,
                "owner/repo",
                "main",
                "abc123",
                "2026-07-28T00:00:00Z",
                "zh",
                ["https://example.com/"],
            )
            self.assertIn("<!-- source-file: zh/首页.md -->", merged)
            self.assertIn("# 首页", merged)


if __name__ == "__main__":
    unittest.main()

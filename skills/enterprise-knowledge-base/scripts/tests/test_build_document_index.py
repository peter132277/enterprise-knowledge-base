import importlib.util
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT = Path(__file__).resolve().parents[1] / "build_document_index.py"
SPEC = importlib.util.spec_from_file_location("build_document_index", SCRIPT)
INDEX = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(INDEX)


class BuildDocumentIndexTests(unittest.TestCase):
    def test_builds_continuous_heading_index(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            extraction = vault / "10_来源/提取/文档正文/正文.md"
            extraction.parent.mkdir(parents=True)
            extraction.write_text(
                "# 正文\n\n## 001｜第一条\n\n内容\n\n## 002｜第二条\n\n内容\n",
                encoding="utf-8",
            )
            args = Namespace(
                vault=vault,
                extraction="10_来源/提取/文档正文/正文.md",
                output="10_来源/提取/文档索引/索引.md",
                title="条目索引",
                source_sha256="a" * 64,
                expected_count=2,
                write=True,
            )
            result = INDEX.run(args)
            self.assertEqual(result["entry_count"], 2)
            output = vault / "10_来源/提取/文档索引/索引.md"
            text = output.read_text(encoding="utf-8")
            self.assertIn("[[10_来源/提取/文档正文/正文#001｜第一条|001｜第一条]]", text)
            self.assertIn("standalone-collection", text)

    def test_rejects_numbering_gap(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            extraction = vault / "10_来源/提取/正文.md"
            extraction.parent.mkdir(parents=True)
            extraction.write_text(
                "## 001｜第一条\n\n## 003｜第三条\n", encoding="utf-8"
            )
            args = Namespace(
                vault=vault,
                extraction="10_来源/提取/正文.md",
                output="10_来源/提取/索引.md",
                title="索引",
                source_sha256="b" * 64,
                expected_count=2,
                write=False,
            )
            with self.assertRaises(INDEX.IndexError):
                INDEX.run(args)


if __name__ == "__main__":
    unittest.main()

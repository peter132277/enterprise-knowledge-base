import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "split_document_collection.py"
SPEC = importlib.util.spec_from_file_location("split_document_collection", SCRIPT)
SPLIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SPLIT)


class SplitDocumentCollectionTests(unittest.TestCase):
    def make_args(self, vault: Path, write: bool = True) -> Namespace:
        return Namespace(
            vault=vault,
            extraction="10_来源/提取/文档正文/资料.md",
            index="10_来源/提取/文档索引/资料_索引.md",
            chunk_root="10_来源/提取/文档正文/资料",
            evidence=".kb/evidence/document-collections/" + "a" * 64 + "/full.md",
            title="资料",
            source_sha256="a" * 64,
            native_unit_type="transcript",
            expected_count=3,
            chunk_size=2,
            write=write,
        )

    def test_splits_and_links_directly_to_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            extraction = vault / "10_来源/提取/文档正文/资料.md"
            extraction.parent.mkdir(parents=True)
            extraction.write_text(
                "# 完整正文\n\n"
                "## 001｜第一条\n\n内容一\n\n"
                "## 002｜第二条\n\n内容二\n\n"
                "## 003｜第三条\n\n内容三\n",
                encoding="utf-8",
            )
            result = SPLIT.run(self.make_args(vault))
            self.assertEqual(result["chunk_count"], 2)
            self.assertEqual(result["item_count"], 3)
            landing = extraction.read_text(encoding="utf-8")
            self.assertNotIn("内容一", landing)
            self.assertIn("[[10_来源/提取/文档正文/资料/001-002", landing)
            index = (
                vault / "10_来源/提取/文档索引/资料_索引.md"
            ).read_text(encoding="utf-8")
            self.assertIn(
                "[[10_来源/提取/文档正文/资料/001-002#001｜第一条|001｜第一条]]",
                index,
            )
            self.assertTrue(
                (
                    vault
                    / ".kb/evidence/document-collections"
                    / ("a" * 64)
                    / "full.md"
                ).is_file()
            )
            state = json.loads(
                (vault / ".kb/state/document_collections.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["items"][0]["native_unit_count"], 3)

    def test_rejects_numbering_gap(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            extraction = vault / "10_来源/提取/文档正文/资料.md"
            extraction.parent.mkdir(parents=True)
            extraction.write_text(
                "## 001｜第一条\n\n## 003｜第三条\n", encoding="utf-8"
            )
            args = self.make_args(vault, write=False)
            args.expected_count = 2
            with self.assertRaises(SPLIT.CollectionError):
                SPLIT.run(args)


if __name__ == "__main__":
    unittest.main()

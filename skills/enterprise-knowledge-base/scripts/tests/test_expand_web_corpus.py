import importlib.util
import json
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "expand_web_corpus.py"
SPEC = importlib.util.spec_from_file_location("expand_web_corpus", SCRIPT)
EXPAND = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(EXPAND)


class ExpandWebCorpusTests(unittest.TestCase):
    def make_args(self, vault: Path, digest: str, tree_hash: str, write: bool) -> Namespace:
        return Namespace(
            vault=vault,
            archive="10_来源/原件/网页/docs.zip",
            archive_sha256=digest,
            source_prefix="zh",
            target="10_来源/提取/网页/官方文档",
            landing="10_来源/提取/网页/官方文档索引.md",
            title="官方文档",
            expected_tree_sha256=tree_hash,
            write=write,
            replace=False,
        )

    def test_preview_and_write_preserve_individual_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            archive_path = vault / "10_来源/原件/网页/docs.zip"
            archive_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "zh/由此开始.md",
                    "# 由此开始\r\n\r\n[[指南/安装]]\r\n".encode("utf-8"),
                )
                archive.writestr("zh/指南/安装.md", "# 安装\n")
                archive.writestr("zh/附件/图片.png", b"PNG")
            with zipfile.ZipFile(archive_path) as archive:
                entries = EXPAND.safe_archive_entries(archive, "zh")
            tree_hash = EXPAND.tree_digest(entries)
            digest = EXPAND.sha256_file(archive_path)

            preview = EXPAND.expand(self.make_args(vault, digest, tree_hash, False))
            self.assertFalse(preview["write"])
            self.assertEqual(preview["markdown_count"], 2)
            self.assertFalse((vault / "10_来源/提取/网页/官方文档").exists())

            result = EXPAND.expand(self.make_args(vault, digest, tree_hash, True))
            self.assertTrue(result["ok"])
            self.assertEqual(
                (vault / "10_来源/提取/网页/官方文档/指南/安装.md").read_text(
                    encoding="utf-8"
                ),
                "# 安装\n",
            )
            landing = (vault / "10_来源/提取/网页/官方文档索引.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("[[10_来源/提取/网页/官方文档/指南/安装|安装]]", landing)
            state = json.loads(
                (vault / ".kb/state/web_corpora.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["items"][0]["file_count"], 3)

            start = vault / "10_来源/提取/网页/官方文档/由此开始.md"
            start.write_bytes(start.read_bytes().replace(b"\r\n", b"\n"))
            current = EXPAND.expand(self.make_args(vault, digest, tree_hash, True))
            self.assertTrue(current["already_current"])

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("zh/../escape.md", "# bad")
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(EXPAND.CorpusError):
                    EXPAND.safe_archive_entries(archive, "zh")


if __name__ == "__main__":
    unittest.main()

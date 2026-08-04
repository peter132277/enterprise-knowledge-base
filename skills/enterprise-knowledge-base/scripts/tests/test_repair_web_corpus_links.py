import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT = Path(__file__).resolve().parents[1] / "repair_web_corpus_links.py"
SPEC = importlib.util.spec_from_file_location("repair_web_corpus_links", SCRIPT)
REPAIR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(REPAIR)


class RepairWebCorpusLinksTests(unittest.TestCase):
    def test_repairs_mapped_links_and_moves_empty_stub(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            root = vault / "10_来源/提取/网页/语料"
            root.mkdir(parents=True)
            source = root / "来源.md"
            source.write_text(
                "# 来源\n\n[[旧标题|显示]] [[重名]] [[重名]] [[Example]]\n",
                encoding="utf-8",
            )
            (root / "目标.md").write_text("# 目标\n", encoding="utf-8")
            (root / "一").mkdir()
            (root / "一/重名.md").write_text("# 一\n", encoding="utf-8")
            (root / "二").mkdir()
            (root / "二/重名.md").write_text("# 二\n", encoding="utf-8")
            inbox = vault / "00_收件箱"
            inbox.mkdir()
            (inbox / "旧标题.md").write_bytes(b"")
            state_dir = vault / ".kb/state"
            state_dir.mkdir(parents=True)

            entries = []
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                data = path.read_bytes()
                entries.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": REPAIR.sha256_file(path),
                        "normalized_sha256": REPAIR.normalized_markdown_sha256(data),
                        "size_bytes": len(data),
                    }
                )
            (state_dir / "web_corpora.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [
                            {
                                "corpus_root": "10_来源/提取/网页/语料",
                                "entries": entries,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            rules_path = vault / ".kb/rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "corpus_root": "10_来源/提取/网页/语料",
                        "direct_targets": {"旧标题": "目标.md"},
                        "context_targets": [
                            {
                                "source": "来源.md",
                                "target": "重名",
                                "occurrence": 1,
                                "replacement": "一/重名.md",
                            },
                            {
                                "source": "来源.md",
                                "target": "重名",
                                "occurrence": 2,
                                "replacement": "二/重名.md",
                            },
                        ],
                        "intentional_unresolved": [
                            {
                                "source": "来源.md",
                                "target": "Example",
                                "reason": "example",
                            }
                        ],
                        "empty_inbox_stubs": ["旧标题.md"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            args = Namespace(vault=vault, rules=rules_path, write=False)
            preview = REPAIR.run(args)
            self.assertEqual(preview["links_repaired"], 3)
            self.assertEqual(preview["empty_stubs_to_remove"], 1)
            self.assertEqual(preview["intentional_example_links"], 1)

            args.write = True
            result = REPAIR.run(args)
            self.assertTrue(result["ok"])
            repaired = source.read_text(encoding="utf-8")
            self.assertIn(
                "[[10_来源/提取/网页/语料/目标|显示]]",
                repaired,
            )
            self.assertIn("[[10_来源/提取/网页/语料/一/重名]]", repaired)
            self.assertIn("[[10_来源/提取/网页/语料/二/重名]]", repaired)
            self.assertIn("[[Example]]", repaired)
            self.assertFalse((inbox / "旧标题.md").exists())
            state = json.loads(
                (state_dir / "web_corpora.json").read_text(encoding="utf-8")
            )
            source_entry = next(
                entry
                for entry in state["items"][0]["entries"]
                if entry["path"] == "来源.md"
            )
            self.assertEqual(source_entry["adapted_sha256"], REPAIR.sha256_file(source))
            self.assertEqual(
                source_entry["adapted_normalized_sha256"],
                REPAIR.normalized_markdown_sha256(source.read_bytes()),
            )

    def test_replaces_empty_corpus_stub_with_explicit_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            root = vault / "10_来源/提取/网页/语料"
            attachments = root / "Attachments"
            attachments.mkdir(parents=True)
            source = root / "来源.md"
            source.write_text(
                "![[10_来源/提取/网页/语料/Attachments/image|400]]\n",
                encoding="utf-8",
            )
            asset = attachments / "image.png"
            asset.write_bytes(b"png")
            stub = attachments / "image.md"
            stub.write_bytes(b"")
            state_dir = vault / ".kb/state"
            state_dir.mkdir(parents=True)
            entries = []
            for path in (source, asset):
                entry = {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": REPAIR.sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
                if path.suffix == ".md":
                    entry["normalized_sha256"] = REPAIR.normalized_markdown_sha256(
                        path.read_bytes()
                    )
                entries.append(entry)
            (state_dir / "web_corpora.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [
                            {
                                "corpus_root": "10_来源/提取/网页/语料",
                                "entries": entries,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            rules_path = vault / ".kb/rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "corpus_root": "10_来源/提取/网页/语料",
                        "normalize_all_resolved": True,
                        "auto_empty_attachment_shadow_stubs": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            args = Namespace(vault=vault, rules=rules_path, write=False)
            preview = REPAIR.run(args)
            self.assertEqual(preview["links_repaired"], 1)
            self.assertEqual(preview["empty_stubs_to_remove"], 1)

            args.write = True
            result = REPAIR.run(args)
            self.assertTrue(result["ok"])
            self.assertFalse(stub.exists())
            self.assertIn(
                "![[10_来源/提取/网页/语料/Attachments/image.png|400]]",
                source.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "check_vault_health.py"
sys.path.insert(0, str(SCRIPT.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
SPEC = importlib.util.spec_from_file_location("check_vault_health", SCRIPT)
HEALTH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(HEALTH)

import company_sync_coordinator as coordinator
from company_test_support import FakeKnowledgeReader, configure_admin, configure_employee


class CheckVaultHealthTests(unittest.TestCase):
    def test_company_health_performs_strict_full_hash_validation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            coordinator.explicit_sync(employee, FakeKnowledgeReader())
            healthy = HEALTH.check_company_sync(employee)
            self.assertEqual(healthy["records"], 1)
            self.assertEqual(healthy["errors"], [])
            mirror = employee / coordinator.MIRROR_ROOT / "node-1.md"
            mirror.write_text(mirror.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
            edited = HEALTH.check_company_sync(employee)
            self.assertEqual(edited["records"], 0)
            self.assertTrue(edited["errors"])

    def test_external_web_corpus_links_are_not_local_link_errors(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            for relative in (
                "00_收件箱",
                "10_来源/原件/网页",
                "10_来源/提取/网页",
                "20_知识/个人",
                "30_导航/主题/个人主题",
                "90_归档",
                ".kb/state",
            ):
                (vault / relative).mkdir(parents=True, exist_ok=True)
            (vault / "10_来源/提取/网页/外部语料.md").write_text(
                "# 外部语料\n\n[[外部仓库内部链接]]\n", encoding="utf-8"
            )
            (vault / "20_知识/个人/入口.md").write_text(
                "---\ntype: atomic-note\nscope: personal\n---\n# 入口\n",
                encoding="utf-8",
            )
            (vault / "30_导航/主题/个人主题/索引.md").write_text(
                "# 索引\n\n- [[20_知识/个人/入口]]\n"
                "- [[10_来源/提取/网页/外部语料]]\n",
                encoding="utf-8",
            )
            (vault / ".kb/state/processed_files.json").write_text(
                json.dumps({"version": 2, "files": [], "by_sha256": {}}),
                encoding="utf-8",
            )
            (vault / ".kb/state/publish_queue.json").write_text(
                json.dumps({"version": 2, "items": []}),
                encoding="utf-8",
            )
            result = HEALTH.check(vault)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["broken_links"], [])
            self.assertEqual(result["skipped_external_corpus_files"], 1)
            self.assertEqual(result["skipped_external_corpus_links"], 1)

    def test_web_corpus_manifest_verifies_files_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            for relative in (
                "00_收件箱",
                "10_来源/原件/网页",
                "10_来源/提取/网页/官方文档",
                "20_知识/个人",
                "30_导航/主题/个人主题",
                "90_归档",
                ".kb/state",
            ):
                (vault / relative).mkdir(parents=True, exist_ok=True)
            archive = vault / "10_来源/原件/网页/docs.zip"
            archive.write_bytes(b"archive")
            document = vault / "10_来源/提取/网页/官方文档/首页.md"
            document.write_text("# 首页\n", encoding="utf-8")
            landing = vault / "10_来源/提取/网页/官方文档索引.md"
            landing.write_text("# 索引\n", encoding="utf-8")
            file_hash = HEALTH.sha256(document)
            normalized_hash = HEALTH.normalized_markdown_sha256(document)
            tree = hashlib.sha256()
            tree.update("首页.md".encode("utf-8"))
            tree.update(b"\0")
            tree.update(bytes.fromhex(file_hash))
            tree.update(b"\0")
            (vault / ".kb/state/web_corpora.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [
                            {
                                "archive": "10_来源/原件/网页/docs.zip",
                                "archive_sha256": HEALTH.sha256(archive),
                                "corpus_root": "10_来源/提取/网页/官方文档",
                                "landing": "10_来源/提取/网页/官方文档索引.md",
                                "content_tree_sha256": tree.hexdigest(),
                                "file_count": 1,
                                "markdown_count": 1,
                                "attachment_count": 0,
                                "entries": [
                                    {
                                        "path": "首页.md",
                                        "sha256": file_hash,
                                        "normalized_sha256": normalized_hash,
                                        "size_bytes": document.stat().st_size,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (vault / "20_知识/个人/入口.md").write_text(
                "---\ntype: atomic-note\nscope: personal\n---\n# 入口\n",
                encoding="utf-8",
            )
            (vault / "30_导航/主题/个人主题/索引.md").write_text(
                "# 索引\n\n- [[20_知识/个人/入口]]\n", encoding="utf-8"
            )
            (vault / ".kb/state/processed_files.json").write_text(
                json.dumps({"version": 2, "files": [], "by_sha256": {}}),
                encoding="utf-8",
            )
            (vault / ".kb/state/publish_queue.json").write_text(
                json.dumps({"version": 2, "items": []}),
                encoding="utf-8",
            )
            result = HEALTH.check(vault)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["web_corpus_count"], 1)
            self.assertEqual(result["web_corpus_markdown"], 1)
            self.assertEqual(result["web_corpus_errors"], [])

            document.write_text("# 已适配首页\n", encoding="utf-8")
            state_path = vault / ".kb/state/web_corpora.json"
            adapted_state = json.loads(state_path.read_text(encoding="utf-8"))
            adapted_state["items"][0]["entries"][0]["adapted_sha256"] = HEALTH.sha256(
                document
            )
            adapted_state["items"][0]["entries"][0]["adapted_normalized_sha256"] = (
                HEALTH.normalized_markdown_sha256(document)
            )
            state_path.write_text(
                json.dumps(adapted_state, ensure_ascii=False), encoding="utf-8"
            )
            adapted_result = HEALTH.check(vault)
            self.assertTrue(adapted_result["ok"])
            self.assertEqual(adapted_result["web_corpus_errors"], [])

            untracked_stub = (
                vault / "10_来源/提取/网页/官方文档/Attachments/image.md"
            )
            untracked_stub.parent.mkdir()
            untracked_stub.write_bytes(b"")
            untracked_result = HEALTH.check(vault)
            self.assertFalse(untracked_result["ok"])
            self.assertIn(
                "10_来源/提取/网页/官方文档/Attachments/image.md",
                untracked_result["empty_markdown_files"],
            )
            self.assertEqual(
                untracked_result["web_corpus_errors"][0]["reason"],
                "untracked-empty-markdown",
            )

    def test_document_collection_manifest_verifies_continuous_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            for relative in (
                "00_收件箱",
                "10_来源/提取/文档正文/资料",
                "10_来源/提取/文档索引",
                "20_知识/个人",
                "30_导航/主题/个人主题",
                "90_归档",
                ".kb/state",
                ".kb/evidence/document-collections/source",
            ):
                (vault / relative).mkdir(parents=True, exist_ok=True)
            evidence = (
                vault / ".kb/evidence/document-collections/source/full-extraction.md"
            )
            evidence.write_text(
                "## 001｜第一条\n\n内容\n\n## 002｜第二条\n\n内容\n",
                encoding="utf-8",
            )
            landing = vault / "10_来源/提取/文档正文/资料.md"
            landing.write_text("# 分卷入口\n", encoding="utf-8")
            index = vault / "10_来源/提取/文档索引/资料.md"
            index.write_text("# 索引\n", encoding="utf-8")
            chunk = vault / "10_来源/提取/文档正文/资料/001-002.md"
            chunk.write_text(
                "# 分卷\n\n## 001｜第一条\n\n内容\n\n## 002｜第二条\n\n内容\n",
                encoding="utf-8",
            )
            (vault / ".kb/state/document_collections.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [
                            {
                                "source_sha256": "a" * 64,
                                "evidence": (
                                    ".kb/evidence/document-collections/source/"
                                    "full-extraction.md"
                                ),
                                "evidence_sha256": HEALTH.sha256(evidence),
                                "landing": "10_来源/提取/文档正文/资料.md",
                                "landing_sha256": HEALTH.sha256(landing),
                                "index": "10_来源/提取/文档索引/资料.md",
                                "index_sha256": HEALTH.sha256(index),
                                "chunk_root": "10_来源/提取/文档正文/资料",
                                "native_unit_count": 2,
                                "chunk_count": 1,
                                "chunks": [
                                    {
                                        "path": (
                                            "10_来源/提取/文档正文/资料/001-002.md"
                                        ),
                                        "start": "001",
                                        "end": "002",
                                        "item_count": 2,
                                        "sha256": HEALTH.sha256(chunk),
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (vault / "20_知识/个人/入口.md").write_text(
                "---\ntype: atomic-note\nscope: personal\n---\n# 入口\n",
                encoding="utf-8",
            )
            (vault / "30_导航/主题/个人主题/索引.md").write_text(
                "# 索引\n\n- [[20_知识/个人/入口]]\n", encoding="utf-8"
            )
            (vault / ".kb/state/processed_files.json").write_text(
                json.dumps({"version": 2, "files": [], "by_sha256": {}}),
                encoding="utf-8",
            )
            (vault / ".kb/state/publish_queue.json").write_text(
                json.dumps({"version": 2, "items": []}), encoding="utf-8"
            )
            result = HEALTH.check(vault)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["document_collection_count"], 1)
            self.assertEqual(result["document_collection_chunks"], 1)
            self.assertEqual(result["document_collection_items"], 2)
            self.assertEqual(result["document_collection_errors"], [])


if __name__ == "__main__":
    unittest.main()

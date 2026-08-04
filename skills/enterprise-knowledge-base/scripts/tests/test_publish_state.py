import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT = Path(__file__).resolve().parents[1] / "publish_state.py"
SPEC = importlib.util.spec_from_file_location("publish_state", SCRIPT)
STATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(STATE)


def note(**fields: str) -> str:
    body = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{body}\n---\n\n# 内容\n"


class PublishStateTests(unittest.TestCase):
    def make_vault(self, root: Path) -> tuple[Path, Path]:
        enterprise = root / "20_知识/企业"
        atomic = root / "20_知识/原子/方法"
        state = root / ".kb/state"
        mappings = root / ".kb/mappings"
        enterprise.mkdir(parents=True)
        atomic.mkdir(parents=True)
        state.mkdir(parents=True)
        mappings.mkdir(parents=True)

        legacy = enterprise / "legacy.md"
        legacy.write_text(
            note(
                title="历史已发布",
                type="source-note",
                scope="enterprise",
                status="published",
                review_status="reviewed",
                sensitivity="internal",
                publish_to_feishu="true",
                feishu_url='"https://example.feishu.cn/wiki/legacy"',
            ),
            encoding="utf-8",
        )
        pending = enterprise / "pending.md"
        pending.write_text(
            note(
                title="待发布",
                type="source-note",
                scope="enterprise",
                status="ready-to-publish",
                review_status="reviewed",
                sensitivity="internal",
                publish_status="pending",
                publish_to_feishu="false",
            ),
            encoding="utf-8",
        )
        (atomic / "local.md").write_text(
            note(
                title="本地原子知识",
                type="method",
                scope="enterprise",
                status="reviewed",
                publish_to_feishu="false",
            ),
            encoding="utf-8",
        )
        (state / "publish_queue.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "items": [
                        {
                            "local_path": "20_知识/企业/pending.md",
                            "sync_status": "pending",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (mappings / "feishu_documents.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "documents": [
                        {
                            "local_path": "20_知识/企业/legacy.md",
                            "feishu_url": "https://example.feishu.cn/wiki/legacy",
                            "sync_status": "success",
                            "verified_content": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return legacy, pending

    def test_audit_distinguishes_publication_sources_from_local_notes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = STATE.audit(vault)
            self.assertFalse(result["ok"])
            self.assertEqual(result["knowledge_notes"], 3)
            self.assertEqual(result["publication_source_notes"], 2)
            self.assertEqual(result["published"], 1)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(result["not_applicable"], 1)
            self.assertIn(
                {
                    "path": "20_知识/企业/legacy.md",
                    "action": "set_publish_status_published",
                },
                result["repairable"],
            )

    def test_repair_previews_then_backs_up_and_normalizes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            legacy, _ = self.make_vault(vault)
            preview = STATE.reconcile(vault)
            self.assertTrue(preview["ok"])
            self.assertEqual(preview["changed"], 0)
            self.assertNotIn(
                "publish_status: published",
                legacy.read_text(encoding="utf-8"),
            )

            applied = STATE.reconcile(vault, write=True)
            self.assertTrue(applied["ok"])
            self.assertGreater(applied["changed"], 0)
            self.assertIn(
                "publish_status: published",
                legacy.read_text(encoding="utf-8"),
            )
            self.assertTrue((vault / applied["backup"]).is_dir())

    def test_successful_batch_requires_local_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            _, pending = self.make_vault(vault)
            batch = {
                "batch_id": "kbp-test",
                "items": [
                    {
                        "item_id": "one",
                        "note_relative": pending.relative_to(vault).as_posix(),
                        "state": "success",
                    }
                ],
            }
            (vault / ".kb/state/publish_batch_kbp-test.json").write_text(
                json.dumps(batch, ensure_ascii=False),
                encoding="utf-8",
            )
            result = STATE.audit(vault)
            self.assertIn(
                "successful_batch_not_locally_finalized",
                {error["reason"] for error in result["errors"]},
            )


if __name__ == "__main__":
    unittest.main()

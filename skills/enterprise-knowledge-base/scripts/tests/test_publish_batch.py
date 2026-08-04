import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "publish_batch.py"
SPEC = importlib.util.spec_from_file_location("publish_batch", SCRIPT)
BATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(BATCH)


class PublishBatchTests(unittest.TestCase):
    def make_vault(self, root: Path, count: int = 1) -> None:
        notes = root / "20_知识/企业"
        sources = root / "10_来源/原件/文档"
        mappings = root / ".kb/mappings"
        state = root / ".kb/state"
        logs = root / ".kb/logs/收录"
        notes.mkdir(parents=True)
        sources.mkdir(parents=True)
        mappings.mkdir(parents=True)
        state.mkdir(parents=True)
        logs.mkdir(parents=True)
        (mappings / "feishu_nodes.json").write_text(
            json.dumps(
                {
                    "space_name": "测试空间",
                    "space_id": "space",
                    "nodes": [
                        {
                            "node_name": "03｜流程与交付",
                            "node_token": "parent-token",
                            "verified": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (mappings / "feishu_documents.json").write_text(
            '{"version": 2, "documents": []}\n',
            encoding="utf-8",
        )
        queue_items = []
        processed_records = []
        for index in range(count):
            source = sources / f"source-{index}.txt"
            source.write_text(f"source {index}\n", encoding="utf-8")
            source_hash = BATCH.sha256_file(source)
            note = notes / f"note-{index}.md"
            note.write_text(
                f"""---
title: 测试知识 {index}
status: ready-to-publish
scope: enterprise
sensitivity: internal
review_status: reviewed
publish_status: pending
publish_to_feishu: false
feishu_writer: lark-cli
feishu_parent_node_name: 03｜流程与交付
source_file: "source-{index}.txt"
source_path: "{source}"
source_hash: "{source_hash}"
---

# 测试知识 {index}

正文 {index}
""",
                encoding="utf-8",
            )
            relative_note = note.relative_to(root).as_posix()
            queue_items.append(
                {
                    "local_path": relative_note,
                    "sync_status": "pending",
                    "source_hash": source_hash,
                }
            )
            processed_records.append(
                {
                    "sha256": source_hash,
                    "source_name": f"source-{index}.txt",
                    "stored_original": source.relative_to(root).as_posix(),
                    "status": "complete",
                }
            )
            (logs / f"source-{index}.md").write_text(
                f"""---
source_id: "sha256:{source_hash}"
---

# 收录日志

- 飞书操作：未执行。
""",
                encoding="utf-8",
            )
        (state / "publish_queue.json").write_text(
            json.dumps(
                {"version": 2, "items": queue_items},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (state / "processed_files.json").write_text(
            json.dumps({"version": 2, "files": processed_records}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_preview_persists_content_locked_batch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = BATCH.create_preview(
                vault,
                created_at="2026-07-29T13:00:00+08:00",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["preview"]["items"]), 1)
            self.assertNotIn("batch_id", result)
            self.assertNotIn("manifest_relative", result)
            self.assertNotIn("item_id", result["preview"]["items"][0])
            manifest = BATCH.load_json(vault / result["_internal"]["manifest_relative"])
            self.assertEqual(manifest["state"], "previewed")
            self.assertEqual(manifest["items"][0]["local_intention"], "create")
            self.assertTrue(manifest["items"][0]["note_hash"])
            self.assertTrue(manifest["items"][0]["source_hash"])
            self.assertEqual(manifest["items"][0]["source_display_name"], "source-0.txt")
            self.assertTrue(manifest["items"][0]["payload_hash"])
            self.assertEqual(manifest["space_name"], "测试空间")
            self.assertEqual(manifest["space_id"], "space")

    def test_source_less_note_keeps_its_claimed_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            claimed = "a" * 64
            self.assertEqual(
                BATCH.source_snapshot(vault, "", claimed),
                ("", claimed, ""),
            )

    def test_managed_state_survives_external_path_or_filename_change(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            managed = vault / "10_来源/原件/文档/2026-08-03_托管原件.docx"
            managed.parent.mkdir(parents=True)
            managed.write_bytes(b"managed original")
            digest = BATCH.sha256_file(managed)
            processed = {
                "files": [
                    {
                        "sha256": digest,
                        "source_name": "工作人员原文件.docx",
                        "stored_original": managed.relative_to(vault).as_posix(),
                    }
                ]
            }
            self.assertEqual(
                BATCH.source_snapshot(
                    vault,
                    "C:/external/location/renamed.docx",
                    digest,
                    "renamed.docx",
                    processed,
                ),
                (
                    managed.relative_to(vault).as_posix(),
                    digest,
                    "工作人员原文件.docx",
                ),
            )

    def test_confirmation_requires_exact_phrase_and_unchanged_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            preview = BATCH.create_preview(vault)
            manifest = vault / preview["_internal"]["manifest_relative"]
            with self.assertRaises(BATCH.BatchError):
                BATCH.confirm_batch(vault, manifest, "确认")

            note = next((vault / "20_知识/企业").glob("*.md"))
            note.write_text(note.read_text(encoding="utf-8") + "\n变化\n", encoding="utf-8")
            with self.assertRaises(BATCH.BatchError):
                BATCH.confirm_batch(vault, manifest, BATCH.EXACT_CONFIRMATION)

    def test_confirmation_is_idempotent_for_same_immutable_batch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            preview = BATCH.create_preview(vault)
            manifest = vault / preview["_internal"]["manifest_relative"]
            first = BATCH.confirm_batch(vault, manifest, BATCH.EXACT_CONFIRMATION)
            second = BATCH.confirm_batch(vault, manifest, BATCH.EXACT_CONFIRMATION)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(second["state"], "confirmed")

    def test_collision_and_remote_edit_decisions_fail_closed(self) -> None:
        item = {
            "title": "同名文档",
            "mapping": {
                "node_token": "mapped-node",
                "last_synced_remote_hash": "old-hash",
            },
        }
        multiple = BATCH.decide_remote_action(
            item,
            [
                {"title": "同名文档", "node_token": "one"},
                {"title": "同名文档", "node_token": "two"},
            ],
        )
        self.assertEqual(multiple["action"], "collision")

        manual_edit = BATCH.decide_remote_action(
            item,
            [
                {
                    "title": "同名文档",
                    "node_token": "mapped-node",
                    "content_hash": "new-hash",
                }
            ],
        )
        self.assertEqual(manual_edit["action"], "conflict")
        self.assertEqual(manual_edit["reason"], "remote_manual_edit")

        missing = BATCH.decide_remote_action(item, [])
        self.assertEqual(missing["action"], "conflict")

    def test_unmapped_exact_title_is_a_collision(self) -> None:
        decision = BATCH.decide_remote_action(
            {"title": "已有标题", "mapping": None},
            [{"title": "已有标题", "node_token": "remote"}],
        )
        self.assertEqual(decision["action"], "collision")
        self.assertEqual(decision["reason"], "unmapped_existing_title")

    def test_create_and_verified_update_are_distinct(self) -> None:
        create = BATCH.decide_remote_action(
            {"title": "新文档", "mapping": None},
            [],
        )
        self.assertEqual(create["action"], "create")

        update = BATCH.decide_remote_action(
            {
                "title": "已映射文档",
                "mapping": {
                    "node_token": "mapped",
                    "last_synced_remote_hash": "same",
                },
            },
            [
                {
                    "title": "已映射文档",
                    "node_token": "mapped",
                    "content_hash": "same",
                }
            ],
        )
        self.assertEqual(update["action"], "update")
        self.assertFalse(update["requires_baseline_check"])

    def test_retry_reuses_partial_remote_artifacts(self) -> None:
        item = {
            "item_id": "item",
            "state": "retryable",
            "source_relative": "10_来源/source.pdf",
            "mapping": None,
            "journal": {
                "node_token": "created-on-first-attempt",
                "source_file_url": "https://example.invalid/file",
            },
        }
        plan = BATCH.retry_plan(item)
        self.assertEqual(plan["action"], "retry_same_item")
        self.assertEqual(plan["document"], "reuse_existing")
        self.assertEqual(plan["source"], "reuse_existing")

    def test_retry_reuses_source_from_existing_mapping(self) -> None:
        item = {
            "item_id": "mapped",
            "state": "retryable",
            "source_relative": "10_来源/source.pdf",
            "mapping": {
                "node_token": "mapped-node",
                "source_file_url": "https://example.invalid/existing",
            },
            "journal": {},
        }
        plan = BATCH.retry_plan(item)
        self.assertEqual(plan["document"], "reuse_existing")
        self.assertEqual(plan["source"], "reuse_existing")

    def test_failure_recovery_keeps_independent_items_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault, count=2)
            preview = BATCH.create_preview(vault)
            manifest_path = vault / preview["_internal"]["manifest_relative"]
            BATCH.confirm_batch(vault, manifest_path, BATCH.EXACT_CONFIRMATION)
            manifest = BATCH.load_json(manifest_path)
            first_id = manifest["items"][0]["item_id"]
            second_id = manifest["items"][1]["item_id"]
            after_failure, _ = BATCH.record_outcome(
                manifest,
                first_id,
                "transient-failure",
                node_token="created-node",
                reason="rate-limit",
            )
            self.assertEqual(after_failure["state"], "executing")
            self.assertEqual(after_failure["items"][0]["state"], "retryable")
            self.assertEqual(after_failure["items"][1]["state"], "pending")

            after_success, _ = BATCH.record_outcome(
                after_failure,
                second_id,
                "success",
                verified=True,
                node_token="second-node",
                source_file_url="https://example.feishu.cn/file/source",
                source_outside_wiki_verified=True,
                final_payload_hash="a" * 64,
            )
            self.assertEqual(after_success["summary"]["success"], 1)
            self.assertEqual(after_success["summary"]["retryable"], 1)
            self.assertEqual(after_success["state"], "executing")

    def test_only_unverified_failed_item_can_reopen_as_retryable(self) -> None:
        manifest = {
            "state": "completed",
            "summary": {"failed": 1},
            "items": [
                {
                    "item_id": "one",
                    "state": "failed",
                    "attempts": 1,
                    "journal": {
                        "verified": False,
                        "node_token": "existing-node",
                        "source_file_url": "https://example.feishu.cn/file/existing",
                    },
                }
            ],
        }
        recovered, idempotent = BATCH.record_outcome(
            manifest,
            "one",
            "transient-failure",
            reason="cli-envelope-drift",
        )
        self.assertFalse(idempotent)
        self.assertEqual(recovered["state"], "executing")
        self.assertEqual(recovered["items"][0]["state"], "retryable")
        self.assertEqual(recovered["items"][0]["attempts"], 2)
        self.assertEqual(
            recovered["items"][0]["journal"]["node_token"],
            "existing-node",
        )
        self.assertEqual(recovered["summary"]["failed"], 0)
        self.assertEqual(recovered["summary"]["retryable"], 1)

        verified_failure = {
            **manifest,
            "items": [
                {
                    **manifest["items"][0],
                    "journal": {"verified": True},
                }
            ],
        }
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(
                verified_failure,
                "one",
                "transient-failure",
            )
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(manifest, "one", "success", verified=True)

    def test_verified_success_is_idempotent_and_cannot_be_replaced(self) -> None:
        manifest = {
            "state": "confirmed",
            "summary": {},
            "items": [
                {
                    "item_id": "one",
                    "state": "pending",
                    "attempts": 0,
                    "journal": {},
                }
            ],
        }
        success, first_idempotent = BATCH.record_outcome(
            manifest,
            "one",
            "success",
            verified=True,
            node_token="node",
        )
        repeated, second_idempotent = BATCH.record_outcome(
            success,
            "one",
            "success",
            verified=True,
            node_token="node",
        )
        self.assertFalse(first_idempotent)
        self.assertTrue(second_idempotent)
        self.assertEqual(repeated["items"][0]["journal"]["node_token"], "node")
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(success, "one", "failed", reason="overwrite")

    def test_unverified_or_node_less_success_is_rejected(self) -> None:
        manifest = {
            "state": "confirmed",
            "summary": {},
            "items": [
                {
                    "item_id": "one",
                    "state": "pending",
                    "attempts": 0,
                    "mapping": None,
                    "journal": {},
                }
            ],
        }
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(manifest, "one", "success", node_token="node")
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(manifest, "one", "success", verified=True)

    def test_source_bearing_success_requires_linked_final_payload(self) -> None:
        manifest = {
            "state": "confirmed",
            "summary": {},
            "items": [
                {
                    "item_id": "one",
                    "state": "pending",
                    "attempts": 0,
                    "source_relative": "10_来源/原件/文档/source.docx",
                    "mapping": None,
                    "journal": {},
                }
            ],
        }
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(
                manifest,
                "one",
                "success",
                verified=True,
                node_token="node",
            )
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(
                manifest,
                "one",
                "success",
                verified=True,
                node_token="node",
                source_file_url="https://example.feishu.cn/file/source",
            )
        with self.assertRaises(BATCH.BatchError):
            BATCH.record_outcome(
                manifest,
                "one",
                "success",
                verified=True,
                node_token="node",
                source_file_url="https://example.feishu.cn/file/source",
                final_payload_hash="b" * 64,
            )
        success, _ = BATCH.record_outcome(
            manifest,
            "one",
            "success",
            verified=True,
            node_token="node",
            source_file_url="https://example.feishu.cn/file/source",
            source_outside_wiki_verified=True,
            final_payload_hash="b" * 64,
        )
        self.assertEqual(
            success["items"][0]["journal"]["final_payload_hash"],
            "b" * 64,
        )

    def test_verified_success_finalizes_all_local_state_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            preview = BATCH.create_preview(vault)
            manifest_path = vault / preview["_internal"]["manifest_relative"]
            BATCH.confirm_batch(vault, manifest_path, BATCH.EXACT_CONFIRMATION)
            manifest = BATCH.load_json(manifest_path)
            item = manifest["items"][0]
            updated, _ = BATCH.record_outcome(
                manifest,
                item["item_id"],
                "success",
                verified=True,
                node_token="node-token",
                obj_token="obj-token",
                feishu_url="https://example.feishu.cn/wiki/node-token",
                source_file_url="https://example.feishu.cn/file/source",
                source_outside_wiki_verified=True,
                final_payload_hash="a" * 64,
                last_synced_remote_hash="b" * 64,
            )
            result = BATCH.finalize_local_success(
                vault,
                manifest_path,
                updated,
                item["item_id"],
                synced_at="2026-07-29T18:00:00+08:00",
            )
            self.assertTrue(result["queue_removed"])
            note = (vault / item["note_relative"]).read_text(encoding="utf-8")
            self.assertIn('status: "published"', note)
            self.assertIn('publish_status: "published"', note)
            self.assertIn("publish_to_feishu: true", note)

            queue = BATCH.load_json(vault / ".kb/state/publish_queue.json")
            self.assertEqual(queue["items"], [])
            mappings = BATCH.load_json(
                vault / ".kb/mappings/feishu_documents.json"
            )
            self.assertEqual(len(mappings["documents"]), 1)
            self.assertEqual(
                mappings["documents"][0]["last_synced_remote_hash"],
                "b" * 64,
            )
            log = next((vault / ".kb/logs/收录").glob("*.md")).read_text(
                encoding="utf-8"
            )
            self.assertIn("已发布并完成回读验证", log)
            self.assertIn("`测试空间 / 03｜流程与交付`", log)
            stored = BATCH.load_json(manifest_path)
            self.assertEqual(stored["items"][0]["state"], "success")


if __name__ == "__main__":
    unittest.main()

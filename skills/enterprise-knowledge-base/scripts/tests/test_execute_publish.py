import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "execute_publish.py"
SPEC = importlib.util.spec_from_file_location("execute_publish", SCRIPT)
EXECUTE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(EXECUTE)
BATCH = EXECUTE.batch


class FakeLark:
    def __init__(
        self,
        vault: Path,
        update_result: str = "success",
        mutate_fetch: bool = False,
        source_name: str = "source.txt",
    ) -> None:
        self.vault = vault
        self.update_result = update_result
        self.mutate_fetch = mutate_fetch
        self.source_name = source_name
        self.commands: list[list[str]] = []
        self.remote_markdown = ""

    def __call__(self, arguments: list[str]) -> dict:
        self.commands.append(arguments)
        prefix = arguments[:2]
        if prefix == ["auth", "status"]:
            return {"ok": True, "verified": True}
        if prefix == ["wiki", "+node-list"]:
            return {
                "ok": True,
                "data": {"nodes": [], "has_more": False, "page_token": ""},
            }
        if prefix == ["wiki", "+node-create"]:
            return {
                "ok": True,
                "data": {
                    "node_token": "node-created",
                    "obj_token": "obj-created",
                    "obj_type": "docx",
                },
            }
        if prefix == ["drive", "+upload"]:
            return {
                "ok": True,
                "data": {
                    "url": "https://tenant.feishu.cn/file/file-created",
                    "file_token": "file-created",
                },
            }
        if prefix == ["drive", "+inspect"]:
            return {
                "ok": True,
                "data": {
                    "type": "file",
                    "token": "file-created",
                    "title": self.source_name,
                },
            }
        if prefix == ["wiki", "+node-get"]:
            raise EXECUTE.ExecutionError(
                "document is not in wiki",
                payload={"ok": False, "error": {"code": 131005}},
            )
        if prefix == ["docs", "+update"]:
            content_arg = arguments[arguments.index("--content") + 1]
            self.remote_markdown = (self.vault / content_arg[1:]).read_text(
                encoding="utf-8"
            )
            return {
                "ok": True,
                "data": {
                    "result": self.update_result,
                    "updated_blocks_count": 1,
                    "warnings": [],
                },
            }
        if prefix == ["docs", "+fetch"]:
            return {
                "ok": True,
                "data": {
                    "document": {
                        "content": (
                            self.remote_markdown.replace("正文。", "远端不一致。")
                            if self.mutate_fetch
                            else self.remote_markdown
                        ),
                        "revision_id": 2,
                    }
                },
            }
        raise AssertionError(f"Unexpected fake command: {arguments}")


class ExecutePublishTests(unittest.TestCase):
    def test_parse_json_output_uses_last_embedded_object(self) -> None:
        payload = EXECUTE.parse_json_output(
            'progress\n{"ok": false}\nmore progress\n{"ok": true, "data": {"value": 1}}\n',
            "",
        )
        self.assertEqual(payload, {"ok": True, "data": {"value": 1}})

    def test_auth_status_accepts_verified_without_ok(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout='{"verified": true, "account": "current"}\n',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            EXECUTE.subprocess,
            "run",
            return_value=completed,
        ):
            payload = EXECUTE.lark_runner(Path(folder))(["auth", "status"])
        self.assertTrue(payload["verified"])

    def test_non_auth_command_still_requires_ok(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout='{"verified": true}\n',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            EXECUTE.subprocess,
            "run",
            return_value=completed,
        ):
            runner = EXECUTE.lark_runner(Path(folder))
            with self.assertRaises(EXECUTE.ExecutionError):
                runner(["docs", "+fetch"])

    def make_vault(
        self,
        vault: Path,
        *,
        source_name: str = "source.txt",
        source_display_name: str = "",
        source_bytes: bytes = b"\xe5\x8e\x9f\xe4\xbb\xb6\n",
        preuploaded_url: str = "",
    ) -> None:
        note_root = vault / "20_知识/企业/03_业务流程"
        source_root = vault / "10_来源/原件/文档"
        mapping_root = vault / ".kb/mappings"
        state_root = vault / ".kb/state"
        log_root = vault / ".kb/logs/收录"
        note_root.mkdir(parents=True)
        source_root.mkdir(parents=True)
        mapping_root.mkdir(parents=True)
        state_root.mkdir(parents=True)
        log_root.mkdir(parents=True)
        source = source_root / source_name
        source.write_bytes(source_bytes)
        source_hash = BATCH.sha256_file(source)
        source_display_name = source_display_name or source_name
        note = note_root / "note.md"
        note.write_text(
            f"""---
title: 发布执行测试
type: source-note
status: ready-to-publish
scope: enterprise
sensitivity: internal
review_status: reviewed
publish_status: pending
publish_to_feishu: false
feishu_writer: lark-cli
feishu_space_name: 测试空间
feishu_parent_node_name: 03｜流程与交付
source_file: "{source_display_name}"
source_path: "10_来源/原件/文档/{source_name}"
source_hash: "{source_hash}"
{f'source_feishu_file_url: "{preuploaded_url}"' if preuploaded_url else ''}
---

# 发布执行测试

正文。

## 来源

- 原件：本地保存
""",
            encoding="utf-8",
        )
        (mapping_root / "feishu_nodes.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "space_name": "测试空间",
                    "space_id": "123",
                    "nodes": [
                        {
                            "node_name": "03｜流程与交付",
                            "node_token": "parent",
                            "verified": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (mapping_root / "feishu_documents.json").write_text(
            '{"version": 2, "documents": []}\n',
            encoding="utf-8",
        )
        (state_root / "publish_queue.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "items": [
                        {
                            "local_path": note.relative_to(vault).as_posix(),
                            "sync_status": "pending",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (log_root / "source.md").write_text(
            f"""---
source_id: "sha256:{source_hash}"
---

# 收录日志

- 飞书操作：未执行。
""",
            encoding="utf-8",
        )

    def run_success(
        self,
        update_result: str,
        *,
        mutate_fetch: bool = False,
        source_name: str = "source.txt",
        source_display_name: str = "",
        remote_source_name: str = "",
        source_bytes: bytes = b"\xe5\x8e\x9f\xe4\xbb\xb6\n",
        preuploaded_url: str = "",
    ) -> tuple[dict, FakeLark, Path]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        vault = Path(folder.name)
        self.make_vault(
            vault,
            source_name=source_name,
            source_display_name=source_display_name,
            source_bytes=source_bytes,
            preuploaded_url=preuploaded_url,
        )
        preview = BATCH.create_preview(vault)
        manifest = vault / preview["_internal"]["manifest_relative"]
        BATCH.confirm_batch(vault, manifest, BATCH.EXACT_CONFIRMATION)
        fake = FakeLark(
            vault,
            update_result=update_result,
            mutate_fetch=mutate_fetch,
            source_name=remote_source_name or source_display_name or source_name,
        )
        result = EXECUTE.execute_batch(
            vault,
            manifest,
            runner=fake,
            synced_at="2026-07-29T18:30:00+08:00",
        )
        return result, fake, vault

    def test_executes_one_confirmed_batch_and_finalizes_locally(self) -> None:
        result, fake, vault = self.run_success("success")
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["success"], 1)
        self.assertEqual(result["links"][0]["url"], "https://tenant.feishu.cn/wiki/node-created")
        self.assertEqual(
            sum(command[:2] == ["wiki", "+node-list"] for command in fake.commands),
            1,
        )
        self.assertEqual(
            sum(command[:2] == ["auth", "status"] for command in fake.commands),
            1,
        )
        self.assertEqual(result["performance"]["remote_call_count"], 8)
        self.assertIn("docs +fetch", result["performance"]["remote_calls_by_command"])
        performance_log = vault / result["performance_log"]
        self.assertTrue(performance_log.is_file())
        performance_text = performance_log.read_text(encoding="utf-8")
        self.assertNotIn("node-created", performance_text)
        self.assertNotIn("file-created", performance_text)
        self.assertNotIn("tenant.feishu.cn", performance_text)
        queue = BATCH.load_json(vault / ".kb/state/publish_queue.json")
        self.assertEqual(queue["items"], [])
        note = next((vault / "20_知识/企业").rglob("*.md")).read_text(
            encoding="utf-8"
        )
        self.assertIn('publish_status: "published"', note)

    def test_retry_reuses_matching_remote_without_second_overwrite(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        vault = Path(folder.name)
        self.make_vault(vault)
        preview = BATCH.create_preview(vault)
        manifest_path = vault / preview["_internal"]["manifest_relative"]
        BATCH.confirm_batch(vault, manifest_path, BATCH.EXACT_CONFIRMATION)
        manifest = BATCH.load_json(manifest_path)
        item = manifest["items"][0]
        item["state"] = "retryable"
        item["journal"] = {
            "node_token": "node-created",
            "obj_token": "obj-created",
            "feishu_url": "https://tenant.feishu.cn/wiki/node-created",
            "source_file_url": "https://tenant.feishu.cn/file/file-created",
            "verified": False,
        }
        manifest["state"] = "executing"
        manifest["summary"].update(
            {"success": 0, "failed": 0, "retryable": 1}
        )
        BATCH.atomic_write_json(manifest_path, manifest)
        fake = FakeLark(vault)
        final_payload, _ = EXECUTE.source_payload(
            vault,
            item,
            item["journal"]["source_file_url"],
        )
        fake.remote_markdown = final_payload.read_text(encoding="utf-8")

        result = EXECUTE.execute_batch(
            vault,
            manifest_path,
            runner=fake,
            synced_at="2026-07-29T18:30:00+08:00",
        )

        self.assertTrue(result["ok"])
        self.assertFalse(
            any(command[:2] == ["docs", "+update"] for command in fake.commands)
        )
        self.assertEqual(
            sum(command[:2] == ["docs", "+fetch"] for command in fake.commands),
            1,
        )

    def test_partial_success_is_accepted_only_after_matching_readback(self) -> None:
        result, _, _ = self.run_success("partial_success")
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["success"], 1)

    def test_partial_success_fails_when_readback_differs(self) -> None:
        result, _, _ = self.run_success(
            "partial_success",
            mutate_fetch=True,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["failed"], 1)

    def test_original_file_types_share_drive_and_source_link_contract(self) -> None:
        samples = {
            "source.docx": b"PK\x03\x04docx",
            "source.pdf": b"%PDF-1.7\n",
            "source.png": b"\x89PNG\r\n\x1a\n",
            "source.mp3": b"ID3\x04\x00\x00",
            "source.mp4": b"\x00\x00\x00\x18ftypisom",
        }
        for source_name, source_bytes in samples.items():
            with self.subTest(source_name=source_name):
                result, fake, _ = self.run_success(
                    "success",
                    source_name=source_name,
                    source_bytes=source_bytes,
                )
                self.assertTrue(result["ok"])
                uploads = [
                    command
                    for command in fake.commands
                    if command[:2] == ["drive", "+upload"]
                ]
                self.assertEqual(len(uploads), 1)
                self.assertEqual(
                    uploads[0][uploads[0].index("--name") + 1],
                    source_name,
                )
                self.assertIn(f"下载原文件：{source_name}", fake.remote_markdown)

    def test_preuploaded_minutes_original_is_reused_without_second_upload(self) -> None:
        source_url = "https://tenant.feishu.cn/file/file-created"
        result, fake, _ = self.run_success(
            "success",
            source_name="meeting.mp3",
            source_bytes=b"ID3\x04\x00\x00",
            preuploaded_url=source_url,
        )
        self.assertTrue(result["ok"])
        uploads = [
            command for command in fake.commands if command[:2] == ["drive", "+upload"]
        ]
        self.assertEqual(uploads, [])
        self.assertIn(source_url, fake.remote_markdown)

    def test_managed_filename_may_differ_from_original_display_name(self) -> None:
        source_url = "https://tenant.feishu.cn/file/file-created"
        result, fake, _ = self.run_success(
            "success",
            source_name="2026-07-10_周五工作会议.m4a",
            source_display_name="周五工作会议.m4a",
            remote_source_name="周五工作会议.m4a",
            source_bytes=b"ID3\x04\x00\x00",
            preuploaded_url=source_url,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(any(command[:2] == ["drive", "+upload"] for command in fake.commands))
        self.assertIn("下载原文件：周五工作会议.m4a", fake.remote_markdown)
        self.assertNotIn("下载原文件：2026-07-10_周五工作会议.m4a", fake.remote_markdown)

    def test_legacy_batch_resolves_display_name_from_processed_state(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        vault = Path(folder.name)
        managed_name = "2026-07-10_周五工作会议.m4a"
        original_name = "周五工作会议.m4a"
        source_url = "https://tenant.feishu.cn/file/file-created"
        self.make_vault(
            vault,
            source_name=managed_name,
            source_display_name=original_name,
            source_bytes=b"ID3\x04\x00\x00",
            preuploaded_url=source_url,
        )
        source = vault / "10_来源/原件/文档" / managed_name
        (vault / ".kb/state/processed_files.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "files": [
                        {
                            "sha256": BATCH.sha256_file(source),
                            "source_name": original_name,
                            "stored_original": source.relative_to(vault).as_posix(),
                            "status": "complete",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        preview = BATCH.create_preview(vault)
        manifest_path = vault / preview["_internal"]["manifest_relative"]
        BATCH.confirm_batch(vault, manifest_path, BATCH.EXACT_CONFIRMATION)
        manifest = BATCH.load_json(manifest_path)
        manifest["items"][0].pop("source_display_name")
        BATCH.atomic_write_json(manifest_path, manifest)
        fake = FakeLark(vault, source_name=original_name)
        result = EXECUTE.execute_batch(
            vault,
            manifest_path,
            runner=fake,
            synced_at="2026-08-03T18:00:00+08:00",
        )
        self.assertTrue(result["ok"])
        self.assertIn(f"下载原文件：{original_name}", fake.remote_markdown)


if __name__ == "__main__":
    unittest.main()

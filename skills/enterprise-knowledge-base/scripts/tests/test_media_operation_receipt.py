import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT = Path(__file__).resolve().parents[1] / "media_operation_receipt.py"
SPEC = importlib.util.spec_from_file_location("media_operation_receipt", SCRIPT)
RECEIPT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(RECEIPT)


class MediaOperationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / ".kb/temp").mkdir(parents=True)
        self.source = self.root / "meeting.mp3"
        self.source.write_bytes(b"ID3\x04\x00\x00audio")
        self.digest = RECEIPT.sha256_file(self.source)
        self.receipt = self.vault / ".kb/temp" / f"media-operation-{self.digest}.json"
        preflight = self.vault / ".kb/temp/ingest-performance" / f"{self.digest}.json"
        preflight.parent.mkdir(parents=True)
        preflight.write_text(
            json.dumps(
                {
                    "schema": "kb-ingest-performance-session/v1",
                    "source_sha256": self.digest,
                    "started_at": "1970-01-01T00:00:00.000+00:00",
                    "preflight_duration_ms": 100.0,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def start(self) -> dict:
        return RECEIPT.start_receipt(
            argparse.Namespace(
                vault=str(self.vault),
                source_sha256=self.digest,
                source_name=self.source.name,
            )
        )

    def mark(self, stage: str, **overrides) -> dict:
        values = {
            "vault": str(self.vault),
            "receipt": str(self.receipt),
            "stage": stage,
            "source_file_url": "",
            "minute_url": "",
            "stored_original_path": "",
            "extraction_path": "",
            "source_note_path": "",
        }
        values.update(overrides)
        return RECEIPT.mark_stage(argparse.Namespace(**values))

    def complete_stages(self) -> None:
        self.mark("preview_ready")
        self.mark("confirmed")
        self.mark(
            "drive_uploaded",
            source_file_url="https://tenant.feishu.cn/file/file-token",
        )
        self.mark(
            "minute_created",
            minute_url="https://tenant.feishu.cn/minutes/minute-token",
        )
        self.mark("transcript_ready")
        self.mark(
            "local_commit_complete",
            stored_original_path="10_来源/原件/音视频/meeting.mp3",
            extraction_path="10_来源/提取/音视频转录/meeting.md",
            source_note_path="20_知识/企业/meeting.md",
        )

    def make_durable_state(self, minute_url="https://tenant.feishu.cn/minutes/minute-token") -> None:
        stored = self.vault / "10_来源/原件/音视频/meeting.mp3"
        extraction = self.vault / "10_来源/提取/音视频转录/meeting.md"
        source_note = self.vault / "20_知识/企业/meeting.md"
        for path in (stored, extraction, source_note):
            path.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(self.source.read_bytes())
        extraction.write_text("# 转录\n\n- [00:00:00] 测试\n", encoding="utf-8")
        file_url = "https://tenant.feishu.cn/file/file-token"
        source_note.write_text(f"source_feishu_file_url: {file_url}\n", encoding="utf-8")
        state = self.vault / ".kb/state/processed_files.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "sha256": self.digest,
                            "status": "complete",
                            "stored_original": "10_来源/原件/音视频/meeting.mp3",
                            "extraction": "10_来源/提取/音视频转录/meeting.md",
                            "source_note": "20_知识/企业/meeting.md",
                            "source_feishu_file_url": file_url,
                            "extraction_profile": {
                                "provider": "feishu-minutes",
                                "source_sha256": self.digest,
                                "source_file_url": file_url,
                                "minute_url": minute_url,
                                "transcript_sha256": RECEIPT.sha256_file(extraction),
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_complete_sequence_writes_timing_log_then_deletes_receipt(self) -> None:
        clock = [
            {"recorded_at": f"1970-01-01T00:00:0{i}.000+00:00", "epoch_ms": 1000 * i}
            for i in range(1, 7)
        ]
        with mock.patch.object(RECEIPT, "now_record", side_effect=clock):
            self.assertTrue(self.start()["created"])
            self.complete_stages()
        self.make_durable_state()
        result = RECEIPT.finalize_receipt(
            argparse.Namespace(vault=str(self.vault), receipt=str(self.receipt))
        )
        self.assertFalse(self.receipt.exists())
        self.assertEqual(result["durations_ms"]["preflight_ms"], 100)
        self.assertEqual(result["durations_ms"]["preview_generation_ms"], 900)
        self.assertEqual(result["durations_ms"]["end_to_end_ms"], 6000)
        performance = self.vault / result["performance_log"]
        self.assertTrue(performance.is_file())
        self.assertNotIn(str(self.source), performance.read_text(encoding="utf-8"))

    def test_stage_order_is_fail_closed_and_receipt_is_preserved(self) -> None:
        self.start()
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "before preview_ready"):
            self.mark("confirmed")
        self.assertTrue(self.receipt.exists())

    def test_finalize_preserves_incomplete_timing(self) -> None:
        self.start()
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "collected-and-verified"):
            RECEIPT.finalize_receipt(
                argparse.Namespace(vault=str(self.vault), receipt=str(self.receipt))
            )
        self.assertTrue(self.receipt.exists())

    def test_finalize_preserves_mismatched_durable_state(self) -> None:
        self.start()
        self.complete_stages()
        self.make_durable_state("https://tenant.feishu.cn/minutes/other-token")
        with self.assertRaisesRegex(RECEIPT.ReceiptError, "does not match"):
            RECEIPT.finalize_receipt(
                argparse.Namespace(vault=str(self.vault), receipt=str(self.receipt))
            )
        self.assertTrue(self.receipt.exists())

    def test_stage_marking_is_idempotent(self) -> None:
        self.start()
        first = self.mark("preview_ready")
        second = self.mark("preview_ready")
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])


if __name__ == "__main__":
    unittest.main()

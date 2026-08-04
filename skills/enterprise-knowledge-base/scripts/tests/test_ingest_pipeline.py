from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "ingest_pipeline.py"
SPEC = importlib.util.spec_from_file_location("ingest_pipeline", SCRIPT)
PIPELINE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PIPELINE)


class IngestPipelineTest(unittest.TestCase):
    def test_structure_fields_parse_capture_mode(self) -> None:
        fields = PIPELINE.structure_fields_from_frontmatter(
            {
                "capture_mode": "standalone-collection",
                "native_unit_type": "transcript",
                "native_unit_count": "425",
                "structure_index": "10_来源/提取/文档索引/索引.md",
                "extraction_policy": "complete",
                "knowledge_policy": "selective",
            }
        )
        self.assertEqual(fields["capture_mode"], "standalone-collection")
        self.assertEqual(fields["native_unit_count"], 425)
        self.assertEqual(
            fields["structure_index"], "10_来源/提取/文档索引/索引.md"
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        for relative in (
            ".kb/state",
            ".kb/temp",
            ".kb/mappings",
            "10_来源/原件/文档",
            "10_来源/提取/文档正文",
            "20_知识/个人",
            "20_知识/企业/03_业务流程",
            "20_知识/原子/方法",
            "30_导航/主题/企业主题",
            ".kb/logs/收录",
        ):
            (self.vault / relative).mkdir(parents=True, exist_ok=True)
        (self.vault / ".kb/state/processed_files.json").write_text(
            '{"version":1,"files":[]}\n', encoding="utf-8"
        )
        (self.vault / ".kb/state/publish_queue.json").write_text(
            '{"version":1,"items":[]}\n', encoding="utf-8"
        )
        (self.vault / ".kb/mappings/feishu_nodes.json").write_text(
            json.dumps(
                {
                    "space_name": "测试知识空间",
                    "space_id": "test-space-id",
                    "nodes": [
                        {
                            "node_name": "03｜流程与交付",
                            "node_token": "test-parent-token",
                            "verified": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.source = self.root / "sample.txt"
        self.source.write_text("# 样本\n\n这是一份企业流程模板。\n", encoding="utf-8")
        (self.vault / "30_导航/主题/企业主题/AI.md").write_text(
            "---\ntitle: AI\n---\n\n# AI\n\n## 核心资料\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_manifest(self) -> Path:
        digest = PIPELINE.sha256_file(self.source)
        source_note = f"""---
title: 样本
status: ready-to-publish
source_file: sample.txt
source_hash: "{digest}"
scope: enterprise
review_status: reviewed
sensitivity: internal
publish_status: pending
publish_to_feishu: false
feishu_writer: lark-cli
feishu_space_name: 测试知识空间
feishu_parent_node_name: "03｜流程与交付"
---

# 样本
"""
        manifest = {
            "schema_version": 1,
            "route": "enterprise",
            "source_note": "20_知识/企业/03_业务流程/样本.md",
            "source": {
                "path": str(self.source),
                "sha256": digest,
                "stored_original": "10_来源/原件/文档/2026-07-28_sample.txt",
                "extraction": "10_来源/提取/文档正文/2026-07-28_sample.md",
                "copy_source_to_extraction": True,
            },
            "metadata": {"processed_at": "2026-07-28"},
            "outputs": [
                {
                    "path": "20_知识/企业/03_业务流程/样本.md",
                    "mode": "create",
                    "content": source_note,
                },
                {
                    "path": "20_知识/原子/方法/样本方法.md",
                    "mode": "create",
                    "content": "---\ntitle: 样本方法\n---\n\n# 样本方法\n",
                },
                {
                    "path": "30_导航/主题/企业主题/AI.md",
                    "mode": "append_once",
                    "heading": "## 核心资料",
                    "content": "- [[20_知识/企业/03_业务流程/样本]]",
                },
                {
                    "path": ".kb/logs/收录/2026-07-28_样本.md",
                    "mode": "create",
                    "content": "# 样本处理记录\n",
                },
            ],
        }
        path = self.vault / ".kb/temp/manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def make_complex_manifest(
        self,
        suffix: str,
        source_bytes: bytes,
        profile: dict,
        extraction: str = "# 完整提取\n\n[page 1] 正文\n",
    ) -> Path:
        self.source = self.root / f"sample{suffix}"
        self.source.write_bytes(source_bytes)
        manifest_path = self.make_manifest()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        extraction_source = self.vault / ".kb/temp/complex-extraction.md"
        extraction_source.write_text(extraction, encoding="utf-8")
        manifest["source"].update(
            {
                "stored_original": f"10_来源/原件/文档/2026-07-28_sample{suffix}",
                "copy_source_to_extraction": False,
                "extraction_source_path": str(extraction_source),
                "extraction_sha256": PIPELINE.sha256_file(extraction_source),
                "extraction_profile": profile,
            }
        )
        if suffix.lower() in {".mp3", ".mp4"}:
            locator = extraction.split("[", 1)[1].split("]", 1)[0]
            manifest["classification_review"] = {
                "phase": "post-extraction-semantic-pass",
                "route": manifest["route"],
                "filename_used": False,
                "additional_confirmation": False,
                "evidence": [
                    {"locator": locator, "reason": "Reusable process content"}
                ],
            }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return manifest_path

    def test_preflight_commit_duplicate_and_repair(self) -> None:
        before = PIPELINE.preflight(self.vault, self.source, include_text=True)
        self.assertTrue(before["ok"])
        self.assertFalse(before["duplicate"])
        self.assertEqual(before["recommended_path"], "fast-text")
        self.assertGreaterEqual(
            before["performance"]["preflight_duration_ms"],
            0,
        )
        self.assertTrue(
            (self.vault / before["performance"]["session_path"]).is_file()
        )

        result = PIPELINE.commit(self.vault, self.make_manifest(), consume=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertGreaterEqual(
            result["performance"]["commit_total_duration_ms"],
            0,
        )
        self.assertIsNotNone(result["performance"]["workflow_elapsed_ms"])
        performance_log = self.vault / result["performance_log"]
        self.assertTrue(performance_log.is_file())
        performance_text = performance_log.read_text(encoding="utf-8")
        self.assertNotIn(str(self.source), performance_text)
        self.assertEqual(
            PIPELINE.sha256_file(self.source),
            PIPELINE.sha256_file(self.vault / "10_来源/原件/文档/2026-07-28_sample.txt"),
        )
        second = PIPELINE.commit(self.vault, self.make_manifest(), consume=False)
        self.assertTrue(second["ok"])
        index_text = (self.vault / "30_导航/主题/企业主题/AI.md").read_text(encoding="utf-8")
        self.assertEqual(index_text.count("[[20_知识/企业/03_业务流程/样本]]"), 1)

        after = PIPELINE.preflight(self.vault, self.source, include_text=False)
        self.assertTrue(after["duplicate"])
        self.assertTrue(after["existing_outputs_present"])

        processed = json.loads(
            (self.vault / ".kb/state/processed_files.json").read_text(encoding="utf-8")
        )
        self.assertEqual(processed["version"], 2)
        self.assertIn(PIPELINE.sha256_file(self.source), processed["by_sha256"])
        self.assertEqual(
            processed["files"][0]["source_path"],
            "10_来源/原件/文档/2026-07-28_sample.txt",
        )

        (self.vault / ".kb/state/processed_files.json").write_text(
            '{"version":1,"files":[]}\n', encoding="utf-8"
        )
        repaired = PIPELINE.repair_state(self.vault, write=True)
        self.assertEqual(repaired["processed_records"], 1)
        self.assertEqual(repaired["pending_records"], 1)
        self.assertTrue(repaired["backups"])
        repaired_state = json.loads(
            (self.vault / ".kb/state/processed_files.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            repaired_state["files"][0]["source_path"],
            "10_来源/原件/文档/2026-07-28_sample.txt",
        )

        (self.vault / "10_来源/提取/文档正文/2026-07-28_sample.md").unlink()
        stale = PIPELINE.preflight(self.vault, self.source, include_text=False)
        self.assertFalse(stale["duplicate"])
        self.assertTrue(stale["stale_state_record"])
        repaired_missing = PIPELINE.repair_state(
            self.vault,
            write=True,
            repair_missing_text_extractions=True,
        )
        self.assertTrue(repaired_missing["repaired_extractions"])
        self.assertTrue(
            (self.vault / "10_来源/提取/文档正文/2026-07-28_sample.md").is_file()
        )

    def test_personal_sources_of_every_kind_require_non_publication_frontmatter(self) -> None:
        for source_kind in ("attachment", "document", "media", "local"):
            with self.subTest(source_kind=source_kind):
                manifest_path = self.make_manifest()
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                personal_path = f"20_知识/个人/{source_kind}.md"
                manifest["route"] = "personal"
                manifest["source_note"] = personal_path
                source_output = next(
                    item for item in manifest["outputs"] if "source_hash:" in item.get("content", "")
                )
                source_output["path"] = personal_path
                source_output["content"] = source_output["content"].replace(
                    "scope: enterprise", "scope: personal"
                ).replace("publish_to_feishu: false", "publish_to_feishu: true")
                manifest["metadata"]["source_kind"] = source_kind
                with self.assertRaises(PIPELINE.PipelineError):
                    PIPELINE.validate_manifest(self.vault, manifest)

    def test_source_change_is_blocked(self) -> None:
        manifest_path = self.make_manifest()
        self.source.write_text("changed", encoding="utf-8")
        with self.assertRaises(PIPELINE.PipelineError):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

    def test_enterprise_space_name_must_match_verified_mapping(self) -> None:
        manifest_path = self.make_manifest()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["outputs"][0]["content"] = manifest["outputs"][0][
            "content"
        ].replace("feishu_space_name: 测试知识空间", "feishu_space_name: 其他空间")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(PIPELINE.PipelineError, "verified space mapping"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

    def test_missing_feishu_category_stays_local(self) -> None:
        manifest_path = self.make_manifest()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_note = manifest["outputs"][0]["content"]
        source_note = source_note.replace(
            'feishu_parent_node_name: "03｜流程与交付"\n', ""
        )
        manifest["outputs"][0]["content"] = source_note
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

        result = PIPELINE.commit(self.vault, manifest_path, consume=False)

        self.assertTrue(result["ok"])
        self.assertFalse(result["queued"])
        queue = json.loads(
            (self.vault / ".kb/state/publish_queue.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(queue["items"], [])

    def test_secret_is_blocked(self) -> None:
        self.source.write_text("api_key = abcdefghijklmnop", encoding="utf-8")
        result = PIPELINE.preflight(self.vault, self.source, include_text=False)
        self.assertTrue(result["blocked"])
        self.assertFalse(result["ok"])

    def test_external_extraction_source_under_temp(self) -> None:
        manifest_path = self.make_manifest()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        extraction_source = self.vault / ".kb/temp/captured-extraction.md"
        extraction_source.write_text("# 完整网页提取\n\n正文\n", encoding="utf-8")
        manifest["source"]["copy_source_to_extraction"] = False
        manifest["source"]["extraction_source_path"] = str(extraction_source)
        manifest["source"]["extraction_sha256"] = PIPELINE.sha256_file(extraction_source)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        result = PIPELINE.commit(self.vault, manifest_path, consume=False)
        self.assertTrue(result["ok"])
        self.assertEqual(
            (self.vault / "10_来源/提取/文档正文/2026-07-28_sample.md").read_text(
                encoding="utf-8"
            ),
            "# 完整网页提取\n\n正文\n",
        )

    def test_preflight_routes_complex_formats_and_blocks_mismatch(self) -> None:
        docx = self.root / "sample.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        samples = {
            docx: ("document", "document-native"),
            self.root / "sample.pdf": ("pdf", "pdf-adaptive"),
            self.root / "sample.png": ("image", "image-vision"),
            self.root / "sample.mp3": ("audio", "transcription-required"),
            self.root / "sample.mp4": ("video", "video-transcription-required"),
        }
        (self.root / "sample.pdf").write_bytes(b"%PDF-1.7\n")
        (self.root / "sample.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "sample.mp3").write_bytes(b"ID3\x04\x00\x00")
        (self.root / "sample.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")
        for path, (kind, recommended) in samples.items():
            with self.subTest(path=path.name):
                result = PIPELINE.preflight(self.vault, path, include_text=False)
                self.assertTrue(result["ok"])
                self.assertEqual(result["source_format"]["kind"], kind)
                self.assertTrue(result["source_format"]["profile_required"])
                self.assertEqual(result["recommended_path"], recommended)

        mismatch = self.root / "mismatch.pdf"
        mismatch.write_bytes(b"not-a-pdf")
        blocked = PIPELINE.preflight(self.vault, mismatch, include_text=False)
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["block_reason"], "extension-container-mismatch")

    def test_complex_profile_commits_and_is_persisted(self) -> None:
        profile = {
            "source_kind": "pdf",
            "method": "native-text",
            "provider": "bundled-runtime",
            "locator_type": "page",
            "units_total": 1,
            "units_processed": 1,
            "complete": True,
            "review_required": False,
            "warnings": [],
        }
        result = PIPELINE.commit(
            self.vault,
            self.make_complex_manifest(".pdf", b"%PDF-1.7\n", profile),
            consume=False,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        processed = json.loads(
            (self.vault / ".kb/state/processed_files.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            processed["files"][0]["extraction_profile"]["source_kind"],
            "pdf",
        )

    def test_complex_profile_and_complete_coverage_are_required(self) -> None:
        profile = {
            "source_kind": "image",
            "method": "vision",
            "provider": "host-vision",
            "locator_type": "region",
            "units_total": 1,
            "units_processed": 1,
            "complete": True,
            "warnings": [],
        }
        manifest_path = self.make_complex_manifest(
            ".png", b"\x89PNG\r\n\x1a\n", profile
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"].pop("extraction_profile")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "extraction_profile"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

        manifest["source"]["extraction_profile"] = {
            **profile,
            "units_total": 2,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "coverage"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

    def test_secret_found_only_in_binary_extraction_is_blocked(self) -> None:
        profile = {
            "source_kind": "pdf",
            "method": "native-text",
            "provider": "bundled-runtime",
            "locator_type": "page",
            "units_total": 1,
            "units_processed": 1,
            "complete": True,
            "warnings": [],
        }
        manifest_path = self.make_complex_manifest(
            ".pdf",
            b"%PDF-1.7\n",
            profile,
            extraction="# 提取\n\n[page 1] api_key = abcdefghijklmnop\n",
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "in extraction"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

    def test_media_requires_available_timestamped_provider(self) -> None:
        profile = {
            "source_kind": "audio",
            "method": "transcription",
            "provider": "unavailable",
            "locator_type": "timestamp",
            "units_total": 1,
            "units_processed": 1,
            "complete": True,
            "timestamped": True,
            "warnings": [],
        }
        manifest_path = self.make_complex_manifest(
            ".mp3", b"ID3\x04\x00\x00", profile, "[00:00-00:05] 测试\n"
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "available provider"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["extraction_profile"].update(
            {
                "provider": "feishu-minutes",
                "provider_reference": "minute-token",
                "provider_engine": "feishu-minutes",
                "audio_duration_ms": 5000,
                "source_sha256": PIPELINE.sha256_file(self.source),
                "transcript_sha256": manifest["source"]["extraction_sha256"],
                "normalized_audio": False,
                "remote_original_reusable": True,
                "source_file_url": "https://tenant.feishu.cn/file/file-token",
                "minute_url": "https://tenant.feishu.cn/minutes/minute-token",
            }
        )
        manifest["outputs"][0]["content"] = manifest["outputs"][0][
            "content"
        ].replace(
            "---\n\n# 样本",
            "source_feishu_file_url: https://tenant.feishu.cn/file/file-token\n---\n\n# 样本",
        )
        manifest["source"]["extraction_profile"]["timestamped"] = False
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "timestamps"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

        manifest["source"]["extraction_profile"]["timestamped"] = True
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        result = PIPELINE.commit(self.vault, manifest_path, consume=False)
        self.assertTrue(result["ok"])
        processed = json.loads(
            (self.vault / ".kb/state/processed_files.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            processed["files"][0]["extraction_profile"]["provider"],
            "feishu-minutes",
        )
        self.assertEqual(
            processed["files"][0]["source_feishu_file_url"],
            "https://tenant.feishu.cn/file/file-token",
        )

    def test_media_accepts_explicit_local_whisper_profile(self) -> None:
        extraction = "# 转录\n\n[00:00:00.000–00:00:05.000] 本地测试\n"
        profile = {
            "source_kind": "audio",
            "method": "transcription",
            "provider": "local-whisper-cpp",
            "locator_type": "timestamp",
            "units_total": 1,
            "units_processed": 1,
            "complete": True,
            "review_required": False,
            "timestamped": True,
            "warnings": [],
            "provider_reference": "model-sha256:" + "a" * 64,
            "provider_engine": "ggml-small-q5.bin",
            "audio_duration_ms": 5000,
            "source_sha256": "",
            "transcript_sha256": "",
            "normalized_audio": True,
            "remote_original_reusable": False,
            "model_sha256": "a" * 64,
            "runtime_sha256": "b" * 64,
        }
        manifest_path = self.make_complex_manifest(
            ".mp3", b"ID3\x04\x00\x00", profile, extraction
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["extraction_profile"]["source_sha256"] = (
            PIPELINE.sha256_file(self.source)
        )
        manifest["source"]["extraction_profile"]["transcript_sha256"] = manifest[
            "source"
        ]["extraction_sha256"]
        valid_review = manifest.pop("classification_review")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "classification_review"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

        manifest["classification_review"] = {
            **valid_review,
            "filename_used": True,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(PIPELINE.PipelineError, "filename evidence"):
            PIPELINE.commit(self.vault, manifest_path, consume=False)

        manifest["classification_review"] = valid_review
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        result = PIPELINE.commit(self.vault, manifest_path, consume=False)
        self.assertTrue(result["ok"])
        processed = json.loads(
            (self.vault / ".kb/state/processed_files.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            processed["files"][0]["extraction_profile"]["provider"],
            "local-whisper-cpp",
        )
        self.assertEqual(
            processed["files"][0]["classification_review"]["phase"],
            "post-extraction-semantic-pass",
        )
        self.assertFalse(
            processed["files"][0]["classification_review"]["additional_confirmation"]
        )

    def test_review_required_complex_extraction_stays_out_of_queue(self) -> None:
        profile = {
            "source_kind": "pdf",
            "method": "hybrid",
            "provider": "host-vision",
            "locator_type": "page",
            "units_total": 1,
            "units_processed": 1,
            "complete": True,
            "review_required": True,
            "warnings": ["一处手写文字无法确认"],
        }
        manifest_path = self.make_complex_manifest(
            ".pdf", b"%PDF-1.7\n", profile
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["outputs"][0]["content"] = manifest["outputs"][0][
            "content"
        ].replace("review_status: reviewed", "review_status: needs-verification")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        result = PIPELINE.commit(self.vault, manifest_path, consume=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["queued"])

    def test_consume_path_is_validated_before_writes(self) -> None:
        safe_manifest = self.make_manifest()
        outside_manifest = self.root / "outside-manifest.json"
        outside_manifest.write_bytes(safe_manifest.read_bytes())
        with self.assertRaises(PIPELINE.PipelineError):
            PIPELINE.commit(self.vault, outside_manifest, consume=True)
        self.assertFalse(
            (self.vault / "10_来源/原件/文档/2026-07-28_sample.txt").exists()
        )

    def test_transaction_rolls_back_after_mid_commit_failure(self) -> None:
        first = self.vault / ".kb/logs/收录/existing.md"
        second = self.vault / ".kb/logs/收录/new.md"
        first.write_text("old", encoding="utf-8")
        real_replace = PIPELINE.os.replace
        calls = {"count": 0}

        def flaky_replace(source, target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("injected failure")
            return real_replace(source, target)

        with mock.patch.object(PIPELINE.os, "replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                PIPELINE.apply_transaction(
                    self.vault,
                    [
                        (first, b"new-value", "replace-state"),
                        (second, b"new-file", "create"),
                    ],
                )
        self.assertEqual(first.read_text(encoding="utf-8"), "old")
        self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()

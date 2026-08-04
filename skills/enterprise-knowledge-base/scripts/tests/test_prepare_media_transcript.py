import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_media_transcript.py"
SPEC = importlib.util.spec_from_file_location("prepare_media_transcript", SCRIPT)
MEDIA = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MEDIA)


class PrepareMediaTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / ".kb/temp").mkdir(parents=True)
        self.source = self.root / "meeting.mp3"
        self.source.write_bytes(b"ID3\x04\x00\x00audio")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def feishu_args(self, **overrides):
        transcript = self.vault / ".kb/temp/raw.txt"
        transcript.write_text(
            "张三 00:00:00.100\n开始讨论。\n李四 00:00:03.500\n同意。\n",
            encoding="utf-8",
        )
        values = {
            "vault": str(self.vault),
            "source": str(self.source),
            "transcript": str(transcript),
            "output": ".kb/temp/meeting.transcript.md",
            "profile_output": ".kb/temp/meeting.profile.json",
            "source_file_url": "https://tenant.feishu.cn/file/file-token",
            "minute_url": "https://tenant.feishu.cn/minutes/minute-token",
            "minute_token": "minute-token",
            "duration_ms": 5000,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_feishu_profile_is_timestamped_and_reusable(self) -> None:
        result = MEDIA.prepare_feishu(self.feishu_args())
        self.assertTrue(result["ok"])
        profile = json.loads(
            (self.vault / ".kb/temp/meeting.profile.json").read_text(
                encoding="utf-8"
            )
        )
        transcript = (self.vault / ".kb/temp/meeting.transcript.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(profile["provider"], "feishu-minutes")
        self.assertTrue(profile["remote_original_reusable"])
        self.assertEqual(profile["provider_reference"], "minute-token")
        self.assertEqual(profile["transcript_sha256"], MEDIA.sha256_text(transcript))
        self.assertIn("00:00:00.100", transcript)

    def test_feishu_profile_rejects_mismatched_minute_token(self) -> None:
        with self.assertRaisesRegex(MEDIA.MediaTranscriptError, "does not match"):
            MEDIA.prepare_feishu(self.feishu_args(minute_token="other"))

    def test_outputs_must_stay_under_temp(self) -> None:
        with self.assertRaisesRegex(MEDIA.MediaTranscriptError, "under Vault"):
            MEDIA.prepare_feishu(self.feishu_args(output="outside.md"))

    def local_args(self, **overrides):
        whisper = self.root / "whisper-cli.exe"
        ffmpeg = self.root / "ffmpeg.exe"
        ffprobe = self.root / "ffprobe.exe"
        model = self.root / "ggml-small-q5.bin"
        for path, payload in (
            (whisper, b"runtime"),
            (ffmpeg, b"ffmpeg"),
            (ffprobe, b"ffprobe"),
            (model, b"model"),
        ):
            path.write_bytes(payload)
        values = {
            "vault": str(self.vault),
            "source": str(self.source),
            "output": ".kb/temp/local.transcript.md",
            "profile_output": ".kb/temp/local.profile.json",
            "whisper_bin": str(whisper),
            "model": str(model),
            "ffmpeg_bin": str(ffmpeg),
            "ffprobe_bin": str(ffprobe),
            "language": "auto",
            "timeout_seconds": 60,
            "allow_local_execution": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_local_route_requires_explicit_execution_gate(self) -> None:
        with self.assertRaisesRegex(MEDIA.MediaTranscriptError, "authorization"):
            MEDIA.transcribe_local(
                self.local_args(allow_local_execution=False)
            )

    def test_local_route_records_runtime_and_model_hashes(self) -> None:
        def fake_run(arguments, timeout):
            if "-osrt" in arguments:
                prefix = Path(arguments[arguments.index("-of") + 1])
                prefix.with_suffix(".srt").write_text(
                    "1\n00:00:00,000 --> 00:00:02,000\n本地测试。\n\n",
                    encoding="utf-8",
                )
            return mock.Mock(returncode=0, stdout="5.0\n", stderr="")

        with mock.patch.object(MEDIA, "run_checked", side_effect=fake_run), mock.patch.object(
            MEDIA, "probe_duration_ms", return_value=5000
        ):
            result = MEDIA.transcribe_local(self.local_args())
        self.assertTrue(result["ok"])
        profile = json.loads(
            (self.vault / ".kb/temp/local.profile.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(profile["provider"], "local-whisper-cpp")
        self.assertRegex(profile["model_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(profile["runtime_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(profile["remote_original_reusable"])
        self.assertNotIn("source_file_url", profile)

    def test_video_profile_waits_for_scene_coverage(self) -> None:
        video = self.root / "meeting.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypisom")
        result = MEDIA.prepare_feishu(self.feishu_args(source=str(video)))
        self.assertFalse(result["complete"])
        self.assertIn("video-scene-coverage-required", result["warnings"])

if __name__ == "__main__":
    unittest.main()

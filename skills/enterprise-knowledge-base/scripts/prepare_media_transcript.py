#!/usr/bin/env python3
"""Prepare Feishu Minutes or optional local whisper.cpp transcripts for ingestion."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kb_core import sha256_bytes, sha256_file


AUDIO_EXTENSIONS = {".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".wav", ".wma"}
VIDEO_EXTENSIONS = {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".webm", ".wmv"}
TIME_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,3})?\b")
SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*\d+\s*\n(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3}).*?\n(?P<text>.*?)(?=\n\s*\n|\Z)"
)
FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
class MediaTranscriptError(RuntimeError):
    pass


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def media_kind(source: Path) -> str:
    suffix = source.suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    raise MediaTranscriptError(f"Unsupported audio or video extension: {suffix}")


def resolve_source(value: str) -> Path:
    source = Path(value).expanduser().resolve()
    if not source.is_file():
        raise MediaTranscriptError(f"Source file not found: {source}")
    media_kind(source)
    return source


def resolve_existing(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise MediaTranscriptError(f"{label} not found: {path}")
    return path


def resolve_temp_output(vault: Path, value: str) -> Path:
    path = Path(value)
    resolved = (vault / path).resolve() if not path.is_absolute() else path.resolve()
    temp_root = (vault / ".kb" / "temp").resolve()
    try:
        resolved.relative_to(temp_root)
    except ValueError as exc:
        raise MediaTranscriptError("Outputs must stay under Vault .kb/temp") from exc
    if resolved.exists():
        raise MediaTranscriptError(f"Refusing to overwrite existing output: {resolved}")
    return resolved


def validate_feishu_url(value: str, path_prefix: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not any(host.endswith(suffix) for suffix in FEISHU_HOST_SUFFIXES)
        or path_prefix not in parsed.path
    ):
        raise MediaTranscriptError(f"Invalid Feishu URL for {path_prefix}: {value}")
    return value


def atomic_write_outputs(output: Path, text: str, profile_output: Path, profile: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    profile_output.parent.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for target, payload in (
            (output, text.encode("utf-8")),
            (
                profile_output,
                (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            ),
        ):
            handle, temp_name = tempfile.mkstemp(
                prefix=target.name + ".", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            staged.append((Path(temp_name), target))
        for temp_path, target in staged:
            os.replace(temp_path, target)
    except Exception:
        for temp_path, target in staged:
            temp_path.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        raise


def markdown_transcript(source: Path, provider: str, body: str) -> tuple[str, int]:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    timestamps = TIME_RE.findall(normalized)
    if not normalized or not timestamps:
        raise MediaTranscriptError("Transcript must contain timestamped content")
    header = [
        "# 音视频转录",
        "",
        f"- 原文件：`{source.name}`",
        f"- 转录提供者：`{provider}`",
        "",
        "## 转录正文",
        "",
    ]
    return "\n".join(header) + normalized + "\n", len(timestamps)


def base_profile(
    source: Path,
    provider: str,
    engine: str,
    reference: str,
    duration_ms: int,
    transcript: str,
    units: int,
    *,
    normalized_audio: bool,
) -> dict[str, Any]:
    kind = media_kind(source)
    if duration_ms <= 0:
        raise MediaTranscriptError("Media duration must be positive")
    warnings: list[str] = []
    complete = True
    review_required = False
    if kind == "video":
        warnings.append("video-scene-coverage-required")
        complete = False
        review_required = True
    return {
        "source_kind": kind,
        "method": "transcription",
        "provider": provider,
        "locator_type": "timestamp",
        "units_total": units,
        "units_processed": units,
        "complete": complete,
        "review_required": review_required,
        "warnings": warnings,
        "timestamped": True,
        "provider_engine": engine,
        "provider_reference": reference,
        "audio_duration_ms": duration_ms,
        "source_sha256": sha256_file(source),
        "transcript_sha256": sha256_text(transcript),
        "normalized_audio": normalized_audio,
    }


def prepare_feishu(args: argparse.Namespace) -> dict[str, Any]:
    vault = Path(args.vault).resolve()
    source = resolve_source(args.source)
    transcript_input = resolve_existing(args.transcript, "Feishu transcript")
    output = resolve_temp_output(vault, args.output)
    profile_output = resolve_temp_output(vault, args.profile_output)
    file_url = validate_feishu_url(args.source_file_url, "/file/")
    minute_url = validate_feishu_url(args.minute_url, "/minutes/")
    minute_token = urlparse(minute_url).path.rstrip("/").split("/")[-1]
    if not minute_token or (args.minute_token and args.minute_token != minute_token):
        raise MediaTranscriptError("Minute token does not match the Minute URL")
    if args.duration_ms > 6 * 60 * 60 * 1000:
        raise MediaTranscriptError("Feishu Minutes media exceeds six hours")
    raw = transcript_input.read_text(encoding="utf-8-sig")
    transcript, units = markdown_transcript(source, "feishu-minutes", raw)
    profile = base_profile(
        source,
        "feishu-minutes",
        "feishu-minutes",
        minute_token,
        args.duration_ms,
        transcript,
        units,
        normalized_audio=False,
    )
    profile.update({
        "source_file_url": file_url,
        "minute_url": minute_url,
        "remote_original_reusable": True,
    })
    atomic_write_outputs(output, transcript, profile_output, profile)
    return {
        "ok": True,
        "provider": "feishu-minutes",
        "output": output.relative_to(vault).as_posix(),
        "profile_output": profile_output.relative_to(vault).as_posix(),
        "source_file_url": file_url,
        "minute_url": minute_url,
        "complete": profile["complete"],
        "warnings": profile["warnings"],
    }


def resolve_program(value: str, env_name: str, fallback: str) -> Path:
    candidate = value or os.environ.get(env_name, "") or shutil.which(fallback) or ""
    if not candidate:
        raise MediaTranscriptError(
            f"Missing {fallback}; set {env_name} or pass its explicit path"
        )
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise MediaTranscriptError(f"Executable not found: {path}")
    return path


def run_checked(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            arguments,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaTranscriptError(f"Local transcription command failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise MediaTranscriptError(
            f"Local transcription command exited {completed.returncode}: {detail}"
        )
    return completed


def probe_duration_ms(ffprobe: Path, source: Path) -> int:
    completed = run_checked(
        [
            str(ffprobe), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ],
        60,
    )
    try:
        duration = int(round(float(completed.stdout.strip()) * 1000))
    except ValueError as exc:
        raise MediaTranscriptError("Unable to parse media duration") from exc
    if duration <= 0:
        raise MediaTranscriptError("Media duration must be positive")
    return duration


def parse_srt(path: Path) -> tuple[str, int]:
    text = path.read_text(encoding="utf-8-sig")
    segments: list[str] = []
    for match in SRT_BLOCK_RE.finditer(text):
        content = " ".join(line.strip() for line in match.group("text").splitlines() if line.strip())
        if not content:
            continue
        start = match.group("start").replace(",", ".")
        end = match.group("end").replace(",", ".")
        segments.append(f"- [{start}–{end}] {content}")
    if not segments:
        raise MediaTranscriptError("whisper.cpp produced no timestamped SRT segments")
    return "\n".join(segments), len(segments)


def transcribe_local(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_local_execution:
        raise MediaTranscriptError("Local model execution requires explicit authorization")
    vault = Path(args.vault).resolve()
    source = resolve_source(args.source)
    output = resolve_temp_output(vault, args.output)
    profile_output = resolve_temp_output(vault, args.profile_output)
    whisper = resolve_program(args.whisper_bin, "WHISPER_CPP_BIN", "whisper-cli")
    ffmpeg = resolve_program(args.ffmpeg_bin, "FFMPEG_BIN", "ffmpeg")
    ffprobe = resolve_program(args.ffprobe_bin, "FFPROBE_BIN", "ffprobe")
    model_value = args.model or os.environ.get("WHISPER_CPP_MODEL", "")
    if not model_value:
        raise MediaTranscriptError("Missing whisper.cpp model; set WHISPER_CPP_MODEL")
    model = resolve_existing(model_value, "whisper.cpp model")
    duration_ms = probe_duration_ms(ffprobe, source)

    with tempfile.TemporaryDirectory(prefix="kb-local-whisper-") as temp_name:
        temp = Path(temp_name)
        normalized = temp / "normalized.wav"
        run_checked(
            [
                str(ffmpeg), "-v", "error", "-y", "-i", str(source),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(normalized),
            ],
            min(args.timeout_seconds, 3600),
        )
        prefix = temp / "transcript"
        run_checked(
            [
                str(whisper), "-m", str(model), "-f", str(normalized),
                "-l", args.language, "-osrt", "-of", str(prefix),
            ],
            args.timeout_seconds,
        )
        srt = prefix.with_suffix(".srt")
        if not srt.is_file():
            raise MediaTranscriptError("whisper.cpp did not create the expected SRT file")
        body, units = parse_srt(srt)

    transcript, _ = markdown_transcript(source, "local-whisper-cpp", body)
    model_hash = sha256_file(model)
    runtime_hash = sha256_file(whisper)
    profile = base_profile(
        source,
        "local-whisper-cpp",
        model.name,
        f"model-sha256:{model_hash}",
        duration_ms,
        transcript,
        units,
        normalized_audio=True,
    )
    profile.update(
        {
            "model_sha256": model_hash,
            "runtime_sha256": runtime_hash,
            "remote_original_reusable": False,
        }
    )
    atomic_write_outputs(output, transcript, profile_output, profile)
    return {
        "ok": True,
        "provider": "local-whisper-cpp",
        "output": output.relative_to(vault).as_posix(),
        "profile_output": profile_output.relative_to(vault).as_posix(),
        "complete": profile["complete"],
        "warnings": profile["warnings"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    feishu = subparsers.add_parser("feishu-profile")
    feishu.add_argument("--vault", default=".")
    feishu.add_argument("--source", required=True)
    feishu.add_argument("--transcript", required=True)
    feishu.add_argument("--output", required=True)
    feishu.add_argument("--profile-output", required=True)
    feishu.add_argument("--source-file-url", required=True)
    feishu.add_argument("--minute-url", required=True)
    feishu.add_argument("--minute-token", default="")
    feishu.add_argument("--duration-ms", required=True, type=int)

    local = subparsers.add_parser("local-transcribe")
    local.add_argument("--vault", default=".")
    local.add_argument("--source", required=True)
    local.add_argument("--output", required=True)
    local.add_argument("--profile-output", required=True)
    local.add_argument("--whisper-bin", default="")
    local.add_argument("--model", default="")
    local.add_argument("--ffmpeg-bin", default="")
    local.add_argument("--ffprobe-bin", default="")
    local.add_argument("--language", default="auto")
    local.add_argument("--timeout-seconds", type=int, default=21600)
    local.add_argument("--allow-local-execution", action="store_true")

    receipt_start = subparsers.add_parser("receipt-start")
    receipt_start.add_argument("--vault", default=".")
    receipt_start.add_argument("--source-sha256", required=True)
    receipt_start.add_argument("--source-name", required=True)

    receipt_mark = subparsers.add_parser("receipt-mark")
    receipt_mark.add_argument("--vault", default=".")
    receipt_mark.add_argument("--receipt", required=True)
    receipt_mark.add_argument("--stage", required=True)
    receipt_mark.add_argument("--source-file-url", default="")
    receipt_mark.add_argument("--minute-url", default="")
    receipt_mark.add_argument("--stored-original-path", default="")
    receipt_mark.add_argument("--extraction-path", default="")
    receipt_mark.add_argument("--source-note-path", default="")

    receipt_finalize = subparsers.add_parser("receipt-finalize")
    receipt_finalize.add_argument("--vault", default=".")
    receipt_finalize.add_argument("--receipt", required=True)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        from runtime_access import authorize
        from vault_context import discover_vault

        args.vault = str(discover_vault(args.vault))
        authorize(Path(args.vault), "media")
        if args.command.startswith("receipt-"):
            import media_operation_receipt as receipt

            result = {
                "receipt-start": receipt.start_receipt,
                "receipt-mark": receipt.mark_stage,
                "receipt-finalize": receipt.finalize_receipt,
            }[args.command](args)
        elif args.command == "feishu-profile":
            result = prepare_feishu(args)
        else:
            result = transcribe_local(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

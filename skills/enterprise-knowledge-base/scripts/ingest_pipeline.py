#!/usr/bin/env python3
"""Deterministic local-ingestion helpers for the knowledge Vault."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kb_core import (
    CoreError,
    atomic_write_json as write_json_atomic,
    load_json as core_load_json,
    sha256_bytes,
    sha256_file,
)


TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
COMPLEX_SOURCE_FORMATS = {
    ".docx": ("document", "document-native", "document-reading"),
    ".doc": ("document", "document-converter-required", "legacy-word-converter"),
    ".odt": ("document", "document-native", "document-reading"),
    ".rtf": ("document", "document-native", "document-reading"),
    ".pdf": ("pdf", "pdf-adaptive", "pdf-reading"),
    ".png": ("image", "image-vision", "vision"),
    ".jpg": ("image", "image-vision", "vision"),
    ".jpeg": ("image", "image-vision", "vision"),
    ".gif": ("image", "image-vision", "vision"),
    ".webp": ("image", "image-vision", "vision"),
    ".bmp": ("image", "image-vision", "vision"),
    ".tif": ("image", "image-vision", "vision"),
    ".tiff": ("image", "image-vision", "vision"),
    ".svg": ("image", "image-vision", "vision"),
    ".mp3": ("audio", "transcription-required", "approved-transcription"),
    ".wav": ("audio", "transcription-required", "approved-transcription"),
    ".m4a": ("audio", "transcription-required", "approved-transcription"),
    ".flac": ("audio", "transcription-required", "approved-transcription"),
    ".ogg": ("audio", "transcription-required", "approved-transcription"),
    ".aac": ("audio", "transcription-required", "approved-transcription"),
    ".mp4": ("video", "video-transcription-required", "approved-transcription"),
    ".mov": ("video", "video-transcription-required", "approved-transcription"),
    ".mkv": ("video", "video-transcription-required", "approved-transcription"),
    ".webm": ("video", "video-transcription-required", "approved-transcription"),
    ".avi": ("video", "video-transcription-required", "approved-transcription"),
    ".m4v": ("video", "video-transcription-required", "approved-transcription"),
}
EXTRACTION_METHODS = {
    "document": {"native-text", "converted-text", "vision", "hybrid"},
    "pdf": {"native-text", "ocr", "vision", "hybrid"},
    "image": {"ocr", "vision", "hybrid"},
    "audio": {"transcription", "hybrid"},
    "video": {"transcription", "hybrid"},
}
EXTRACTION_LOCATORS = {
    "document": {"structure", "page", "page-or-structure"},
    "pdf": {"page"},
    "image": {"region"},
    "audio": {"timestamp"},
    "video": {"timestamp"},
}
APPROVED_MEDIA_PROVIDERS = {"feishu-minutes", "local-whisper-cpp"}
FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
FRONTMATTER_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$")
CAPTURE_MODES = {
    "standalone",
    "standalone-collection",
    "corpus",
    "dataset",
    "media",
    "fragment",
}
ALLOWED_OUTPUT_PREFIXES = {
    "20_知识/个人",
    "20_知识/企业",
    "20_知识/原子",
    "30_导航/主题",
    ".kb/logs/收录",
}
SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:app_?secret|client_?secret|api_?key|access_?token|"
            r"refresh_?token|cookie)\b\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{8,})"
        ),
    ),
)


class PipelineError(RuntimeError):
    pass


def preflight_performance_path(vault: Path, digest: str) -> Path:
    return vault / ".kb" / "temp" / "ingest-performance" / f"{digest}.json"


def record_preflight_performance(
    vault: Path,
    digest: str,
    started_at: datetime,
    duration_ms: float,
) -> str:
    relative = (
        Path(".kb")
        / "temp"
        / "ingest-performance"
        / f"{digest}.json"
    )
    write_json_atomic(
        vault / relative,
        {
            "schema": "kb-ingest-performance-session/v1",
            "source_sha256": digest,
            "started_at": started_at.isoformat(timespec="milliseconds"),
            "preflight_duration_ms": round(duration_ms, 1),
        },
    )
    return relative.as_posix()


def source_format(path: Path) -> dict[str, Any] | None:
    configured = COMPLEX_SOURCE_FORMATS.get(path.suffix.lower())
    if not configured:
        return None
    kind, recommended_path, capability = configured
    signature_valid, detected_container = verify_source_signature(path)
    return {
        "kind": kind,
        "recommended_path": recommended_path,
        "required_capability": capability,
        "profile_required": True,
        "signature_valid": signature_valid,
        "detected_container": detected_container,
        "locator_types": sorted(EXTRACTION_LOCATORS[kind]),
    }


def verify_source_signature(path: Path) -> tuple[bool | None, str]:
    suffix = path.suffix.lower()
    with path.open("rb") as stream:
        head = stream.read(1024)
    if suffix == ".pdf":
        return head.startswith(b"%PDF-"), "pdf" if head.startswith(b"%PDF-") else "unknown"
    if suffix == ".docx":
        if not zipfile.is_zipfile(path):
            return False, "unknown"
        try:
            with zipfile.ZipFile(path) as archive:
                valid = "word/document.xml" in archive.namelist()
        except (OSError, zipfile.BadZipFile):
            valid = False
        return valid, "docx" if valid else "zip"
    if suffix == ".odt":
        if not zipfile.is_zipfile(path):
            return False, "unknown"
        try:
            with zipfile.ZipFile(path) as archive:
                valid = archive.read("mimetype") == b"application/vnd.oasis.opendocument.text"
        except (KeyError, OSError, zipfile.BadZipFile):
            valid = False
        return valid, "odt" if valid else "zip"
    checks = {
        ".doc": (head.startswith(bytes.fromhex("D0CF11E0A1B11AE1")), "ole"),
        ".rtf": (head.lstrip().startswith(b"{\\rtf"), "rtf"),
        ".png": (head.startswith(b"\x89PNG\r\n\x1a\n"), "png"),
        ".jpg": (head.startswith(b"\xff\xd8\xff"), "jpeg"),
        ".jpeg": (head.startswith(b"\xff\xd8\xff"), "jpeg"),
        ".gif": (head.startswith((b"GIF87a", b"GIF89a")), "gif"),
        ".webp": (head.startswith(b"RIFF") and head[8:12] == b"WEBP", "webp"),
        ".bmp": (head.startswith(b"BM"), "bmp"),
        ".tif": (head.startswith((b"II*\x00", b"MM\x00*")), "tiff"),
        ".tiff": (head.startswith((b"II*\x00", b"MM\x00*")), "tiff"),
        ".svg": (b"<svg" in head.lower(), "svg"),
        ".wav": (head.startswith(b"RIFF") and head[8:12] == b"WAVE", "wav"),
        ".mp3": (head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0), "mp3"),
        ".flac": (head.startswith(b"fLaC"), "flac"),
        ".ogg": (head.startswith(b"OggS"), "ogg"),
        ".aac": (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0, "aac"),
        ".avi": (head.startswith(b"RIFF") and head[8:12] == b"AVI ", "avi"),
        ".mkv": (head.startswith(b"\x1aE\xdf\xa3"), "matroska"),
        ".webm": (head.startswith(b"\x1aE\xdf\xa3"), "webm"),
    }
    if suffix in checks:
        valid, container = checks[suffix]
        return valid, container if valid else "unknown"
    if suffix in {".mp4", ".mov", ".m4a", ".m4v"}:
        valid = len(head) >= 12 and head[4:8] == b"ftyp"
        return valid, "iso-base-media" if valid else "unknown"
    return None, "unverified"


def validate_extraction_profile(
    source_path: Path,
    source: dict[str, Any],
    extraction_text: str,
) -> dict[str, Any] | None:
    format_info = source_format(source_path)
    profile = source.get("extraction_profile")
    if not format_info:
        return profile if isinstance(profile, dict) else None
    if format_info["signature_valid"] is False:
        raise PipelineError(
            f"Source extension does not match its container: {source_path.suffix.lower()}"
        )
    if not isinstance(profile, dict):
        raise PipelineError("Complex source requires source.extraction_profile")
    kind = str(profile.get("source_kind", "")).strip()
    if kind != format_info["kind"]:
        raise PipelineError(
            f"extraction_profile source_kind must be {format_info['kind']}"
        )
    method = str(profile.get("method", "")).strip()
    if method not in EXTRACTION_METHODS[kind]:
        raise PipelineError(f"Unsupported extraction method for {kind}: {method}")
    provider = str(profile.get("provider", "")).strip()
    if not provider or provider.casefold() in {"none", "unavailable", "unknown"}:
        raise PipelineError("Complex extraction requires an available provider")
    locator = str(profile.get("locator_type", "")).strip()
    if locator not in EXTRACTION_LOCATORS[kind]:
        raise PipelineError(f"Unsupported locator_type for {kind}: {locator}")
    total = profile.get("units_total")
    processed = profile.get("units_processed")
    if (
        isinstance(total, bool)
        or isinstance(processed, bool)
        or not isinstance(total, int)
        or not isinstance(processed, int)
        or total < 1
        or processed != total
        or profile.get("complete") is not True
    ):
        raise PipelineError("Complex extraction coverage is incomplete")
    warnings = profile.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise PipelineError("extraction_profile warnings must be a list of strings")
    if not isinstance(profile.get("review_required", False), bool):
        raise PipelineError("extraction_profile review_required must be boolean")
    if kind in {"audio", "video"}:
        if provider not in APPROVED_MEDIA_PROVIDERS:
            raise PipelineError("Media extraction requires an approved provider")
        if profile.get("timestamped") is not True:
            raise PipelineError("Audio and video transcriptions require timestamps")
        reference = str(profile.get("provider_reference", "")).strip()
        engine = str(profile.get("provider_engine", "")).strip()
        duration = profile.get("audio_duration_ms")
        source_digest = str(profile.get("source_sha256", "")).strip().lower()
        transcript_digest = str(profile.get("transcript_sha256", "")).strip().lower()
        normalized_audio = profile.get("normalized_audio")
        if not reference or not engine:
            raise PipelineError("Media extraction requires provider reference evidence")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration <= 0
        ):
            raise PipelineError("Media extraction has an invalid provider duration")
        if provider == "feishu-minutes" and duration > 6 * 60 * 60 * 1000:
            raise PipelineError("Feishu Minutes media exceeds six hours")
        if not HASH_RE.fullmatch(source_digest) or source_digest != sha256_file(source_path):
            raise PipelineError("Media extraction source hash does not match")
        expected_extraction_digest = normalize_hash(
            str(source.get("extraction_sha256", ""))
        )
        if (
            not HASH_RE.fullmatch(transcript_digest)
            or transcript_digest != expected_extraction_digest
        ):
            raise PipelineError("Media extraction transcript hash does not match")
        if not isinstance(normalized_audio, bool):
            raise PipelineError("Media extraction normalized_audio must be boolean")
        remote_reusable = profile.get("remote_original_reusable")
        if not isinstance(remote_reusable, bool):
            raise PipelineError("Media extraction remote reuse flag must be boolean")
        if provider == "feishu-minutes":
            source_file_url = validate_feishu_url(
                str(profile.get("source_file_url", "")).strip(), "/file/"
            )
            minute_url = validate_feishu_url(
                str(profile.get("minute_url", "")).strip(), "/minutes/"
            )
            minute_token = urlparse(minute_url).path.rstrip("/").split("/")[-1]
            if reference != minute_token:
                raise PipelineError("Minute reference does not match Minute URL")
            if remote_reusable is not True:
                raise PipelineError("Feishu original must be marked reusable")
        else:
            model_digest = str(profile.get("model_sha256", "")).strip().lower()
            runtime_digest = str(profile.get("runtime_sha256", "")).strip().lower()
            if not HASH_RE.fullmatch(model_digest) or not HASH_RE.fullmatch(runtime_digest):
                raise PipelineError("Local Whisper requires model and runtime hashes")
            if remote_reusable is not False:
                raise PipelineError("Local Whisper must not claim a remote original")
            if profile.get("source_file_url") or profile.get("minute_url"):
                raise PipelineError("Local Whisper profile must not contain Feishu URLs")
    if not extraction_text.strip():
        raise PipelineError("Complex extraction must not be empty")
    normalized = {
        "source_kind": kind,
        "method": method,
        "provider": provider,
        "locator_type": locator,
        "units_total": total,
        "units_processed": processed,
        "complete": True,
        "review_required": bool(profile.get("review_required", False)),
        "warnings": warnings,
    }
    if kind in {"audio", "video"}:
        normalized.update(
            {
                "timestamped": True,
                "provider_reference": reference,
                "provider_engine": engine,
                "audio_duration_ms": duration,
                "source_sha256": source_digest,
                "transcript_sha256": transcript_digest,
                "normalized_audio": normalized_audio,
                "remote_original_reusable": remote_reusable,
            }
        )
        if provider == "feishu-minutes":
            normalized.update(
                {
                    "source_file_url": source_file_url,
                    "minute_url": minute_url,
                }
            )
        else:
            normalized.update(
                {
                    "model_sha256": model_digest,
                    "runtime_sha256": runtime_digest,
                }
            )
    return normalized


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = core_load_json(path, default)
    except CoreError as exc:
        raise PipelineError(str(exc)) from exc
    if not isinstance(value, dict):
        raise PipelineError(f"JSON state must be an object: {path}")
    return value


def load_verified_space_mapping(vault: Path) -> tuple[str, str]:
    mapping = load_json(vault / ".kb/mappings/feishu_nodes.json", {})
    space_name = str(mapping.get("space_name", "")).strip()
    space_id = str(mapping.get("space_id", "")).strip()
    nodes = mapping.get("nodes")
    has_verified_node = isinstance(nodes, list) and any(
        isinstance(node, dict) and node.get("verified") is True for node in nodes
    )
    if not space_name or not space_id or not has_verified_node:
        raise PipelineError("Current Vault has no complete verified space mapping")
    return space_name, space_id


def decode_text(path: Path) -> tuple[str | None, dict[str, Any]]:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, {
            "supported": False,
            "encoding": None,
            "bom_detected": bom,
            "error": f"UTF-8 decode failed at byte {exc.start}",
        }
    return text, {
        "supported": True,
        "encoding": "utf-8",
        "bom_detected": bom,
        "line_count": len(text.splitlines()),
        "character_count": len(text),
    }


def find_secrets(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append({"kind": kind, "line": line_number})
    return findings


def normalize_hash(value: str) -> str:
    candidate = value.lower().removeprefix("sha256:")
    if not HASH_RE.fullmatch(candidate):
        raise PipelineError(f"Invalid SHA-256: {value}")
    return candidate


def validate_feishu_url(value: str, path_prefix: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not any(host.endswith(suffix) for suffix in FEISHU_HOST_SUFFIXES)
        or path_prefix not in parsed.path
    ):
        raise PipelineError(f"Invalid Feishu media URL for {path_prefix}")
    return value


def under_prefix(relative: str, prefix: str) -> bool:
    return relative == prefix or relative.startswith(prefix.rstrip("/") + "/")


def normalize_rel_path(value: str, allowed_prefixes: set[str] | None = None) -> str:
    rel = value.replace("\\", "/").strip("/")
    path = Path(rel)
    if not rel or path.is_absolute() or ".." in path.parts:
        raise PipelineError(f"Unsafe relative path: {value}")
    if allowed_prefixes is not None and not any(under_prefix(rel, prefix) for prefix in allowed_prefixes):
        raise PipelineError(f"Path outside allowed roots: {value}")
    return rel


def parse_frontmatter_text(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = FRONTMATTER_RE.match(line)
        if not match:
            continue
        key, raw_value = match.groups()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace("\\\\", "\\")
        if value.lower() == "true":
            result[key] = True
        elif value.lower() == "false":
            result[key] = False
        else:
            result[key] = value
    return result


def structure_fields_from_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    capture_mode = str(frontmatter.get("capture_mode", "")).strip()
    if capture_mode:
        if capture_mode not in CAPTURE_MODES:
            raise PipelineError(f"Unsupported capture_mode: {capture_mode}")
        fields["capture_mode"] = capture_mode
    for key in ("native_unit_type", "extraction_policy", "knowledge_policy"):
        value = str(frontmatter.get(key, "")).strip()
        if value:
            fields[key] = value
    raw_count = str(frontmatter.get("native_unit_count", "")).strip()
    if raw_count:
        if not raw_count.isdigit() or int(raw_count) < 1:
            raise PipelineError(f"Invalid native_unit_count: {raw_count}")
        fields["native_unit_count"] = int(raw_count)
    structure_index = str(frontmatter.get("structure_index", "")).strip()
    if structure_index:
        fields["structure_index"] = normalize_rel_path(
            structure_index, {"10_来源/提取"}
        )
    evidence_bundle = str(frontmatter.get("evidence_bundle", "")).strip()
    if evidence_bundle:
        fields["evidence_bundle"] = normalize_rel_path(
            evidence_bundle, {".kb/evidence"}
        )
    chunk_root = str(frontmatter.get("chunk_root", "")).strip()
    if chunk_root:
        fields["chunk_root"] = normalize_rel_path(
            chunk_root, {"10_来源/提取"}
        )
    raw_chunk_count = str(frontmatter.get("chunk_count", "")).strip()
    if raw_chunk_count:
        if not raw_chunk_count.isdigit() or int(raw_chunk_count) < 1:
            raise PipelineError(f"Invalid chunk_count: {raw_chunk_count}")
        fields["chunk_count"] = int(raw_chunk_count)
    return fields


def relative_to_vault(path: Path, vault: Path) -> str | None:
    try:
        return path.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        return None


def lookup_hash(state: dict[str, Any], digest: str) -> dict[str, Any] | None:
    files = state.get("files", [])
    if not isinstance(files, list):
        return None
    by_hash = state.get("by_sha256", {})
    if isinstance(by_hash, dict) and digest in by_hash:
        index = by_hash[digest]
        if isinstance(index, int) and 0 <= index < len(files):
            item = files[index]
            return item if isinstance(item, dict) else None
    for item in files:
        if isinstance(item, dict) and str(item.get("sha256", "")).lower() == digest:
            return item
    return None


def preflight(vault: Path, source: Path, include_text: bool) -> dict[str, Any]:
    phase_started = time.perf_counter()
    started_at = datetime.now().astimezone()
    source = source.resolve()
    vault = vault.resolve()
    if not source.is_file():
        raise PipelineError(f"Source file not found: {source}")

    digest = sha256_file(source)
    state_path = vault / ".kb" / "state" / "processed_files.json"
    state = load_json(state_path, {"version": 2, "files": [], "by_sha256": {}})
    existing = lookup_hash(state, digest)
    existing_outputs_present = False
    if existing:
        required_paths = [
            existing.get("stored_original"),
            existing.get("extraction"),
            existing.get("source_note"),
        ]
        existing_outputs_present = all(
            isinstance(value, str) and value and (vault / Path(value)).is_file()
            for value in required_paths
        )

    result: dict[str, Any] = {
        "ok": True,
        "mode": "preflight",
        "source": {
            "path": str(source),
            "name": source.name,
            "extension": source.suffix.lower(),
            "size_bytes": source.stat().st_size,
            "modified_at": datetime.fromtimestamp(source.stat().st_mtime).astimezone().isoformat(),
            "sha256": digest,
        },
        "duplicate": existing is not None and existing_outputs_present,
        "stale_state_record": existing is not None and not existing_outputs_present,
        "existing_outputs_present": existing_outputs_present,
        "existing_record": existing,
        "state_version": state.get("version", 1),
        "state_has_hash_index": isinstance(state.get("by_sha256"), dict),
        "text": {"supported": False},
        "sensitive_findings": [],
        "blocked": False,
        "recommended_path": "adaptive",
    }

    format_info = source_format(source)
    if format_info:
        result["source_format"] = format_info
        result["recommended_path"] = format_info["recommended_path"]
        if format_info["signature_valid"] is False:
            result["blocked"] = True
            result["ok"] = False
            result["block_reason"] = "extension-container-mismatch"

    if source.suffix.lower() in TEXT_EXTENSIONS:
        text, metadata = decode_text(source)
        result["text"] = metadata
        if text is None:
            result["blocked"] = True
            result["ok"] = False
        else:
            findings = find_secrets(text)
            result["sensitive_findings"] = findings
            result["blocked"] = bool(findings)
            result["ok"] = not result["blocked"]
            if len(text) <= 100_000:
                result["recommended_path"] = "fast-text"
                if include_text:
                    result["text"]["content"] = text
            else:
                result["recommended_path"] = "large-text"
    preflight_duration_ms = (time.perf_counter() - phase_started) * 1000
    result["performance"] = {
        "preflight_duration_ms": round(preflight_duration_ms, 1),
        "session_path": record_preflight_performance(
            vault,
            digest,
            started_at,
            preflight_duration_ms,
        ),
    }
    return result


def insert_after_heading(existing: str, heading: str, snippet: str) -> str:
    marker = snippet.strip()
    if marker and marker in existing:
        return existing
    lines = existing.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == heading.strip():
            newline = "\n" if "\r\n" not in existing else "\r\n"
            addition = newline + marker + newline
            lines.insert(index + 1, addition)
            return "".join(lines)
    raise PipelineError(f"Index heading not found: {heading}")


def prepare_output(vault: Path, output: dict[str, Any]) -> tuple[Path, bytes, str]:
    rel = normalize_rel_path(str(output.get("path", "")), ALLOWED_OUTPUT_PREFIXES)
    target = vault / Path(rel)
    mode = str(output.get("mode", "create"))
    content = output.get("content")
    if not isinstance(content, str):
        raise PipelineError(f"Output content must be text: {rel}")

    if mode == "create":
        data = content.encode("utf-8")
    elif mode == "append_once":
        if not target.is_file():
            raise PipelineError(f"append_once target does not exist: {rel}")
        current = target.read_text(encoding="utf-8-sig")
        heading = str(output.get("heading", ""))
        if not heading:
            raise PipelineError(f"append_once requires heading: {rel}")
        data = insert_after_heading(current, heading, content).encode("utf-8")
    elif mode == "replace":
        if not target.is_file():
            raise PipelineError(f"replace target does not exist: {rel}")
        expected = normalize_hash(str(output.get("expected_sha256", "")))
        actual = sha256_file(target)
        if actual != expected:
            raise PipelineError(f"Concurrent modification detected: {rel}")
        data = content.encode("utf-8")
    else:
        raise PipelineError(f"Unsupported output mode '{mode}': {rel}")
    return target, data, mode


def build_state_documents(
    vault: Path,
    manifest: dict[str, Any],
    source_note_text: str,
) -> tuple[bytes, bytes, dict[str, Any]]:
    state_dir = vault / ".kb" / "state"
    processed_path = state_dir / "processed_files.json"
    queue_path = state_dir / "publish_queue.json"
    processed = load_json(processed_path, {"version": 2, "files": [], "by_sha256": {}})
    queue = load_json(queue_path, {"version": 2, "items": []})

    source = manifest["source"]
    digest = normalize_hash(source["sha256"])
    metadata = manifest.get("metadata", {})
    frontmatter = parse_frontmatter_text(source_note_text)
    record = {
        "sha256": digest,
        "source_name": Path(source["path"]).name,
        "source_path": normalize_rel_path(source["stored_original"]),
        "stored_original": normalize_rel_path(source["stored_original"]),
        "extraction": normalize_rel_path(source["extraction"]),
        "source_note": normalize_rel_path(manifest["source_note"]),
        "scope": manifest["route"],
        "processed_at": metadata.get("processed_at", datetime.now().astimezone().date().isoformat()),
        "status": "complete",
    }
    if isinstance(source.get("extraction_profile"), dict):
        record["extraction_profile"] = source["extraction_profile"]
    if isinstance(manifest.get("classification_review"), dict):
        record["classification_review"] = manifest["classification_review"]
    if str(frontmatter.get("source_feishu_file_url", "")).strip():
        record["source_feishu_file_url"] = str(
            frontmatter["source_feishu_file_url"]
        ).strip()
    record.update(structure_fields_from_frontmatter(frontmatter))

    files = [item for item in processed.get("files", []) if isinstance(item, dict)]
    files = [item for item in files if str(item.get("sha256", "")).lower() != digest]
    files.append(record)
    files.sort(key=lambda item: (str(item.get("processed_at", "")), str(item.get("source_note", ""))))
    processed_new = {
        "version": 2,
        "files": files,
        "by_sha256": {
            str(item["sha256"]).lower(): index
            for index, item in enumerate(files)
            if HASH_RE.fullmatch(str(item.get("sha256", "")).lower())
        },
    }

    items = [item for item in queue.get("items", []) if isinstance(item, dict)]
    local_path = record["source_note"]
    items = [item for item in items if item.get("local_path") != local_path]
    eligible = (
        manifest["route"] == "enterprise"
        and frontmatter.get("status") == "ready-to-publish"
        and frontmatter.get("publish_status") == "pending"
        and frontmatter.get("review_status") == "reviewed"
        and frontmatter.get("sensitivity") not in {"restricted", "personal-sensitive"}
        and bool(str(frontmatter.get("feishu_parent_node_name", "")).strip())
    )
    if eligible:
        items.append(
            {
                "local_path": local_path,
                "local_source_hash": digest,
                "writer": frontmatter.get("feishu_writer", "lark-cli"),
                "space_name": frontmatter["feishu_space_name"],
                "parent_node_name": frontmatter["feishu_parent_node_name"],
                "sync_status": "pending",
            }
        )
    items.sort(key=lambda item: str(item.get("local_path", "")))
    queue_new = {"version": 2, "items": items}

    return (
        (json.dumps(processed_new, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        (json.dumps(queue_new, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        {"processed_record": record, "queued": eligible},
    )


def validate_media_classification_review(
    manifest: dict[str, Any], route: str, extraction_text: str
) -> dict[str, Any]:
    review = manifest.get("classification_review")
    if not isinstance(review, dict):
        raise PipelineError("Media manifest requires classification_review")
    if review.get("phase") != "post-extraction-semantic-pass":
        raise PipelineError("Media classification must occur after extraction")
    if review.get("route") != route:
        raise PipelineError("Media classification route does not match manifest route")
    if review.get("filename_used") is not False:
        raise PipelineError("Media classification must not use filename evidence")
    if review.get("additional_confirmation") is not False:
        raise PipelineError("Media classification must not add another confirmation")
    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise PipelineError("Media classification requires semantic evidence")
    normalized: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise PipelineError("Media classification evidence must be structured")
        locator = str(item.get("locator", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not locator or locator not in extraction_text:
            raise PipelineError("Media classification locator is absent from extraction")
        if not reason:
            raise PipelineError("Media classification evidence requires a reason")
        normalized.append({"locator": locator, "reason": reason})
    return {
        "phase": "post-extraction-semantic-pass",
        "route": route,
        "filename_used": False,
        "additional_confirmation": False,
        "evidence": normalized,
    }


def validate_manifest(vault: Path, manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if manifest.get("schema_version") != 1:
        raise PipelineError("Manifest schema_version must be 1")
    route = manifest.get("route")
    if route not in {"personal", "enterprise"}:
        raise PipelineError("File route must be personal or enterprise")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise PipelineError("Manifest source is required")
    source_path = Path(str(source.get("path", ""))).resolve()
    if not source_path.is_file():
        raise PipelineError(f"Source file not found: {source_path}")
    digest = normalize_hash(str(source.get("sha256", "")))
    if sha256_file(source_path) != digest:
        raise PipelineError("Source changed after preflight")
    text, text_metadata = decode_text(source_path) if source_path.suffix.lower() in TEXT_EXTENSIONS else (None, {})
    if text_metadata.get("supported") and text is not None:
        findings = find_secrets(text)
        if findings:
            raise PipelineError(f"Sensitive credential patterns found at lines: {[x['line'] for x in findings]}")

    original_rel = normalize_rel_path(str(source.get("stored_original", "")), {"10_来源/原件"})
    extraction_rel = normalize_rel_path(str(source.get("extraction", "")), {"10_来源/提取"})
    source["stored_original"] = original_rel
    source["extraction"] = extraction_rel
    extraction_text: str
    if source.get("copy_source_to_extraction") is True:
        if source_path.suffix.lower() not in TEXT_EXTENSIONS or text is None:
            raise PipelineError("copy_source_to_extraction is only valid for readable text/Markdown")
        extraction_text = text
    else:
        extraction_content = source.get("extraction_content")
        extraction_source_value = source.get("extraction_source_path")
        if isinstance(extraction_content, str):
            extraction_text = extraction_content
            extraction_data = extraction_content.encode("utf-8")
        else:
            if not isinstance(extraction_source_value, str) or not extraction_source_value:
                raise PipelineError(
                    "Non-copy extraction requires extraction_content or extraction_source_path"
                )
            extraction_source = Path(extraction_source_value).resolve()
            try:
                extraction_source.relative_to((vault / ".kb" / "temp").resolve())
            except ValueError as exc:
                raise PipelineError(
                    "extraction_source_path must be under the Vault .kb/temp directory"
                ) from exc
            if not extraction_source.is_file():
                raise PipelineError(f"Extraction source not found: {extraction_source}")
            extraction_data = extraction_source.read_bytes()
            try:
                extraction_text = extraction_data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise PipelineError("Extraction source must be valid UTF-8") from exc
            if "\ufffd" in extraction_text:
                raise PipelineError("Extraction source contains Unicode replacement characters")
            source["extraction_source_path"] = str(extraction_source)

        expected_extraction_hash = source.get("extraction_sha256")
        format_info = source_format(source_path)
        if format_info and not expected_extraction_hash:
            raise PipelineError("Complex extraction requires extraction_sha256")
        if expected_extraction_hash:
            expected = normalize_hash(str(expected_extraction_hash))
            if sha256_bytes(extraction_data) != expected:
                raise PipelineError("Extraction source changed after capture")

    extraction_findings = find_secrets(extraction_text)
    if extraction_findings:
        raise PipelineError(
            "Sensitive credential patterns found in extraction at lines: "
            f"{[item['line'] for item in extraction_findings]}"
        )
    extraction_profile = validate_extraction_profile(
        source_path,
        source,
        extraction_text,
    )
    if extraction_profile:
        source["extraction_profile"] = extraction_profile
        if extraction_profile.get("source_kind") in {"audio", "video"}:
            manifest["classification_review"] = validate_media_classification_review(
                manifest, route, extraction_text
            )

    source_note_rel = normalize_rel_path(str(manifest.get("source_note", "")), ALLOWED_OUTPUT_PREFIXES)
    processed_state = load_json(
        vault / ".kb" / "state" / "processed_files.json",
        {"version": 2, "files": [], "by_sha256": {}},
    )
    existing_record = lookup_hash(processed_state, digest)
    if existing_record and existing_record.get("source_note") != source_note_rel:
        raise PipelineError(
            "Source hash is already registered to a different source note: "
            f"{existing_record.get('source_note')}"
        )
    required_root = "20_知识/个人" if route == "personal" else "20_知识/企业"
    if not under_prefix(source_note_rel, required_root):
        raise PipelineError(f"{route} source note must be under {required_root}")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise PipelineError("Manifest outputs must be a non-empty list")
    source_note_text = None
    has_index = False
    has_log = False
    for output in outputs:
        if not isinstance(output, dict):
            raise PipelineError("Each output must be an object")
        rel = normalize_rel_path(str(output.get("path", "")), ALLOWED_OUTPUT_PREFIXES)
        has_index = has_index or under_prefix(rel, "30_导航/主题")
        has_log = has_log or under_prefix(rel, ".kb/logs/收录")
        if rel == source_note_rel:
            source_note_text = output.get("content")
    if not isinstance(source_note_text, str):
        raise PipelineError("source_note must match one text output")
    if not has_index or not has_log:
        raise PipelineError("Manifest must update an index and create a processing log")

    fm = parse_frontmatter_text(source_note_text)
    structure_fields_from_frontmatter(fm)
    if fm.get("source_hash", "").lower() != digest:
        raise PipelineError("source_note source_hash does not match source")
    if fm.get("scope") != route:
        raise PipelineError("source_note scope does not match route")
    if route == "personal":
        if fm.get("publish_to_feishu") is not False:
            raise PipelineError("Personal source_note requires publish_to_feishu: false")
        if fm.get("publish_status") in {"pending", "published"}:
            raise PipelineError("Personal source_note cannot enter publication state")
    if isinstance(source.get("extraction_profile"), dict):
        media_provider = source["extraction_profile"].get("provider")
        note_source_url = str(fm.get("source_feishu_file_url", "")).strip()
        if media_provider == "feishu-minutes":
            expected_source_url = source["extraction_profile"]["source_file_url"]
            if note_source_url != expected_source_url:
                raise PipelineError(
                    "Feishu media source_note requires the verified source_feishu_file_url"
                )
        elif media_provider == "local-whisper-cpp" and note_source_url:
            raise PipelineError(
                "Offline local media source_note must not contain a Feishu source URL"
            )
    if route == "enterprise":
        verified_space_name, _ = load_verified_space_mapping(vault)
        required = {
            "status": "ready-to-publish",
            "publish_status": "pending",
            "publish_to_feishu": False,
            "feishu_writer": "lark-cli",
        }
        for key, expected in required.items():
            if fm.get(key) != expected:
                raise PipelineError(f"Enterprise source_note requires {key}: {expected}")
        if fm.get("feishu_space_name") != verified_space_name:
            raise PipelineError(
                "Enterprise source_note feishu_space_name must match verified space mapping"
            )
        if (
            isinstance(source.get("extraction_profile"), dict)
            and source["extraction_profile"].get("review_required") is True
            and fm.get("review_status") != "needs-verification"
        ):
            raise PipelineError(
                "Review-required extraction must use review_status: needs-verification"
            )
        forbidden = {"feishu_space_id", "feishu_parent_node_token", "feishu_node_token", "feishu_obj_token"}
        leaked = sorted(forbidden.intersection(fm))
        if leaked:
            raise PipelineError(f"Pending note must not contain Feishu identifiers: {leaked}")
    return source, source_note_text


def apply_transaction(vault: Path, changes: list[tuple[Path, bytes, str]]) -> list[dict[str, Any]]:
    temp_root = vault / ".kb" / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="ingest-stage-", dir=temp_root))
    prepared: list[dict[str, Any]] = []
    committed: list[dict[str, Any]] = []
    try:
        for index, (target, data, mode) in enumerate(changes):
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "create" and target.exists():
                if target.read_bytes() == data:
                    prepared.append({"target": target, "data": data, "mode": "skip-identical"})
                    continue
                raise PipelineError(f"Refusing to overwrite existing file: {target}")
            staged = stage / f"new-{index}"
            staged.write_bytes(data)
            backup = None
            if target.exists():
                backup = stage / f"backup-{index}"
                shutil.copy2(target, backup)
            prepared.append(
                {"target": target, "data": data, "mode": mode, "staged": staged, "backup": backup}
            )

        for item in prepared:
            if item["mode"] == "skip-identical":
                continue
            target = item["target"]
            os.replace(item["staged"], target)
            committed.append(item)

        verification: list[dict[str, Any]] = []
        for item in prepared:
            target = item["target"]
            expected = sha256_bytes(item["data"])
            actual = sha256_file(target)
            if expected != actual:
                raise PipelineError(f"Post-write verification failed: {target}")
            verification.append(
                {
                    "path": relative_to_vault(target, vault) or str(target),
                    "sha256": actual,
                    "status": item["mode"],
                }
            )
        return verification
    except Exception:
        for item in reversed(committed):
            target = item["target"]
            backup = item.get("backup")
            if backup and backup.exists():
                os.replace(backup, target)
            elif target.exists():
                target.unlink()
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def finalize_ingest_performance(
    vault: Path,
    digest: str,
    commit_started: float,
    validation_duration_ms: float,
    preparation_duration_ms: float,
    apply_duration_ms: float,
) -> tuple[dict[str, Any], str]:
    completed_at = datetime.now().astimezone()
    session_path = preflight_performance_path(vault, digest)
    preflight_duration_ms: float | None = None
    workflow_elapsed_ms: float | None = None
    if session_path.is_file():
        session = load_json(session_path, {})
        if (
            session.get("schema") == "kb-ingest-performance-session/v1"
            and session.get("source_sha256") == digest
        ):
            preflight_duration_ms = float(
                session.get("preflight_duration_ms", 0.0)
            )
            try:
                workflow_started = datetime.fromisoformat(
                    str(session["started_at"])
                )
                workflow_elapsed_ms = (
                    completed_at - workflow_started
                ).total_seconds() * 1000
            except (KeyError, TypeError, ValueError):
                workflow_elapsed_ms = None
    performance = {
        "preflight_duration_ms": (
            round(preflight_duration_ms, 1)
            if preflight_duration_ms is not None
            else None
        ),
        "validation_duration_ms": round(validation_duration_ms, 1),
        "preparation_duration_ms": round(preparation_duration_ms, 1),
        "apply_duration_ms": round(apply_duration_ms, 1),
        "commit_total_duration_ms": round(
            (time.perf_counter() - commit_started) * 1000,
            1,
        ),
        "workflow_elapsed_ms": (
            round(workflow_elapsed_ms, 1)
            if workflow_elapsed_ms is not None
            else None
        ),
    }
    log_relative = (
        Path(".kb")
        / "logs"
        / "ingest-performance"
        / completed_at.date().isoformat()
        / (
            completed_at.strftime("%H%M%S%f")
            + "-"
            + digest[:12]
            + ".json"
        )
    )
    performance["logging_status"] = "recorded"
    performance_log = log_relative.as_posix()
    try:
        write_json_atomic(
            vault / log_relative,
            {
                "schema": "kb-ingest-performance/v1",
                "source_sha256": digest,
                "recorded_at": completed_at.isoformat(timespec="milliseconds"),
                "status": "completed",
                "performance": performance,
            },
        )
    except OSError as exc:
        performance["logging_status"] = "failed"
        performance["logging_error_type"] = type(exc).__name__
        performance_log = ""
    session_path.unlink(missing_ok=True)
    return performance, performance_log


def commit(vault: Path, manifest_path: Path, consume: bool) -> dict[str, Any]:
    commit_started = time.perf_counter()
    vault = vault.resolve()
    if consume:
        try:
            manifest_path.resolve().relative_to((vault / ".kb" / "temp").resolve())
        except ValueError as exc:
            raise PipelineError("--consume requires the manifest to be under .kb/temp") from exc
    validation_started = time.perf_counter()
    manifest = load_json(manifest_path, {})
    source, source_note_text = validate_manifest(vault, manifest)
    source_path = Path(source["path"]).resolve()
    validation_duration_ms = (time.perf_counter() - validation_started) * 1000

    preparation_started = time.perf_counter()
    changes: list[tuple[Path, bytes, str]] = []
    original_target = vault / Path(source["stored_original"])
    changes.append((original_target, source_path.read_bytes(), "create"))

    extraction_target = vault / Path(source["extraction"])
    if source.get("copy_source_to_extraction") is True:
        if source_path.suffix.lower() not in TEXT_EXTENSIONS:
            raise PipelineError("copy_source_to_extraction is only valid for text/Markdown")
        extraction_data = source_path.read_bytes()
    else:
        extraction_content = source.get("extraction_content")
        if isinstance(extraction_content, str):
            extraction_data = extraction_content.encode("utf-8")
        else:
            extraction_source = Path(str(source["extraction_source_path"])).resolve()
            extraction_data = extraction_source.read_bytes()
    changes.append((extraction_target, extraction_data, "create"))

    for output in manifest["outputs"]:
        changes.append(prepare_output(vault, output))

    processed_data, queue_data, state_result = build_state_documents(vault, manifest, source_note_text)
    changes.extend(
        [
            (vault / ".kb" / "state" / "processed_files.json", processed_data, "replace-state"),
            (vault / ".kb" / "state" / "publish_queue.json", queue_data, "replace-state"),
        ]
    )
    preparation_duration_ms = (time.perf_counter() - preparation_started) * 1000
    apply_started = time.perf_counter()
    verification = apply_transaction(vault, changes)
    apply_duration_ms = (time.perf_counter() - apply_started) * 1000
    if consume:
        manifest_path.unlink(missing_ok=True)
    digest = normalize_hash(source["sha256"])
    performance, performance_log = finalize_ingest_performance(
        vault,
        digest,
        commit_started,
        validation_duration_ms,
        preparation_duration_ms,
        apply_duration_ms,
    )
    return {
        "ok": True,
        "mode": "commit",
        "source_sha256": digest,
        "route": manifest["route"],
        "outputs_verified": len(verification),
        "verification": verification,
        "performance": performance,
        "performance_log": performance_log,
        **state_result,
    }


def find_matching_file(files: list[Path], source_file: str) -> Path | None:
    stem = Path(source_file).stem
    exact = [path for path in files if path.name == source_file]
    if exact:
        return exact[0]
    contains = [path for path in files if stem and stem in path.stem]
    return sorted(contains, key=lambda path: (len(path.name), str(path)))[0] if contains else None


def repair_state(
    vault: Path,
    write: bool,
    repair_missing_text_extractions: bool = False,
) -> dict[str, Any]:
    vault = vault.resolve()
    state_dir = vault / ".kb" / "state"
    processed_path = state_dir / "processed_files.json"
    queue_path = state_dir / "publish_queue.json"
    existing_processed = load_json(processed_path, {"version": 1, "files": []})
    existing_by_hash = {
        str(item.get("sha256", "")).lower(): item
        for item in existing_processed.get("files", [])
        if isinstance(item, dict) and HASH_RE.fullmatch(str(item.get("sha256", "")).lower())
    }
    existing_queue = load_json(queue_path, {"version": 1, "items": []})
    existing_queue_by_path = {
        str(item.get("local_path", "")): item
        for item in existing_queue.get("items", [])
        if isinstance(item, dict)
    }
    try:
        verified_space_name, _ = load_verified_space_mapping(vault)
    except PipelineError:
        verified_space_name = ""

    originals = [path for path in (vault / "10_来源/原件").rglob("*") if path.is_file()]
    extractions = [path for path in (vault / "10_来源/提取").rglob("*") if path.is_file()]
    note_paths = []
    for root_name in ("20_知识/个人", "20_知识/企业"):
        root = vault / root_name
        if root.exists():
            note_paths.extend(path for path in root.rglob("*.md") if path.is_file())

    discovered: dict[str, dict[str, Any]] = {}
    queue_items: list[dict[str, Any]] = []
    missing_original: list[str] = []
    missing_extraction: list[str] = []
    extraction_repairs: list[tuple[Path, bytes, str]] = []
    repaired_extractions: list[str] = []
    for note_path in sorted(note_paths):
        text = note_path.read_text(encoding="utf-8-sig")
        fm = parse_frontmatter_text(text)
        raw_hash = str(fm.get("source_hash", "")).lower().removeprefix("sha256:")
        if not HASH_RE.fullmatch(raw_hash):
            continue
        existing_record = existing_by_hash.get(raw_hash, {})
        source_file = str(fm.get("source_file") or existing_record.get("source_name", ""))
        stored = None
        existing_stored = existing_record.get("stored_original")
        if isinstance(existing_stored, str) and existing_stored:
            candidate_stored = vault / Path(existing_stored)
            if candidate_stored.is_file():
                stored = candidate_stored
        configured_source_value = fm.get("source_path") or existing_record.get("source_path")
        configured_source = Path(str(configured_source_value)) if configured_source_value else None
        if configured_source is not None and not configured_source.is_absolute():
            configured_source = vault / configured_source
        if configured_source and configured_source.is_file():
            rel = relative_to_vault(configured_source, vault)
            if rel and rel.startswith("10_来源/原件/"):
                stored = configured_source
        if stored is None:
            stored = find_matching_file(originals, source_file)
        extraction = None
        existing_extraction = existing_record.get("extraction")
        if isinstance(existing_extraction, str) and existing_extraction:
            candidate_extraction = vault / Path(existing_extraction)
            if candidate_extraction.is_file():
                extraction = candidate_extraction
        if extraction is None:
            extraction = find_matching_file(extractions, source_file)
        rel_note = note_path.relative_to(vault).as_posix()
        if (
            extraction is None
            and stored is not None
            and stored.suffix.lower() in TEXT_EXTENSIONS
            and repair_missing_text_extractions
        ):
            extraction = vault / "10_来源/提取" / "文档正文" / f"{stored.stem}.md"
            if not write:
                raise PipelineError("--repair-missing-text-extractions requires --write")
            extraction_repairs.append((extraction, stored.read_bytes(), "create"))
            repaired_extractions.append(extraction.relative_to(vault).as_posix())
        if stored is None:
            missing_original.append(rel_note)
        if extraction is None:
            missing_extraction.append(rel_note)

        record = {
            "sha256": raw_hash,
            "source_name": source_file,
            "source_path": stored.relative_to(vault).as_posix() if stored else "",
            "stored_original": stored.relative_to(vault).as_posix() if stored else "",
            "extraction": extraction.relative_to(vault).as_posix() if extraction else "",
            "source_note": rel_note,
            "scope": fm.get("scope", ""),
            "processed_at": fm.get("updated") or fm.get("created") or "",
            "status": "complete" if stored and extraction else "incomplete",
        }
        record.update(structure_fields_from_frontmatter(fm))
        if raw_hash in existing_by_hash:
            richer = {
                key: value
                for key, value in existing_by_hash[raw_hash].items()
                if value not in {"", None}
            }
            richer.update({key: value for key, value in record.items() if value not in {"", None}})
            record = richer
        discovered[raw_hash] = record

        eligible = (
            under_prefix(note_path.relative_to(vault).as_posix(), "20_知识/企业")
            and fm.get("scope") == "enterprise"
            and fm.get("publish_to_feishu") is False
            and fm.get("status") == "ready-to-publish"
            and fm.get("publish_status") == "pending"
            and fm.get("review_status") == "reviewed"
            and fm.get("sensitivity") not in {"restricted", "personal-sensitive"}
            and bool(str(fm.get("feishu_parent_node_name", "")).strip())
            and bool(verified_space_name)
            and fm.get("feishu_space_name") == verified_space_name
        )
        if eligible:
            queue_item = {
                "local_path": rel_note,
                "local_source_hash": raw_hash,
                "writer": fm.get("feishu_writer", "lark-cli"),
                "space_name": verified_space_name,
                "parent_node_name": fm["feishu_parent_node_name"],
                "sync_status": "pending",
            }
            if rel_note in existing_queue_by_path:
                queue_item.update(existing_queue_by_path[rel_note])
                queue_item["sync_status"] = "pending"
            queue_items.append(queue_item)

    files = sorted(discovered.values(), key=lambda item: (str(item.get("processed_at", "")), item["source_note"]))
    processed_new = {
        "version": 2,
        "files": files,
        "by_sha256": {item["sha256"]: index for index, item in enumerate(files)},
    }
    queue_new = {
        "version": 2,
        "items": sorted(queue_items, key=lambda item: item["local_path"]),
    }

    backups: list[str] = []
    if write:
        state_dir.mkdir(parents=True, exist_ok=True)
        backup_dir = state_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        for state_path in (processed_path, queue_path):
            if state_path.exists():
                backup = backup_dir / f"{state_path.stem}-{stamp}.json"
                shutil.copy2(state_path, backup)
                backups.append(backup.relative_to(vault).as_posix())
        changes = extraction_repairs + [
            (
                processed_path,
                (json.dumps(processed_new, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                "replace-state",
            ),
            (
                queue_path,
                (json.dumps(queue_new, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                "replace-state",
            ),
        ]
        apply_transaction(vault, changes)

    return {
        "ok": True,
        "mode": "repair-state",
        "write": write,
        "processed_records": len(files),
        "pending_records": len(queue_items),
        "missing_original": missing_original,
        "missing_extraction": missing_extraction,
        "repaired_extractions": repaired_extractions,
        "backups": backups,
        "processed_preview": processed_new,
        "queue_preview": queue_new,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--vault", required=True, type=Path)
    preflight_parser.add_argument("--source", required=True, type=Path)
    preflight_parser.add_argument("--include-text", action="store_true")
    preflight_parser.add_argument("--out", type=Path)

    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("--vault", required=True, type=Path)
    commit_parser.add_argument("--manifest", required=True, type=Path)
    commit_parser.add_argument("--consume", action="store_true")

    repair_parser = subparsers.add_parser("repair-state")
    repair_parser.add_argument("--vault", required=True, type=Path)
    repair_parser.add_argument("--write", action="store_true")
    repair_parser.add_argument("--repair-missing-text-extractions", action="store_true")

    capture = subparsers.add_parser("capture-link")
    capture.add_argument("--vault", required=True, type=Path)
    capture.add_argument("--url", required=True)
    capture.add_argument("--output-dir", required=True, type=Path)
    capture.add_argument("--name")
    capture.add_argument("--timeout", type=int, default=30)

    github = subparsers.add_parser("capture-github-subtree")
    github.add_argument("--vault", required=True, type=Path)
    github.add_argument("--repo", required=True)
    github.add_argument("--ref", required=True)
    github.add_argument("--subpath", required=True)
    github.add_argument("--source-url", action="append", default=[])
    github.add_argument("--output-dir", required=True, type=Path)
    github.add_argument("--name")

    expand = subparsers.add_parser("expand-web-corpus")
    expand.add_argument("--vault", required=True, type=Path)
    expand.add_argument("--archive", required=True)
    expand.add_argument("--archive-sha256", required=True)
    expand.add_argument("--source-prefix", required=True)
    expand.add_argument("--target", required=True)
    expand.add_argument("--landing", required=True)
    expand.add_argument("--title", required=True)
    expand.add_argument("--expected-tree-sha256")
    expand.add_argument("--write", action="store_true")
    expand.add_argument("--replace", action="store_true")

    link_repair = subparsers.add_parser("repair-web-corpus")
    link_repair.add_argument("--vault", required=True, type=Path)
    link_repair.add_argument("--rules", required=True, type=Path)
    link_repair.add_argument("--write", action="store_true")
    link_repair.add_argument("--details", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        from runtime_access import authorize
        from vault_context import discover_vault

        args.vault = discover_vault(args.vault)
        authorize(args.vault, "collect")
        if args.command in {"capture-link", "capture-github-subtree"}:
            import capture_link

            result = (
                capture_link.capture_single(args)
                if args.command == "capture-link"
                else capture_link.capture_github_subtree(args)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "expand-web-corpus":
            import expand_web_corpus

            print(json.dumps(expand_web_corpus.expand(args), ensure_ascii=False, indent=2))
        elif args.command == "repair-web-corpus":
            import repair_web_corpus_links

            print(json.dumps(repair_web_corpus_links.run(args), ensure_ascii=False, indent=2))
        elif args.command == "preflight":
            result = preflight(args.vault, args.source, args.include_text)
            rendered = json.dumps(result, ensure_ascii=False, indent=2)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(rendered + "\n", encoding="utf-8")
            print(rendered)
        elif args.command == "commit":
            print(json.dumps(commit(args.vault, args.manifest, args.consume), ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    repair_state(
                        args.vault,
                        args.write,
                        args.repair_missing_text_extractions,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

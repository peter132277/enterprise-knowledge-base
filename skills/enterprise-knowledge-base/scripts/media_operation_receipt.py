#!/usr/bin/env python3
"""Record local media-workflow stages and finalize verified Feishu receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


HASH_RE = re.compile(r"[0-9a-f]{64}")
RECEIPT_RE = re.compile(r"media-operation-(?P<sha>[0-9a-f]{64})\.json")
FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
STAGES = (
    "preflight_started",
    "preflight_complete",
    "preview_ready",
    "confirmed",
    "drive_uploaded",
    "minute_created",
    "transcript_ready",
    "local_commit_complete",
)


class ReceiptError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"Unable to read JSON: {path}") from exc


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def now_record() -> dict[str, Any]:
    epoch_ms = time.time_ns() // 1_000_000
    recorded_at = datetime.fromtimestamp(epoch_ms / 1000).astimezone()
    return {
        "recorded_at": recorded_at.isoformat(timespec="milliseconds"),
        "epoch_ms": epoch_ms,
    }


def preflight_records(vault: Path, digest: str) -> dict[str, dict[str, Any]]:
    session = load_json(vault / ".kb" / "temp" / "ingest-performance" / f"{digest}.json")
    if (
        not isinstance(session, dict)
        or session.get("schema") != "kb-ingest-performance-session/v1"
        or session.get("source_sha256") != digest
    ):
        raise ReceiptError("Matching managed preflight timing session is required")
    try:
        started = datetime.fromisoformat(str(session["started_at"]))
        duration_ms = float(session["preflight_duration_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReceiptError("Managed preflight timing session is invalid") from exc
    if duration_ms < 0:
        raise ReceiptError("Managed preflight duration cannot be negative")
    started_ms = int(round(started.timestamp() * 1000))
    completed_ms = started_ms + int(round(duration_ms))
    completed = datetime.fromtimestamp(completed_ms / 1000, tz=started.tzinfo)
    return {
        "preflight_started": {
            "recorded_at": started.isoformat(timespec="milliseconds"),
            "epoch_ms": started_ms,
        },
        "preflight_complete": {
            "recorded_at": completed.isoformat(timespec="milliseconds"),
            "epoch_ms": completed_ms,
        },
    }


def validate_feishu_url(value: str, path_prefix: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not any(host.endswith(suffix) for suffix in FEISHU_HOST_SUFFIXES)
        or path_prefix not in parsed.path
    ):
        raise ReceiptError(f"Invalid Feishu URL for {path_prefix}: {value}")
    return value


def receipt_path(vault: Path, digest: str) -> Path:
    if not HASH_RE.fullmatch(digest):
        raise ReceiptError("source_sha256 must be 64 lowercase hexadecimal characters")
    return vault / ".kb" / "temp" / f"media-operation-{digest}.json"


def resolve_receipt(vault: Path, value: str) -> tuple[Path, str]:
    path = Path(value)
    path = (vault / path).resolve() if not path.is_absolute() else path.resolve()
    temp_root = (vault / ".kb" / "temp").resolve()
    match = RECEIPT_RE.fullmatch(path.name)
    if path.parent != temp_root or not match or not path.is_file():
        raise ReceiptError("Receipt must be an existing media-operation JSON directly under .kb/temp")
    return path, match.group("sha")


def validate_receipt(payload: Any, digest: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != "kb-media-operation/v1":
        raise ReceiptError("Unsupported media operation receipt schema")
    if payload.get("source_sha256") != digest:
        raise ReceiptError("Receipt filename and source hash do not match")
    if payload.get("provider") != "feishu-minutes" or payload.get("identity") != "user":
        raise ReceiptError("Receipt provider or identity is not eligible")
    timing = payload.get("timing")
    if not isinstance(timing, dict) or timing.get("schema") != "kb-media-timing/v1":
        raise ReceiptError("Receipt has no managed media timing state")
    if not isinstance(timing.get("stages"), dict):
        raise ReceiptError("Receipt timing stages are invalid")
    return payload


def start_receipt(args: argparse.Namespace) -> dict[str, Any]:
    vault = Path(args.vault).resolve()
    digest = args.source_sha256.strip().lower()
    path = receipt_path(vault, digest)
    if path.exists():
        payload = validate_receipt(load_json(path), digest)
        if payload.get("source_name") != args.source_name:
            raise ReceiptError("Existing receipt source name does not match")
        return {"ok": True, "created": False, "receipt": path.relative_to(vault).as_posix()}
    payload = {
        "schema": "kb-media-operation/v1",
        "source_name": Path(args.source_name).name,
        "source_sha256": digest,
        "provider": "feishu-minutes",
        "identity": "user",
        "status": "in-progress",
        "timing": {
            "schema": "kb-media-timing/v1",
            "stages": preflight_records(vault, digest),
        },
    }
    atomic_write_json(path, payload)
    return {"ok": True, "created": True, "receipt": path.relative_to(vault).as_posix()}


def mark_stage(args: argparse.Namespace) -> dict[str, Any]:
    vault = Path(args.vault).resolve()
    path, digest = resolve_receipt(vault, args.receipt)
    payload = validate_receipt(load_json(path), digest)
    if payload.get("status") == "collected-and-verified":
        raise ReceiptError("Completed receipt cannot accept another stage")
    stages = payload["timing"]["stages"]
    stage = args.stage
    if stage in stages:
        return {"ok": True, "recorded": False, "stage": stage}
    previous = STAGES[STAGES.index(stage) - 1]
    if previous not in stages:
        raise ReceiptError(f"Cannot record {stage} before {previous}")
    if stage == "drive_uploaded":
        payload["source_file_url"] = validate_feishu_url(args.source_file_url, "/file/")
    elif stage == "minute_created":
        payload["minute_url"] = validate_feishu_url(args.minute_url, "/minutes/")
    elif stage in {"transcript_ready", "local_commit_complete"}:
        validate_feishu_url(str(payload.get("source_file_url", "")), "/file/")
        validate_feishu_url(str(payload.get("minute_url", "")), "/minutes/")
    if stage == "local_commit_complete":
        for key in ("stored_original_path", "extraction_path", "source_note_path"):
            value = str(getattr(args, key, "")).strip()
            if not value:
                raise ReceiptError(f"{key} is required for local_commit_complete")
            payload[key] = value
        payload["status"] = "collected-and-verified"
    stages[stage] = now_record()
    atomic_write_json(path, payload)
    return {"ok": True, "recorded": True, "stage": stage}


def stage_durations(payload: dict[str, Any]) -> dict[str, int]:
    stages = payload["timing"]["stages"]
    missing = [stage for stage in STAGES if stage not in stages]
    if missing:
        raise ReceiptError(f"Receipt timing is incomplete: {missing}")
    points = [int(stages[stage]["epoch_ms"]) for stage in STAGES]
    if any(later < earlier for earlier, later in zip(points, points[1:])):
        raise ReceiptError("Receipt timing stages are not monotonic")
    return {
        "preflight_ms": points[1] - points[0],
        "preview_generation_ms": points[2] - points[1],
        "pre_confirmation_total_ms": points[2] - points[0],
        "user_confirmation_wait_ms": points[3] - points[2],
        "post_confirm_to_drive_ms": points[4] - points[3],
        "minute_creation_ms": points[5] - points[4],
        "transcript_wait_ms": points[6] - points[5],
        "local_collection_ms": points[7] - points[6],
        "end_to_end_ms": points[7] - points[0],
    }


def resolve_vault_file(vault: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ReceiptError(f"{label} must be Vault-relative")
    resolved = (vault / relative).resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise ReceiptError(f"{label} is outside the Vault") from exc
    if not resolved.is_file():
        raise ReceiptError(f"{label} not found: {resolved}")
    return resolved


def finalize_receipt(args: argparse.Namespace) -> dict[str, Any]:
    vault = Path(args.vault).resolve()
    path, digest = resolve_receipt(vault, args.receipt)
    payload = validate_receipt(load_json(path), digest)
    if payload.get("status") != "collected-and-verified":
        raise ReceiptError("Only a collected-and-verified receipt may be deleted")
    durations = stage_durations(payload)
    file_url = validate_feishu_url(str(payload.get("source_file_url", "")), "/file/")
    minute_url = validate_feishu_url(str(payload.get("minute_url", "")), "/minutes/")
    state = load_json(vault / ".kb" / "state" / "processed_files.json")
    records = state.get("files", []) if isinstance(state, dict) else []
    record = next((item for item in records if isinstance(item, dict) and item.get("sha256") == digest), None)
    if not record or record.get("status") != "complete":
        raise ReceiptError("No complete managed collection state matches the receipt")
    profile = record.get("extraction_profile")
    expected = {"provider": "feishu-minutes", "source_sha256": digest,
                "source_file_url": file_url, "minute_url": minute_url}
    if not isinstance(profile, dict) or any(profile.get(key) != value for key, value in expected.items()):
        raise ReceiptError("Managed extraction profile does not match the receipt")
    if record.get("source_feishu_file_url") != file_url:
        raise ReceiptError("Managed source URL does not match the receipt")
    paths: dict[str, Path] = {}
    for key in ("stored_original", "extraction", "source_note"):
        receipt_value = str(payload.get(f"{key}_path", "")).strip()
        state_value = str(record.get(key, "")).strip()
        if not receipt_value or receipt_value != state_value:
            raise ReceiptError(f"Receipt {key} does not match managed state")
        paths[key] = resolve_vault_file(vault, state_value, key)
    if sha256_file(paths["stored_original"]) != digest:
        raise ReceiptError("Stored original hash does not match the receipt")
    if sha256_file(paths["extraction"]) != str(profile.get("transcript_sha256", "")):
        raise ReceiptError("Stored transcript hash does not match managed state")
    if file_url not in paths["source_note"].read_text(encoding="utf-8-sig"):
        raise ReceiptError("Source note does not preserve the verified Drive URL")
    completed = payload["timing"]["stages"]["local_commit_complete"]["recorded_at"]
    date = completed[:10]
    log = vault / ".kb" / "logs" / "media-performance" / date / f"{digest[:12]}.json"
    atomic_write_json(log, {
        "schema": "kb-media-performance/v1",
        "source_sha256": digest,
        "source_name": payload["source_name"],
        "provider": "feishu-minutes",
        "stages": payload["timing"]["stages"],
        "durations_ms": durations,
    })
    path.unlink()
    return {"ok": True, "deleted": path.relative_to(vault).as_posix(),
            "performance_log": log.relative_to(vault).as_posix(), "durations_ms": durations}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--vault", default=".")
    start.add_argument("--source-sha256", required=True)
    start.add_argument("--source-name", required=True)
    mark = commands.add_parser("mark")
    mark.add_argument("--vault", default=".")
    mark.add_argument("--receipt", required=True)
    mark.add_argument("--stage", choices=STAGES[2:], required=True)
    mark.add_argument("--source-file-url", default="")
    mark.add_argument("--minute-url", default="")
    mark.add_argument("--stored-original-path", default="")
    mark.add_argument("--extraction-path", default="")
    mark.add_argument("--source-note-path", default="")
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--vault", default=".")
    finalize.add_argument("--receipt", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = start_receipt(args) if args.command == "start" else (
            mark_stage(args) if args.command == "mark" else finalize_receipt(args)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ReceiptError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

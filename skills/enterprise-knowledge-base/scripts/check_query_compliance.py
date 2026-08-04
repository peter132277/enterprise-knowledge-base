#!/usr/bin/env python3
"""Fail closed unless a knowledge answer is backed by a valid local query receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from query_current_vault import (
    PROJECT_VAULT,
    query_sha256,
    sha256_file,
    sha256_json,
)


ALLOWED_ROOTS = ("20_知识", "30_导航", "10_来源/提取")


class ComplianceError(RuntimeError):
    pass


def normalize_vault_path(vault: Path, value: str) -> str:
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (vault / candidate).resolve()
    if not path.is_relative_to(vault.resolve()):
        raise ComplianceError(f"Path is outside the current Vault: {value}")
    return path.relative_to(vault.resolve()).as_posix()


def is_allowed_knowledge_path(relative: str) -> bool:
    return any(
        relative == root or relative.startswith(root.rstrip("/") + "/")
        for root in ALLOWED_ROOTS
    )


def load_receipt(vault: Path, value: str) -> tuple[Path, dict[str, Any]]:
    relative = normalize_vault_path(vault, value)
    if not relative.startswith(".kb/logs/查询/") or not relative.endswith(".json"):
        raise ComplianceError("Receipt must be under .kb/logs/查询")
    path = vault / Path(relative)
    if not path.is_file():
        raise ComplianceError(f"Receipt not found: {relative}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComplianceError(f"Invalid receipt: {exc}") from exc
    if not isinstance(receipt, dict):
        raise ComplianceError("Receipt must be a JSON object")
    return path, receipt


def verify_receipt(
    vault: Path,
    question: str,
    receipt: dict[str, Any],
    cited_paths: list[str],
    no_result: bool,
    rejected_paths: list[str] | None = None,
) -> dict[str, Any]:
    if receipt.get("schema_version") != 1:
        raise ComplianceError("Unsupported receipt schema")
    recorded_body_hash = str(receipt.get("receipt_body_sha256", ""))
    body = dict(receipt)
    body.pop("receipt_body_sha256", None)
    if sha256_json(body) != recorded_body_hash:
        raise ComplianceError("Receipt body hash mismatch")
    if str(receipt.get("mode", "")) != "current-vault-only":
        raise ComplianceError("Receipt mode is not current-vault-only")
    if Path(str(receipt.get("vault", ""))).resolve() != vault.resolve():
        raise ComplianceError("Receipt Vault differs from the current Vault")
    if str(receipt.get("query_sha256", "")) != query_sha256(question):
        raise ComplianceError("Receipt question hash does not match")
    if bool(receipt.get("external_sources_used", True)):
        raise ComplianceError("Receipt used an external source")
    retrieval_backend = str(receipt.get("retrieval_backend", "legacy-python"))
    if retrieval_backend not in {
        "obsidian-cli",
        "rg",
        "python-filesystem",
        "legacy-python",
    }:
        raise ComplianceError(f"Unsupported retrieval backend: {retrieval_backend}")

    searched_roots = receipt.get("searched_roots", [])
    if not isinstance(searched_roots, list) or not searched_roots:
        raise ComplianceError("Receipt has no searched roots")
    for root in searched_roots:
        relative = str(root).replace("\\", "/").strip("/")
        if not is_allowed_knowledge_path(relative):
            raise ComplianceError(f"Disallowed search root: {relative}")

    results = receipt.get("results", [])
    if not isinstance(results, list):
        raise ComplianceError("Receipt results must be a list")
    if receipt.get("result_count") != len(results):
        raise ComplianceError("Receipt result count mismatch")
    result_paths: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            raise ComplianceError("Receipt result must be an object")
        relative = normalize_vault_path(vault, str(item.get("path", "")))
        if not is_allowed_knowledge_path(relative):
            raise ComplianceError(f"Result is outside knowledge roots: {relative}")
        path = vault / Path(relative)
        if not path.is_file():
            raise ComplianceError(f"Result file is missing: {relative}")
        if sha256_file(path) != str(item.get("file_sha256", "")):
            raise ComplianceError(f"Result file changed after retrieval: {relative}")
        result_paths.add(relative)

    citations = [normalize_vault_path(vault, value) for value in cited_paths]
    rejections = [
        normalize_vault_path(vault, value)
        for value in (rejected_paths or [])
    ]
    if results:
        if no_result:
            if citations:
                raise ComplianceError(
                    "A semantic no-result cannot cite a candidate"
                )
            if set(rejections) != result_paths:
                raise ComplianceError(
                    "Semantic no-result requires rejecting every returned candidate"
                )
        else:
            if not citations:
                raise ComplianceError("At least one cited path is required")
            for relative in citations:
                if relative not in result_paths:
                    raise ComplianceError(
                        f"Citation is not present in the query receipt: {relative}"
                    )
            if rejections:
                raise ComplianceError(
                    "Rejected candidates are only valid with --no-result"
                )
    else:
        if not no_result:
            raise ComplianceError("A zero-result answer must use --no-result")
        if citations:
            raise ComplianceError("A zero-result answer cannot cite a result")
        if rejections:
            raise ComplianceError(
                "A lexical zero-result has no candidates to reject"
            )

    query_id = str(receipt.get("query_id", ""))
    if not query_id:
        raise ComplianceError("Receipt query_id is missing")
    audit_material = {
        "query_id": query_id,
        "question_sha256": query_sha256(question),
        "receipt_body_sha256": recorded_body_hash,
        "cited_paths": citations,
        "rejected_paths": rejections,
        "no_result": no_result,
    }
    audit_id = hashlib.sha256(
        json.dumps(
            audit_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "ok": True,
        "mode": "query-compliance-audit",
        "query_id": query_id,
        "audit_id": audit_id,
        "vault": str(vault.resolve()),
        "result_count": len(results),
        "retrieval_backend": retrieval_backend,
        "approved_citations": citations,
        "rejected_candidates": rejections,
        "external_sources_used": False,
        "answer_contract": {
            "retrieval_scope": "当前知识库",
            "query_receipt": query_id,
            "compliance_audit": audit_id,
            "external_sources": "未使用",
            "no_result": no_result,
        },
    }


def write_audit(
    vault: Path,
    receipt_path: Path,
    result: dict[str, Any],
) -> str:
    now = datetime.now().astimezone()
    audit_path = receipt_path.with_name(
        receipt_path.stem + f".audit-{result['audit_id']}.json"
    )
    payload = {
        "schema_version": 1,
        "created_at": now.isoformat(timespec="milliseconds"),
        **result,
    }
    handle, temp_name = tempfile.mkstemp(
        prefix=audit_path.name + ".",
        suffix=".tmp",
        dir=audit_path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, audit_path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return audit_path.relative_to(vault).as_posix()


def run(
    vault: Path,
    question: str,
    receipt_value: str,
    cited_paths: list[str],
    no_result: bool,
    persist_audit: bool = True,
    enforce_project: bool = True,
    rejected_paths: list[str] | None = None,
) -> dict[str, Any]:
    audit_started = time.perf_counter()
    if enforce_project and vault.resolve() != PROJECT_VAULT.resolve():
        raise ComplianceError(f"Audit is restricted to {PROJECT_VAULT}")
    load_started = time.perf_counter()
    receipt_path, receipt = load_receipt(vault.resolve(), receipt_value)
    load_duration_ms = (time.perf_counter() - load_started) * 1000
    verification_started = time.perf_counter()
    result = verify_receipt(
        vault.resolve(),
        question,
        receipt,
        cited_paths,
        no_result,
        rejected_paths,
    )
    verification_duration_ms = (
        time.perf_counter() - verification_started
    ) * 1000
    now = datetime.now().astimezone()
    receipt_to_audit_ms: float | None = None
    try:
        receipt_created = datetime.fromisoformat(str(receipt["created_at"]))
        receipt_to_audit_ms = (now - receipt_created).total_seconds() * 1000
    except (KeyError, TypeError, ValueError):
        receipt_to_audit_ms = None
    search_duration_ms = float(
        (receipt.get("performance") or {}).get("search_duration_ms", 0.0)
    )
    result["performance"] = {
        "receipt_load_duration_ms": round(load_duration_ms, 1),
        "verification_duration_ms": round(verification_duration_ms, 1),
        "receipt_to_audit_ms": (
            round(receipt_to_audit_ms, 1)
            if receipt_to_audit_ms is not None
            else None
        ),
        "workflow_elapsed_ms": (
            round(receipt_to_audit_ms + search_duration_ms, 1)
            if receipt_to_audit_ms is not None
            else None
        ),
        "audit_before_persist_ms": round(
            (time.perf_counter() - audit_started) * 1000,
            1,
        ),
    }
    if persist_audit:
        persist_started = time.perf_counter()
        result["audit_path"] = write_audit(vault.resolve(), receipt_path, result)
        result["performance"]["persist_audit_duration_ms"] = round(
            (time.perf_counter() - persist_started) * 1000,
            1,
        )
    result["performance"]["audit_total_duration_ms"] = round(
        (time.perf_counter() - audit_started) * 1000,
        1,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--cited-path", action="append", default=[])
    parser.add_argument("--reject-path", action="append", default=[])
    parser.add_argument("--no-result", action="store_true")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        result = run(
            PROJECT_VAULT,
            args.question,
            args.receipt,
            args.cited_path,
            args.no_result,
            rejected_paths=args.reject_path,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ComplianceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

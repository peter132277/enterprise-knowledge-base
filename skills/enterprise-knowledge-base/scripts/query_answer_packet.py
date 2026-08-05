#!/usr/bin/env python3
"""Build a fail-closed local evidence packet and auto-audit only clear rankings."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import check_query_compliance as compliance
import company_sync_coordinator as sync_coordinator
import query_current_vault as query_engine
from vault_context import discover_vault


MIN_TOP_SCORE = 20
SELECTION_RATIO = 0.60
SEPARATION_RATIO = 0.55
MAX_AUTO_CITATIONS = 2
SESSION_ENV_KEYS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")


def resolve_session_id(explicit: str = "") -> str:
    value = explicit.strip()
    if value:
        return value
    for key in SESSION_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError(
        "A stable Codex task identifier is required for the all-knowledge query."
    )


def select_high_confidence(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    if not results:
        return [], "no-result"
    top_score = int(results[0].get("score", 0))
    if top_score < MIN_TOP_SCORE:
        return [], "top-score-below-threshold"

    selected = [
        item
        for item in results
        if int(item.get("score", 0)) >= top_score * SELECTION_RATIO
    ]
    if len(selected) > MAX_AUTO_CITATIONS:
        return [], "too-many-strong-candidates"

    next_index = len(selected)
    if next_index < len(results):
        next_score = int(results[next_index].get("score", 0))
        boundary_score = int(selected[-1].get("score", 0))
        if next_score >= boundary_score * SEPARATION_RATIO:
            return [], "ranking-not-separated"
    return selected, "high-confidence-ranking"


def compact_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("path", "title", "score", "file_sha256", "snippet")
        if key in item
    }


def run_fast_query(
    vault: Path,
    question: str,
    include_sources: bool = False,
    max_results: int = 8,
    backend: str = "auto",
    enforce_project: bool = True,
    session_id: str = "",
    sync_reader: Any | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    stable_session_id = resolve_session_id(session_id)
    sync_result = sync_coordinator.before_knowledge_query(
        vault,
        stable_session_id,
        reader=sync_reader,
    )
    query_result = query_engine.run_query(
        vault,
        question,
        "all",
        include_sources,
        max_results,
        backend=backend,
        enforce_project=enforce_project,
        write_receipt=True,
        sync_credential=sync_result["sync_credential"],
    )
    selected, reason = select_high_confidence(query_result["results"])
    if query_result["results"] and not selected:
        return {
            "ok": True,
            "mode": "current-vault-answer-packet",
            "status": "manual-audit-required",
            "reason": reason,
            "query_id": query_result["query_id"],
            "receipt_path": query_result["receipt_path"],
            "retrieval_backend": query_result["retrieval_backend"],
            "sync": {
                "status": sync_result["sync"],
                "remote_reads": sync_result.get("remote_reads"),
            },
            "results": [
                compact_result(item) for item in query_result["results"]
            ],
            "performance": {
                **query_result["performance"],
                "packet_total_duration_ms": round(
                    (time.perf_counter() - started) * 1000,
                    1,
                ),
            },
        }

    no_result = not query_result["results"]
    citations = [str(item["path"]) for item in selected]
    audit = compliance.run(
        vault,
        question,
        query_result["receipt_path"],
        citations,
        no_result,
        persist_audit=True,
        enforce_project=enforce_project,
    )
    return {
        "ok": True,
        "mode": "current-vault-answer-packet",
        "status": "audited-no-result" if no_result else "audited-fast-path",
        "reason": reason,
        "query_id": query_result["query_id"],
        "receipt_path": query_result["receipt_path"],
        "audit_id": audit["audit_id"],
        "audit_path": audit["audit_path"],
        "retrieval_backend": query_result["retrieval_backend"],
        "sync": {
            "status": sync_result["sync"],
            "remote_reads": sync_result.get("remote_reads"),
        },
        "approved_citations": audit["approved_citations"],
        "external_sources_used": False,
        "answer_contract": audit["answer_contract"],
        "results": [compact_result(item) for item in selected],
        "performance": {
            "query": query_result["performance"],
            "audit": audit["performance"],
            "packet_total_duration_ms": round(
                (time.perf_counter() - started) * 1000,
                1,
            ),
        },
    }


def run_manual_audit(
    vault: Path,
    question: str,
    receipt: str,
    cited_paths: list[str],
    rejected_paths: list[str],
    no_result: bool,
    *,
    enforce_project: bool = True,
) -> dict[str, Any]:
    return compliance.run(
        vault,
        question,
        receipt,
        cited_paths,
        no_result,
        persist_audit=True,
        enforce_project=enforce_project,
        rejected_paths=rejected_paths,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--receipt")
    parser.add_argument("--cited-path", action="append", default=[])
    parser.add_argument("--reject-path", action="append", default=[])
    parser.add_argument("--no-result", action="store_true")
    parser.add_argument("--include-sources", action="store_true")
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument(
        "--backend",
        choices=("auto", "rg", "python"),
        default="auto",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        vault = discover_vault()
        if args.receipt:
            result = run_manual_audit(
                vault,
                args.query,
                args.receipt,
                args.cited_path,
                args.reject_path,
                args.no_result,
            )
        else:
            if args.cited_path or args.reject_path or args.no_result:
                raise RuntimeError("Manual audit options require --receipt.")
            result = run_fast_query(
                vault,
                args.query,
                args.include_sources,
                args.max_results,
                args.backend,
                enforce_project=True,
                session_id=args.session_id or "",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RuntimeError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

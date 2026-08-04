#!/usr/bin/env python3
"""Reproducible local benchmark for the same-task query validation path."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/enterprise-knowledge-base/scripts"
sys.path.insert(0, str(SCRIPTS))

import company_sync_coordinator as coordinator
import kb_core
import membership_policy
import runtime_access


def write_json(path: Path, value: Any) -> None:
    kb_core.atomic_write_json(path, value)


def prepare_consistent_state(vault: Path, count: int) -> None:
    mirror = vault / coordinator.MIRROR_ROOT
    mirror.mkdir(parents=True)
    nodes = []
    records: dict[str, dict[str, Any]] = {}
    content = b"# Company knowledge\n"
    digest = kb_core.sha256_bytes(content)
    for index in range(count):
        token = f"node-{index:05d}"
        relative = (coordinator.MIRROR_ROOT / f"{token}.md").as_posix()
        path = vault / Path(relative)
        path.write_bytes(content)
        stat = path.stat()
        nodes.append(
            {"node_name": token, "node_token": token, "verified": True}
        )
        records[token] = {
            "source": "mirror",
            "local_path": relative,
            "node_fingerprint": kb_core.canonical_hash(token),
            "obj_token": f"doc-{index:05d}",
            "revision_id": "1",
            "file_sha256": digest,
            "file_size": stat.st_size,
            "file_mtime_ns": stat.st_mtime_ns,
        }

    mapping = {
        "version": 2,
        "space_name": "Benchmark",
        "space_id": "123",
        "nodes": nodes,
    }
    sync_mapping_hash = kb_core.canonical_hash(mapping)
    scope = membership_policy.resolve_share_scope(
        "all-employees",
        [
            {
                "kind": "organization-root",
                "selector_id": "root-1",
                "display_name": "All employees",
                "verified": True,
                "internal": True,
            }
        ],
    )
    write_json(vault / ".kb/mappings/feishu_nodes.json", mapping)
    access_mapping_hash = kb_core.canonical_hash(kb_core.mapping_value(vault))
    write_json(
        vault / ".kb/config/organization.json",
        {
            "schema": kb_core.ORGANIZATION_SCHEMA,
            "role": "admin",
            "share_scope": scope,
            "publish_policy": "members",
            "membership_verified": True,
        },
    )
    write_json(
        vault / ".kb/state/membership-verification.json",
        {
            "verified": True,
            "mapping_hash": access_mapping_hash,
            "share_scope_hash": kb_core.canonical_hash(scope),
            "effective_employee_policy": "members",
            "member_list_hash": "a" * 64,
        },
    )
    access = kb_core.company_access_snapshot(vault)
    state = {
        "schema": kb_core.COMPANY_SYNC_SCHEMA,
        "space_id": "123",
        "mapping_hash": sync_mapping_hash,
        "access_snapshot": access,
        "last_sync_at": "benchmark",
        "freshness": "current",
        "tree_hash": kb_core.canonical_hash(sorted(records)),
        "records": records,
    }
    write_json(vault / ".kb/state/company_sync.json", state)
    coordinator.write_session_receipt(
        vault,
        "benchmark-task",
        state,
        sync_status="foreground-current",
    )


def validate_once(vault: Path) -> None:
    runtime_access.authorize(vault, "query")
    receipt = coordinator.validate_session_receipt(
        kb_core.load_json(vault / ".kb/state/company_sync_session.json", {})
    )
    state = coordinator.validate_local_state(vault)
    if (
        receipt["tree_hash"] != state["tree_hash"]
        or receipt["mapping_hash"] != state["mapping_hash"]
        or receipt["access_snapshot"] != state["access_snapshot"]
    ):
        raise RuntimeError("Benchmark state is inconsistent")


def benchmark(count: int, repeats: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as folder:
        vault = Path(folder)
        prepare_consistent_state(vault, count)
        original = coordinator.sha256_file

        def forbidden_hash(path: Path) -> str:
            raise AssertionError(f"same-task validation hashed {path}")

        coordinator.sha256_file = forbidden_hash
        try:
            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                validate_once(vault)
                samples.append((time.perf_counter() - started) * 1000)
        finally:
            coordinator.sha256_file = original
    return {
        "files": count,
        "repeats": repeats,
        "sha256_calls": 0,
        "remote_reads": 0,
        "min_ms": round(min(samples), 3),
        "median_ms": round(sorted(samples)[len(samples) // 2], 3),
        "max_ms": round(max(samples), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[100, 1000, 5000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    results = [benchmark(count, args.repeats) for count in args.counts]
    print(json.dumps({"ok": True, "network_timing_included": False, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

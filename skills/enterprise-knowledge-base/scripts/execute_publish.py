#!/usr/bin/env python3
"""Execute a confirmed immutable publish batch through lark-cli."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import publish_batch as batch  # noqa: E402
import source_link  # noqa: E402


Runner = Callable[[list[str]], dict[str, Any]]
TRANSIENT_WORDS = ("rate limit", "rate_limit", "timeout", "network", "transport")


class ExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        returncode: int = 1,
    ) -> None:
        super().__init__(message)
        self.payload = payload or {}
        self.returncode = returncode


class PerformanceMetrics:
    """Collect non-secret command timing without retaining arguments or payloads."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.remote_calls: list[dict[str, Any]] = []

    def wrap(self, delegate: Runner) -> Runner:
        def measured(arguments: list[str]) -> dict[str, Any]:
            started = time.perf_counter()
            outcome = "ok"
            try:
                return delegate(arguments)
            except Exception:
                outcome = "error"
                raise
            finally:
                command = " ".join(arguments[:2])
                self.remote_calls.append(
                    {
                        "command": command,
                        "outcome": outcome,
                        "duration_ms": round(
                            (time.perf_counter() - started) * 1000,
                            1,
                        ),
                    }
                )

        return measured

    def summary(self) -> dict[str, Any]:
        grouped: dict[str, dict[str, Any]] = {}
        for call in self.remote_calls:
            entry = grouped.setdefault(
                call["command"],
                {"count": 0, "errors": 0, "duration_ms": 0.0},
            )
            entry["count"] += 1
            entry["errors"] += int(call["outcome"] == "error")
            entry["duration_ms"] = round(
                entry["duration_ms"] + call["duration_ms"],
                1,
            )
        return {
            "total_duration_ms": round(
                (time.perf_counter() - self.started) * 1000,
                1,
            ),
            "remote_call_count": len(self.remote_calls),
            "remote_duration_ms": round(
                sum(call["duration_ms"] for call in self.remote_calls),
                1,
            ),
            "remote_calls_by_command": grouped,
        }


class ItemRuntime:
    def __init__(
        self,
        *,
        node_token: str,
        obj_token: str,
        source_url: str,
        wiki_url: str,
    ) -> None:
        self.node_token = node_token
        self.obj_token = obj_token
        self.source_url = source_url
        self.wiki_url = wiki_url
        self.remote_before = ""
        self.remote_title = ""


def parse_json_output(stdout: str, stderr: str) -> dict[str, Any]:
    for raw in (stdout.strip(), stderr.strip()):
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            embedded: list[dict[str, Any]] = []
            index = 0
            while index < len(raw):
                object_start = raw.find("{", index)
                if object_start < 0:
                    break
                try:
                    candidate, consumed = decoder.raw_decode(raw[object_start:])
                except json.JSONDecodeError:
                    index = object_start + 1
                    continue
                if isinstance(candidate, dict):
                    embedded.append(candidate)
                index = object_start + max(consumed, 1)
            if embedded:
                return embedded[-1]
            continue
        if isinstance(value, dict):
            return value
    return {}


def lark_runner(vault: Path) -> Runner:
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

    def run(arguments: list[str]) -> dict[str, Any]:
        result = subprocess.run(
            ["lark-cli", *arguments],
            cwd=vault,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        payload = parse_json_output(result.stdout, result.stderr)
        auth_status_verified = (
            arguments[:2] == ["auth", "status"]
            and payload.get("verified") is True
        )
        if result.returncode != 0 or (
            payload.get("ok") is not True and not auth_status_verified
        ):
            error = payload.get("error", {})
            message = (
                error.get("message")
                if isinstance(error, dict)
                else str(error)
            ) or result.stderr.strip() or "lark-cli command failed"
            raise ExecutionError(
                message,
                payload=payload,
                returncode=result.returncode,
            )
        return payload

    return run


def recursive_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for nested in value.values():
            found.extend(recursive_values(nested, key))
    elif isinstance(value, list):
        for nested in value:
            found.extend(recursive_values(nested, key))
    return found


def first_string(value: Any, *keys: str) -> str:
    for key in keys:
        for candidate in recursive_values(value, key):
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def first_bool(value: Any, key: str) -> bool | None:
    for candidate in recursive_values(value, key):
        if isinstance(candidate, bool):
            return candidate
    return None


def extract_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for candidate in recursive_values(payload, "nodes"):
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def extract_warnings(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for candidate in recursive_values(payload, "warnings"):
        if isinstance(candidate, list):
            warnings.extend(str(item) for item in candidate if str(item).strip())
        elif isinstance(candidate, str) and candidate.strip():
            warnings.append(candidate)
    return warnings


def markdown_title(markdown: str) -> str:
    match = re.search(r"(?m)^#[ \t]+(.+?)\s*$", markdown)
    return match.group(1).strip() if match else ""


def error_code(exc: ExecutionError) -> int | None:
    values = recursive_values(exc.payload, "code")
    for value in values:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def normalized_hash(text: str) -> str:
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def feishu_wiki_url(node_token: str, *known_urls: str) -> str:
    for value in known_urls:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and (
            host.endswith(".feishu.cn") or host.endswith(".larksuite.com")
        ):
            return f"{parsed.scheme}://{parsed.netloc}/wiki/{node_token}"
    raise ExecutionError("Cannot derive the tenant Wiki URL for the created node.")


def validate_active_item(vault: Path, item: dict[str, Any]) -> None:
    if item.get("scope") != "enterprise":
        raise ExecutionError("Only enterprise-scoped knowledge may be executed.")
    note = batch.resolve_in_vault(vault, item["note_relative"])
    if not note.is_file() or batch.sha256_file(note) != item["note_hash"]:
        raise ExecutionError(f"Note changed after preview: {item['note_relative']}")
    if item.get("source_relative"):
        source = batch.resolve_in_vault(vault, item["source_relative"])
        if not source.is_file() or batch.sha256_file(source) != item["source_hash"]:
            raise ExecutionError(
                f"Source changed after preview: {item['source_relative']}"
            )
    prepared = batch.run_helper(
        SCRIPT_DIR / "prepare_publish.py",
        [
            "--allow-pending",
            "--vault",
            str(vault),
            "--note",
            item["note_relative"],
        ],
    )
    if prepared["payload_hash"] != item["payload_hash"]:
        raise ExecutionError(f"Payload changed after preview: {item['note_relative']}")
    if prepared.get("scope") != "enterprise":
        raise ExecutionError(f"Scope changed after preview: {item['note_relative']}")
    if prepared["parent_node_token"] != item["parent_node_token"]:
        raise ExecutionError(f"Target changed after preview: {item['note_relative']}")


def verify_source_outside_wiki(
    run: Runner,
    *,
    source_url: str,
    space_id: str,
) -> tuple[str, str]:
    inspected = run(
        [
            "drive",
            "+inspect",
            "--as",
            "user",
            "--url",
            source_url,
            "--format",
            "json",
        ]
    )
    file_type = first_string(inspected, "type", "obj_type")
    file_token = first_string(inspected, "token", "file_token", "obj_token")
    title = first_string(inspected, "title", "name", "file_name")
    if file_type != "file" or not file_token:
        raise ExecutionError("Uploaded source did not resolve to a readable Drive file.")
    try:
        run(
            [
                "wiki",
                "+node-get",
                "--as",
                "user",
                "--node-token",
                file_token,
                "--obj-type",
                "file",
                "--space-id",
                space_id,
                "--format",
                "json",
            ]
        )
    except ExecutionError as exc:
        if error_code(exc) == 131005:
            return file_token, title
        raise
    raise ExecutionError("The preserved source unexpectedly resolves as a Wiki node.")


def fetch_markdown(run: Runner, document: str) -> tuple[str, dict[str, Any]]:
    payload = run(
        [
            "docs",
            "+fetch",
            "--as",
            "user",
            "--doc",
            document,
            "--doc-format",
            "markdown",
            "--detail",
            "simple",
            "--format",
            "json",
        ]
    )
    try:
        return source_link.extract_remote_markdown(payload), payload
    except source_link.SourceLinkError as exc:
        raise ExecutionError(str(exc)) from exc


def source_payload(
    vault: Path,
    item: dict[str, Any],
    source_url: str,
) -> tuple[Path, str]:
    base_payload = batch.resolve_in_vault(vault, item["payload_relative"])
    if not item.get("source_relative"):
        return base_payload, batch.sha256_file(base_payload)
    source = batch.resolve_in_vault(vault, item["source_relative"])
    display_name = source_display_name(vault, item, source)
    output = base_payload.with_name(
        f"{base_payload.stem}.{item['item_id']}.with-source-link.md"
    )
    content = source_link.inject_source_link(
        base_payload.read_text(encoding="utf-8"),
        source_name=display_name,
        source_url=source_url,
        source_hash=batch.sha256_file(source),
    )
    output.write_text(content, encoding="utf-8", newline="\n")
    return output, batch.sha256_file(output)


def source_display_name(vault: Path, item: dict[str, Any], source: Path) -> str:
    locked = Path(str(item.get("source_display_name", "")).strip()).name
    if locked:
        return locked
    state = batch.load_json(vault / ".kb/state/processed_files.json", {"files": []})
    digest = str(item.get("source_hash", "")).lower()
    records = state.get("files", []) if isinstance(state, dict) else []
    record = next(
        (
            value
            for value in records
            if isinstance(value, dict) and str(value.get("sha256", "")).lower() == digest
        ),
        None,
    )
    return Path(str((record or {}).get("source_name", "")).strip()).name or source.name


def persist_outcome(
    vault: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    item_id: str,
    outcome: str,
    **kwargs: Any,
) -> dict[str, Any]:
    updated, _ = batch.record_outcome(
        manifest,
        item_id,
        outcome,
        **kwargs,
    )
    batch.atomic_write_json(manifest_path, updated)
    return updated


def classify_failure(exc: Exception) -> str:
    if isinstance(exc, ExecutionError):
        message = str(exc).lower()
        if any(word in message for word in TRANSIENT_WORDS):
            return "transient-failure"
    return "failed"


def load_confirmed_context(
    vault: Path,
    manifest_path: Path,
    run: Runner,
) -> tuple[Path, dict[str, Any], str, list[str]]:
    resolved_manifest = batch.resolve_in_vault(vault, manifest_path)
    manifest = batch.load_json(resolved_manifest)
    if manifest.get("schema") != batch.SCHEMA:
        raise ExecutionError("Unsupported batch manifest schema.")
    if manifest.get("state") not in {"confirmed", "executing", "completed"}:
        raise ExecutionError("Batch must be confirmed before execution.")
    pointer = batch.load_json(vault / ".kb/state/publish_batch_current.json")
    if pointer.get("batch_id") != manifest.get("batch_id"):
        raise ExecutionError("This is not the most recently previewed batch.")

    if any(item.get("scope") != "enterprise" for item in manifest.get("items", [])):
        raise ExecutionError("Batch contains non-enterprise knowledge; no remote action is allowed.")

    auth = run(["auth", "status", "--json", "--verify"])
    if first_bool(auth, "verified") is not True:
        raise ExecutionError("The lark-cli user identity is not verified.")
    nodes_mapping = batch.load_json(vault / ".kb/mappings/feishu_nodes.json")
    space_id = str(nodes_mapping.get("space_id", ""))
    if not space_id:
        raise ExecutionError("Verified Feishu space mapping is missing.")
    nodes_path = vault / ".kb/mappings/feishu_nodes.json"
    if batch.sha256_file(nodes_path) != manifest.get("nodes_mapping_hash"):
        raise ExecutionError("Feishu destination mapping changed after preview.")

    current_documents = batch.load_json(
        vault / ".kb/mappings/feishu_documents.json",
        {"version": 2, "documents": []},
    )
    for item in manifest.get("items", []):
        if item.get("state") in batch.TERMINAL_ITEM_STATES:
            continue
        note = batch.resolve_in_vault(vault, item["note_relative"])
        current_mapping = batch.resolve_document_mapping(
            vault,
            note,
            current_documents,
        )
        expected_mapping = item.get("mapping")
        if expected_mapping is None and current_mapping is not None:
            raise ExecutionError(
                f"Feishu document mapping changed after preview: {item['note_relative']}"
            )
        if expected_mapping is not None and batch.safe_mapping_snapshot(
            current_mapping
        ) != expected_mapping:
            raise ExecutionError(
                f"Feishu document mapping changed after preview: {item['note_relative']}"
            )

    known_urls = [
        str(document.get("feishu_url", ""))
        for document in current_documents.get("documents", [])
        if isinstance(document, dict) and document.get("feishu_url")
    ]
    return resolved_manifest, manifest, space_id, known_urls


def load_children_by_parent(
    run: Runner,
    manifest: dict[str, Any],
    space_id: str,
) -> dict[str, list[dict[str, Any]]]:
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in manifest.get("items", []):
        if item.get("state") in batch.TERMINAL_ITEM_STATES:
            continue
        parent = item["parent_node_token"]
        if parent in children_by_parent:
            continue
        payload = run(
            [
                "wiki",
                "+node-list",
                "--as",
                "user",
                "--space-id",
                space_id,
                "--parent-node-token",
                parent,
                "--page-all",
                "--format",
                "json",
            ]
        )
        if first_bool(payload, "has_more") is True:
            raise ExecutionError("Wiki child listing did not exhaust pagination.")
        children_by_parent[parent] = extract_nodes(payload)
    return children_by_parent


def runtime_from_item(item: dict[str, Any]) -> ItemRuntime:
    journal = item.get("journal", {})
    mapping = item.get("mapping") or {}
    return ItemRuntime(
        node_token=str(
            journal.get("node_token") or mapping.get("node_token") or ""
        ),
        obj_token=str(
            journal.get("obj_token") or mapping.get("obj_token") or ""
        ),
        source_url=str(
            journal.get("source_file_url")
            or mapping.get("source_file_url")
            or item.get("source_preuploaded_url")
            or ""
        ),
        wiki_url=str(
            journal.get("feishu_url") or mapping.get("feishu_url") or ""
        ),
    )


def ensure_document(
    vault: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    item: dict[str, Any],
    children: list[dict[str, Any]],
    space_id: str,
    run: Runner,
    runtime: ItemRuntime,
) -> tuple[dict[str, Any], bool]:
    journal = item.get("journal", {})
    mapping = item.get("mapping") or {}
    decision = batch.decide_remote_action(item, children)
    if journal.get("node_token"):
        decision = {"action": "update", "reason": "resume_created_node"}
    if decision["action"] in {"collision", "conflict"}:
        return (
            persist_outcome(
                vault,
                manifest_path,
                manifest,
                item["item_id"],
                decision["action"],
                reason=decision["reason"],
            ),
            False,
        )

    if decision["action"] == "update":
        if not runtime.node_token or not runtime.obj_token:
            raise ExecutionError("Mapped update is missing document identifiers.")
        remote_before, remote_payload = fetch_markdown(run, runtime.obj_token)
        runtime.remote_before = remote_before
        runtime.remote_title = first_string(remote_payload, "title") or markdown_title(
            remote_before
        )
        baseline = str(
            mapping.get("last_synced_remote_hash")
            or mapping.get("last_synced_payload_hash")
            or ""
        )
        if baseline and baseline != normalized_hash(remote_before):
            return (
                persist_outcome(
                    vault,
                    manifest_path,
                    manifest,
                    item["item_id"],
                    "conflict",
                    reason="remote_manual_edit",
                ),
                False,
            )
        return manifest, True

    created = run(
        [
            "wiki",
            "+node-create",
            "--as",
            "user",
            "--space-id",
            space_id,
            "--parent-node-token",
            item["parent_node_token"],
            "--obj-type",
            "docx",
            "--title",
            item["title"],
            "--format",
            "json",
        ]
    )
    runtime.node_token = first_string(created, "node_token")
    runtime.obj_token = first_string(created, "obj_token")
    runtime.wiki_url = first_string(created, "wiki_url", "url")
    if not runtime.node_token or not runtime.obj_token:
        raise ExecutionError("Created Wiki node returned incomplete identifiers.")
    children.append(
        {
            "title": item["title"],
            "node_token": runtime.node_token,
            "obj_token": runtime.obj_token,
        }
    )
    return manifest, True


def ensure_source(
    vault: Path,
    item: dict[str, Any],
    space_id: str,
    run: Runner,
    runtime: ItemRuntime,
) -> str:
    if not item.get("source_relative"):
        return ""
    source = batch.resolve_in_vault(vault, item["source_relative"])
    display_name = source_display_name(vault, item, source)
    if not runtime.source_url:
        uploaded = run(
            [
                "drive",
                "+upload",
                "--as",
                "user",
                "--file",
                item["source_relative"],
                "--name",
                display_name,
                "--format",
                "json",
            ]
        )
        runtime.source_url = first_string(uploaded, "url", "file_url")
        if not runtime.source_url:
            raise ExecutionError("Drive upload returned no readable file URL.")
    file_token, inspected_name = verify_source_outside_wiki(
        run,
        source_url=runtime.source_url,
        space_id=space_id,
    )
    if inspected_name and inspected_name != display_name:
        raise ExecutionError("Drive source filename does not match the original.")
    return file_token


def write_and_verify(
    vault: Path,
    item: dict[str, Any],
    run: Runner,
    runtime: ItemRuntime,
) -> tuple[str, str]:
    final_payload, final_hash = source_payload(
        vault,
        item,
        runtime.source_url,
    )
    expected = final_payload.read_text(encoding="utf-8")
    if runtime.remote_before:
        comparison = source_link.compare_ignoring_source(
            expected,
            runtime.remote_before,
        )
        source_verified = True
        if item.get("source_relative"):
            source = batch.resolve_in_vault(vault, item["source_relative"])
            display_name = source_display_name(vault, item, source)
            source_verified = source_link.verify_source_link(
                runtime.remote_before,
                source_name=display_name,
                source_url=runtime.source_url,
            )["verified"]
        title_matches = not runtime.remote_title or runtime.remote_title == item["title"]
        if comparison["match"] and title_matches and source_verified:
            return final_hash, normalized_hash(runtime.remote_before)
    updated_remote = run(
        [
            "docs",
            "+update",
            "--as",
            "user",
            "--doc",
            runtime.obj_token,
            "--command",
            "overwrite",
            "--doc-format",
            "markdown",
            "--content",
            f"@{batch.relative_posix(final_payload, vault)}",
            "--format",
            "json",
        ]
    )
    result = first_string(updated_remote, "result")
    if result not in {"success", "partial_success"}:
        raise ExecutionError(f"Document overwrite returned {result or 'no result'}.")

    remote_markdown, fetch_payload = fetch_markdown(run, runtime.obj_token)
    comparison = source_link.compare_ignoring_source(expected, remote_markdown)
    if not comparison["match"]:
        raise ExecutionError("Read-back content does not match the immutable payload.")
    remote_title = first_string(fetch_payload, "title") or markdown_title(
        remote_markdown
    )
    if remote_title and remote_title != item["title"]:
        raise ExecutionError("Read-back document title does not match the batch.")
    warnings = extract_warnings(updated_remote)
    if warnings and result != "partial_success":
        raise ExecutionError(
            "Document overwrite returned warnings despite a success result."
        )
    if item.get("source_relative"):
        source = batch.resolve_in_vault(vault, item["source_relative"])
        display_name = source_display_name(vault, item, source)
        verified = source_link.verify_source_link(
            remote_markdown,
            source_name=display_name,
            source_url=runtime.source_url,
        )
        if not verified["verified"]:
            raise ExecutionError("Read-back source section verification failed.")
    return final_hash, normalized_hash(remote_markdown)


def execute_item(
    vault: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    item_id: str,
    *,
    run: Runner,
    children_by_parent: dict[str, list[dict[str, Any]]],
    space_id: str,
    known_urls: list[str],
    synced_at: str | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    item = next(
        candidate for candidate in manifest["items"] if candidate["item_id"] == item_id
    )
    if item.get("state") in batch.TERMINAL_ITEM_STATES:
        if item.get("state") == "success":
            url = str(item.get("journal", {}).get("feishu_url", ""))
            if url:
                return manifest, {"title": item["title"], "url": url}
        return manifest, None

    runtime = runtime_from_item(item)
    try:
        validate_active_item(vault, item)
        manifest, proceed = ensure_document(
            vault,
            manifest_path,
            manifest,
            item,
            children_by_parent[item["parent_node_token"]],
            space_id,
            run,
            runtime,
        )
        if not proceed:
            return manifest, None
        source_file_token = ensure_source(vault, item, space_id, run, runtime)
        final_hash, remote_hash = write_and_verify(vault, item, run, runtime)
        runtime.wiki_url = runtime.wiki_url or feishu_wiki_url(
            runtime.node_token,
            runtime.source_url,
            *known_urls,
        )
        updated, _ = batch.record_outcome(
            manifest,
            item_id,
            "success",
            verified=True,
            node_token=runtime.node_token,
            obj_token=runtime.obj_token,
            feishu_url=runtime.wiki_url,
            source_file_url=runtime.source_url,
            source_outside_wiki_verified=bool(
                not item.get("source_relative") or source_file_token
            ),
            final_payload_hash=final_hash,
            last_synced_remote_hash=remote_hash,
        )
        batch.finalize_local_success(
            vault,
            manifest_path,
            updated,
            item_id,
            synced_at=synced_at,
        )
        return (
            batch.load_json(manifest_path),
            {"title": item["title"], "url": runtime.wiki_url},
        )
    except Exception as exc:
        current = batch.load_json(manifest_path)
        current_item = next(
            candidate
            for candidate in current["items"]
            if candidate["item_id"] == item_id
        )
        if current_item.get("state") in batch.TERMINAL_ITEM_STATES:
            return current, None
        updated, _ = batch.record_outcome(
            current,
            item_id,
            classify_failure(exc),
            node_token=runtime.node_token,
            obj_token=runtime.obj_token,
            feishu_url=runtime.wiki_url,
            source_file_url=runtime.source_url,
            reason=str(exc),
        )
        batch.atomic_write_json(manifest_path, updated)
        return updated, None


def execute_confirmed_batch(
    vault: Path,
    manifest_path: Path,
    run: Runner,
    *,
    synced_at: str | None,
) -> dict[str, Any]:
    manifest_path, manifest, space_id, known_urls = load_confirmed_context(
        vault,
        manifest_path,
        run,
    )
    children_by_parent = load_children_by_parent(run, manifest, space_id)

    links: list[dict[str, str]] = []
    for snapshot in list(manifest.get("items", [])):
        item_id = snapshot["item_id"]
        manifest = batch.load_json(manifest_path)
        manifest, link = execute_item(
            vault,
            manifest_path,
            manifest,
            item_id,
            run=run,
            children_by_parent=children_by_parent,
            space_id=space_id,
            known_urls=known_urls,
            synced_at=synced_at,
        )
        if link:
            links.append(link)

    final = batch.load_json(manifest_path)
    return {
        "ok": final.get("summary", {}).get("failed", 0) == 0
        and final.get("summary", {}).get("collision", 0) == 0
        and final.get("summary", {}).get("conflict", 0) == 0
        and final.get("summary", {}).get("retryable", 0) == 0,
        "batch_id": final.get("batch_id"),
        "state": final.get("state"),
        "summary": final.get("summary", {}),
        "links": links,
    }


def persist_performance(
    vault: Path,
    batch_id: str,
    status: str,
    performance: dict[str, Any],
) -> str:
    path = vault / ".kb/logs/publish-performance" / f"{batch_id}.json"
    batch.atomic_write_json(
        path,
        {
            "schema": "kb-publish-performance/v1",
            "batch_id": batch_id,
            "recorded_at": datetime.now().astimezone().isoformat(),
            "status": status,
            "performance": performance,
        },
    )
    return batch.relative_posix(path, vault)


def execute_batch(
    vault: Path,
    manifest_path: Path,
    *,
    runner: Runner | None = None,
    synced_at: str | None = None,
) -> dict[str, Any]:
    vault = vault.resolve()
    metrics = PerformanceMetrics()
    run = metrics.wrap(runner or lark_runner(vault))
    batch_id = "unknown"
    try:
        resolved = batch.resolve_in_vault(vault, manifest_path)
        initial = batch.load_json(resolved)
        batch_id = str(initial.get("batch_id") or "unknown")
        result = execute_confirmed_batch(
            vault,
            resolved,
            run,
            synced_at=synced_at,
        )
    except Exception:
        performance = metrics.summary()
        try:
            persist_performance(vault, batch_id, "failed", performance)
        except OSError:
            pass
        raise

    performance = metrics.summary()
    result["performance"] = performance
    try:
        result["performance_log"] = persist_performance(
            vault,
            batch_id,
            "completed" if result["ok"] else "completed_with_errors",
            performance,
        )
    except OSError as exc:
        result["performance_log"] = ""
        result["performance_log_error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("preview", "confirm", "execute"),
        default="execute",
    )
    parser.add_argument("--vault", type=Path, default=Path("."))
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--phrase", default="")
    args = parser.parse_args()
    try:
        from company_sync_coordinator import (
            record_publisher_convergence,
            validate_local_state,
        )
        from runtime_access import authorize
        from vault_context import discover_vault

        vault = discover_vault(args.vault)
        authorize(vault, "publish")
        if (vault / ".kb/state/company_sync.json").is_file():
            validate_local_state(vault, verify_file_hashes=True)
        if args.command == "preview":
            result = batch.create_preview(vault)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.batch is None:
            raise ExecutionError("--batch is required for confirmation or execution.")
        if args.command == "confirm":
            batch.confirm_batch(vault, args.batch, args.phrase)
        result = execute_batch(vault, args.batch)
        if result["ok"]:
            result["publisher_convergence"] = record_publisher_convergence(vault)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    except (batch.BatchError, ExecutionError, OSError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

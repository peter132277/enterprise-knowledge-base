#!/usr/bin/env python3
"""Search only the current knowledge Vault and return ranked local evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from vault_context import discover_vault


MARKDOWN_EXTENSIONS = {".md", ".markdown"}
FRONTMATTER_TITLE = re.compile(r"^title:\s*[\"']?(.*?)[\"']?\s*$", re.MULTILINE)
H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
TOKEN = re.compile(r"[A-Za-z0-9_+.-]+|[\u3400-\u9fff]+")
PERSONAL_MARKERS = ("我的知识", "个人知识", "个人库")
ENTERPRISE_MARKERS = ("企业知识", "公司知识", "企业库", "公司库")
MAX_RECALL_TERMS = 24
MAX_RECALL_RESULTS = 2000
OBSIDIAN_CLI_TIMEOUT_SECONDS = 12


class QueryError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: dict[str, Any]) -> str:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def query_sha256(query: str) -> str:
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in TOKEN.findall(normalize(query)):
        if raw not in terms:
            terms.append(raw)
        if re.fullmatch(r"[\u3400-\u9fff]+", raw) and len(raw) > 2:
            for index in range(len(raw) - 1):
                bigram = raw[index : index + 2]
                if bigram not in terms:
                    terms.append(bigram)
    return terms[:32]


def recall_terms(query: str) -> list[str]:
    """Build a compact OR query for deterministic candidate recall."""
    raw_terms = TOKEN.findall(normalize(query))
    terms: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if len(value) >= 2 and value not in terms:
            terms.append(value)

    for raw in raw_terms:
        if re.fullmatch(r"[A-Za-z0-9_+.-]+", raw):
            add(raw)
        elif len(raw) <= 8:
            add(raw)

    for width in (4, 3, 2):
        for raw in raw_terms:
            if not re.fullmatch(r"[\u3400-\u9fff]+", raw) or len(raw) < width:
                continue
            for index in range(len(raw) - width + 1):
                add(raw[index : index + width])
                if len(terms) >= MAX_RECALL_TERMS:
                    return terms
    return terms[:MAX_RECALL_TERMS]


def infer_scope(query: str, requested: str) -> str:
    if requested != "auto":
        return requested
    normalized = normalize(query)
    personal = any(marker in normalized for marker in PERSONAL_MARKERS)
    enterprise = any(marker in normalized for marker in ENTERPRISE_MARKERS)
    if personal and not enterprise:
        return "personal"
    if enterprise and not personal:
        return "enterprise"
    return "all"


def search_roots(vault: Path, scope: str, include_sources: bool) -> list[Path]:
    relative_roots = {
        "personal": (
            "20_知识/个人",
            "20_知识/原子",
            "30_导航/主题/个人主题",
            "30_导航/知识库首页.md",
        ),
        "enterprise": (
            "20_知识/企业",
            "20_知识/原子",
            "30_导航/主题/企业主题",
            "30_导航/知识库首页.md",
        ),
        "all": ("20_知识", "30_导航"),
    }[scope]
    roots = [vault / Path(relative) for relative in relative_roots]
    if include_sources:
        roots.append(vault / "10_来源" / "提取")
    return roots


def iter_markdown(vault: Path, roots: list[Path]) -> list[Path]:
    files: dict[str, Path] = {}
    vault_resolved = vault.resolve()
    for root in roots:
        candidates = [root] if root.is_file() else root.rglob("*") if root.is_dir() else []
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in MARKDOWN_EXTENSIONS:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(vault_resolved):
                continue
            relative = resolved.relative_to(vault_resolved).as_posix()
            files[relative] = resolved
    return [files[key] for key in sorted(files)]


def is_path_in_roots(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        root_resolved = root.resolve()
        if root.is_file() and resolved == root_resolved:
            return True
        if root.is_dir() and resolved.is_relative_to(root_resolved):
            return True
    return False


def normalize_candidate(
    vault: Path,
    roots: list[Path],
    value: str,
) -> Path | None:
    candidate = (vault / Path(value.replace("\\", "/"))).resolve()
    if not candidate.is_relative_to(vault.resolve()):
        return None
    if (
        not candidate.is_file()
        or candidate.suffix.lower() not in MARKDOWN_EXTENSIONS
        or not is_path_in_roots(candidate, roots)
    ):
        return None
    return candidate


def registered_obsidian_cli_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    candidates: list[Path] = []
    uninstall_key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    views = (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                root = winreg.OpenKey(
                    hive,
                    uninstall_key,
                    0,
                    winreg.KEY_READ | view,
                )
            except OSError:
                continue
            with root:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        subkey = winreg.OpenKey(root, subkey_name)
                    except OSError:
                        continue
                    with subkey:
                        try:
                            display_name = str(
                                winreg.QueryValueEx(subkey, "DisplayName")[0]
                            )
                        except OSError:
                            continue
                        if display_name.strip().casefold() != "obsidian":
                            continue
                        for field in ("InstallLocation", "DisplayIcon"):
                            try:
                                value = str(winreg.QueryValueEx(subkey, field)[0])
                            except OSError:
                                continue
                            value = value.strip().strip('"')
                            if field == "DisplayIcon":
                                value = re.sub(r",\d+$", "", value)
                            path = Path(value)
                            directory = path if path.is_dir() else path.parent
                            candidates.append(directory / "Obsidian.com")
    return candidates


def find_obsidian_cli() -> Path | None:
    candidates: list[Path] = []
    configured = os.environ.get("OBSIDIAN_CLI_PATH", "").strip()
    if configured:
        candidates.append(Path(configured))
    for name in ("obsidian", "Obsidian.com", "obsidian.com"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.extend(
            (
                Path(local_app_data) / "Programs" / "Obsidian" / "Obsidian.com",
                Path(local_app_data) / "Obsidian" / "Obsidian.com",
            )
        )
    candidates.extend(registered_obsidian_cli_candidates())
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def subprocess_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def recall_with_obsidian_cli(
    vault: Path,
    roots: list[Path],
    terms: list[str],
    cli_path: Path,
) -> tuple[list[Path], dict[str, Any]]:
    recall_query = " OR ".join(terms)
    if not recall_query:
        return [], {
            "command_count": 0,
            "elapsed_ms": 0,
            "recall_query": "",
        }

    started = time.perf_counter()
    directory_roots = [root for root in roots if root.is_dir()]

    def search_root(root: Path) -> set[str]:
        relative_root = root.relative_to(vault).as_posix()
        command = [
            str(cli_path),
            "search",
            f"query={recall_query}",
            f"path={relative_root}",
            f"limit={MAX_RECALL_RESULTS}",
            "format=json",
        ]
        try:
            process = subprocess.run(
                command,
                cwd=vault,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OBSIDIAN_CLI_TIMEOUT_SECONDS,
                check=False,
                **subprocess_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise QueryError(f"Obsidian CLI failed: {exc}") from exc
        output = process.stdout.strip()
        if process.returncode != 0:
            detail = (process.stderr or output).strip()[-400:]
            raise QueryError(f"Obsidian CLI returned {process.returncode}: {detail}")
        if output == "No matches found." or not output:
            return set()
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise QueryError(
                f"Obsidian CLI did not return JSON: {output[-400:]}"
            ) from exc
        if not isinstance(payload, list):
            raise QueryError("Obsidian CLI search result must be a JSON list")
        root_values: set[str] = set()
        for item in payload:
            if isinstance(item, str):
                root_values.add(item)
            elif isinstance(item, dict) and isinstance(item.get("file"), str):
                root_values.add(str(item["file"]))
        return root_values

    values: set[str] = set()
    if len(directory_roots) == 1:
        values.update(search_root(directory_roots[0]))
    elif directory_roots:
        with ThreadPoolExecutor(max_workers=min(4, len(directory_roots))) as executor:
            for root_values in executor.map(search_root, directory_roots):
                values.update(root_values)

    candidates: dict[str, Path] = {}
    for value in values:
        candidate = normalize_candidate(vault, roots, value)
        if candidate is not None:
            candidates[candidate.relative_to(vault).as_posix()] = candidate
    for root in roots:
        if root.is_file() and root.suffix.lower() in MARKDOWN_EXTENSIONS:
            candidates[root.relative_to(vault).as_posix()] = root.resolve()
    return [candidates[key] for key in sorted(candidates)], {
        "command_count": len(directory_roots),
        "parallel_workers": min(4, len(directory_roots)),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "recall_query": recall_query,
        "cli_path": str(cli_path),
    }


def recall_with_rg(
    vault: Path,
    roots: list[Path],
    terms: list[str],
) -> tuple[list[Path], dict[str, Any]]:
    rg_path = shutil.which("rg")
    if not rg_path:
        raise QueryError("ripgrep is not available")
    if not terms:
        return [], {"elapsed_ms": 0, "recall_terms": []}
    command = [
        rg_path,
        "--files-with-matches",
        "--ignore-case",
        "--fixed-strings",
        "--glob",
        "*.md",
        "--glob",
        "*.markdown",
    ]
    for term in terms:
        command.extend(("-e", term))
    command.extend(str(root.relative_to(vault)) for root in roots if root.exists())
    started = time.perf_counter()
    try:
        process = subprocess.run(
            command,
            cwd=vault,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=OBSIDIAN_CLI_TIMEOUT_SECONDS,
            check=False,
            **subprocess_options(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QueryError(f"ripgrep failed: {exc}") from exc
    if process.returncode not in (0, 1):
        raise QueryError(
            f"ripgrep returned {process.returncode}: {process.stderr.strip()[-400:]}"
        )
    candidates: dict[str, Path] = {}
    for value in process.stdout.splitlines():
        candidate = normalize_candidate(vault, roots, value)
        if candidate is not None:
            candidates[candidate.relative_to(vault).as_posix()] = candidate
    for root in roots:
        if root.is_file() and root.suffix.lower() in MARKDOWN_EXTENSIONS:
            candidates[root.relative_to(vault).as_posix()] = root.resolve()
    return [candidates[key] for key in sorted(candidates)], {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "recall_terms": terms,
        "rg_path": str(Path(rg_path).resolve()),
    }


def retrieve_candidates(
    vault: Path,
    roots: list[Path],
    terms: list[str],
    backend: str,
    allow_cli: bool,
) -> tuple[list[Path], str, dict[str, Any]]:
    fallback_reason = ""
    if backend in ("auto", "obsidian-cli") and allow_cli:
        cli_path = find_obsidian_cli()
        if cli_path is None:
            fallback_reason = "Obsidian CLI was not found"
            if backend == "obsidian-cli":
                raise QueryError(fallback_reason)
        else:
            try:
                candidates, details = recall_with_obsidian_cli(
                    vault, roots, terms, cli_path
                )
                return candidates, "obsidian-cli", details
            except QueryError as exc:
                fallback_reason = str(exc)
                if backend == "obsidian-cli":
                    raise

    if backend in ("auto", "rg") or not allow_cli:
        try:
            candidates, details = recall_with_rg(vault, roots, terms)
            if fallback_reason:
                details["fallback_from"] = "obsidian-cli"
                details["fallback_reason"] = fallback_reason
            return candidates, "rg", details
        except QueryError as exc:
            if backend == "rg":
                raise
            fallback_reason = "; ".join(
                value for value in (fallback_reason, str(exc)) if value
            )

    candidates = iter_markdown(vault, roots)
    return candidates, "python-filesystem", {
        "fallback_reason": fallback_reason or "No external recall backend was available"
    }


def extract_title(text: str, fallback: str) -> str:
    match = FRONTMATTER_TITLE.search(text)
    if match:
        return match.group(1).strip()
    match = H1.search(text)
    return match.group(1).strip() if match else fallback


def without_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :])
    return text


def best_snippet(text: str, terms: list[str], limit: int = 240) -> str:
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in without_frontmatter(text).splitlines()
        if line.strip()
    ]
    if not lines:
        return ""
    ranked = sorted(
        enumerate(lines),
        key=lambda item: (
            -sum(normalize(item[1]).count(term) for term in terms),
            item[0],
        ),
    )
    snippet = ranked[0][1]
    return snippet if len(snippet) <= limit else snippet[: limit - 1] + "…"


def score_document(
    path: Path,
    vault: Path,
    query: str,
    terms: list[str],
) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    relative = path.relative_to(vault).as_posix()
    title = extract_title(text, path.stem)
    normalized_title = normalize(title)
    normalized_path = normalize(relative)
    normalized_text = normalize(text)
    normalized_query = normalize(query)

    score = 0
    if normalized_query and normalized_query in normalized_title:
        score += 40
    elif normalized_query and normalized_query in normalized_text:
        score += 18
    matched: list[str] = []
    for term in terms:
        title_count = normalized_title.count(term)
        path_count = normalized_path.count(term)
        body_count = min(normalized_text.count(term), 12)
        if title_count or path_count or body_count:
            matched.append(term)
        score += title_count * 10 + path_count * 5 + body_count
    if score <= 0:
        return None
    return {
        "path": relative,
        "title": title,
        "score": score,
        "matched_terms": matched[:12],
    }


def finalize_result(
    item: dict[str, Any],
    vault: Path,
    terms: list[str],
) -> dict[str, Any] | None:
    path = vault / Path(str(item["path"]))
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    return {
        **item,
        "file_sha256": sha256_file(path),
        "snippet": best_snippet(text, terms),
    }


def persist_receipt(
    vault: Path,
    result: dict[str, Any],
    requested_scope: str,
    max_results: int,
) -> tuple[str, str, str]:
    now = datetime.now().astimezone()
    question_hash = query_sha256(str(result["query"]))
    query_id = (
        now.strftime("%Y%m%dT%H%M%S%f%z")
        + "-"
        + question_hash[:10]
        + "-"
        + uuid.uuid4().hex[:8]
    )
    relative = (
        Path(".kb")
        / "logs"
        / "查询"
        / now.date().isoformat()
        / f"{query_id}.json"
    )
    receipt = {
        "schema_version": 1,
        "query_id": query_id,
        "created_at": now.isoformat(timespec="milliseconds"),
        "mode": result["mode"],
        "vault": result["vault"],
        "query": result["query"],
        "query_sha256": question_hash,
        "requested_scope": requested_scope,
        "scope": result["scope"],
        "include_sources": result["include_sources"],
        "searched_roots": result["searched_roots"],
        "max_results": max_results,
        "retrieval_backend": result["retrieval_backend"],
        "candidate_count": result["candidate_count"],
        "backend_details": result["backend_details"],
        "result_count": result["result_count"],
        "total_matches": result["total_matches"],
        "external_sources_used": result["external_sources_used"],
        "performance": result.get("performance", {}),
        "results": [
            {
                "path": item["path"],
                "title": item["title"],
                "score": item["score"],
                "file_sha256": item["file_sha256"],
            }
            for item in result["results"]
        ],
    }
    receipt["receipt_body_sha256"] = sha256_json(receipt)
    path = vault / relative
    write_json_atomic(path, receipt)
    return query_id, relative.as_posix(), sha256_file(path)


def run_query(
    vault: Path,
    query: str,
    requested_scope: str,
    include_sources: bool,
    max_results: int,
    backend: str = "auto",
    enforce_project: bool = True,
    write_receipt: bool = True,
) -> dict[str, Any]:
    query_started = time.perf_counter()
    resolved_vault = vault.resolve()
    if enforce_project:
        try:
            resolved_vault = discover_vault(resolved_vault)
        except RuntimeError as exc:
            raise QueryError(str(exc)) from exc
    if not query.strip():
        raise QueryError("Query must not be empty")
    if max_results < 1 or max_results > 20:
        raise QueryError("max_results must be between 1 and 20")

    scope = infer_scope(query, requested_scope)
    roots = search_roots(resolved_vault, scope, include_sources)
    terms = query_terms(query)
    recall = recall_terms(query)
    retrieval_started = time.perf_counter()
    candidates, retrieval_backend, backend_details = retrieve_candidates(
        resolved_vault,
        roots,
        recall,
        backend,
        allow_cli=enforce_project,
    )
    retrieval_duration_ms = (time.perf_counter() - retrieval_started) * 1000
    scoring_started = time.perf_counter()
    results = [
        result
        for path in candidates
        if (result := score_document(path, resolved_vault, query, terms)) is not None
    ]
    results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    bounded = [
        final
        for item in results[:max_results]
        if (final := finalize_result(item, resolved_vault, terms)) is not None
    ]
    scoring_duration_ms = (time.perf_counter() - scoring_started) * 1000
    result: dict[str, Any] = {
        "ok": True,
        "mode": "current-vault-only",
        "vault": str(resolved_vault),
        "scope": scope,
        "include_sources": include_sources,
        "searched_roots": [
            path.relative_to(resolved_vault).as_posix()
            for path in roots
            if path.exists()
        ],
        "query": query,
        "query_terms": terms,
        "recall_terms": recall,
        "retrieval_backend": retrieval_backend,
        "candidate_count": len(candidates),
        "backend_details": backend_details,
        "result_count": len(bounded),
        "total_matches": len(results),
        "external_sources_used": False,
        "results": bounded,
        "performance": {
            "retrieval_duration_ms": round(retrieval_duration_ms, 1),
            "scoring_duration_ms": round(scoring_duration_ms, 1),
            "search_duration_ms": round(
                (time.perf_counter() - query_started) * 1000,
                1,
            ),
        },
    }
    if write_receipt:
        receipt_started = time.perf_counter()
        query_id, receipt_path, receipt_hash = persist_receipt(
            resolved_vault,
            result,
            requested_scope,
            max_results,
        )
        result["performance"]["receipt_duration_ms"] = round(
            (time.perf_counter() - receipt_started) * 1000,
            1,
        )
        result["performance"]["query_total_duration_ms"] = round(
            (time.perf_counter() - query_started) * 1000,
            1,
        )
        result["query_id"] = query_id
        result["query_sha256"] = query_sha256(query)
        result["receipt_path"] = receipt_path
        result["receipt_sha256"] = receipt_hash
        result["answer_gate"] = {
            "status": "retrieval-complete-audit-required",
            "audit_command": "python <skill-root>/scripts/check_query_compliance.py",
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--scope",
        choices=("auto", "personal", "enterprise", "all"),
        default="auto",
    )
    parser.add_argument("--include-sources", action="store_true")
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument(
        "--backend",
        choices=("auto", "obsidian-cli", "rg"),
        default="auto",
        help="Candidate recall backend; auto prefers Obsidian CLI and falls back to rg.",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        project_vault = discover_vault()
        result = run_query(
            project_vault,
            args.query,
            args.scope,
            args.include_sources,
            args.max_results,
            backend=args.backend,
            write_receipt=True,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

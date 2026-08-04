import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "query_current_vault.py"
SCRIPTS = SCRIPT.parent
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("query_current_vault", SCRIPT)
QUERY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(QUERY)

AUDIT_SCRIPT = SCRIPTS / "check_query_compliance.py"
AUDIT_SPEC = importlib.util.spec_from_file_location(
    "check_query_compliance", AUDIT_SCRIPT
)
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
assert AUDIT_SPEC.loader
AUDIT_SPEC.loader.exec_module(AUDIT)
FAST_SCRIPT = SCRIPTS / "query_answer_packet.py"
FAST_SPEC = importlib.util.spec_from_file_location(
    "query_answer_packet", FAST_SCRIPT
)
FAST = importlib.util.module_from_spec(FAST_SPEC)
assert FAST_SPEC.loader
FAST_SPEC.loader.exec_module(FAST)
import setup_wizard as SETUP


class QueryCurrentVaultTests(unittest.TestCase):
    def make_vault(self, root: Path) -> None:
        (root / "20_知识/个人").mkdir(parents=True, exist_ok=True)
        (root / "20_知识/企业").mkdir(parents=True, exist_ok=True)
        (root / "20_知识/原子").mkdir(parents=True, exist_ok=True)
        (root / "30_导航/主题/个人主题").mkdir(parents=True, exist_ok=True)
        (root / "30_导航/主题/企业主题").mkdir(parents=True, exist_ok=True)
        (root / "10_来源/提取").mkdir(parents=True, exist_ok=True)
        (root / "20_知识/个人/个人方法.md").write_text(
            "# 个人方法\n\n番茄工作法与复盘。\n", encoding="utf-8"
        )
        (root / "20_知识/企业/销售方法.md").write_text(
            "---\ntitle: 企业销售流程\n---\n\n客户跟进与成交。\n",
            encoding="utf-8",
        )
        (root / "10_来源/提取/原始资料.md").write_text(
            "# 来源\n\n只有来源层包含独特证据。\n", encoding="utf-8"
        )

    def test_rejects_a_different_vault_when_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            with self.assertRaises(QUERY.QueryError):
                QUERY.run_query(vault, "销售", "all", False, 8, enforce_project=True)

    def test_scope_and_source_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            direct = QUERY.run_query(
                vault,
                "销售流程是什么？",
                "auto",
                False,
                8,
                enforce_project=False,
            )
            self.assertEqual(direct["scope"], "all")
            enterprise = QUERY.run_query(
                vault,
                "企业知识销售",
                "auto",
                False,
                8,
                enforce_project=False,
            )
            self.assertEqual(enterprise["scope"], "all")
            enterprise_paths = {item["path"] for item in enterprise["results"]}
            self.assertIn(
                "20_知识/企业/销售方法.md",
                enterprise_paths,
            )
            without_sources = QUERY.run_query(
                vault,
                "独特证据",
                "all",
                False,
                8,
                enforce_project=False,
            )
            self.assertEqual(without_sources["result_count"], 0)
            with_sources = QUERY.run_query(
                vault,
                "独特证据",
                "all",
                True,
                8,
                enforce_project=False,
            )
            self.assertEqual(with_sources["result_count"], 1)
            self.assertEqual(
                with_sources["results"][0]["path"],
                "10_来源/提取/原始资料.md",
            )
            self.assertFalse(with_sources["external_sources_used"])

    def test_receipt_and_audit_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = QUERY.run_query(
                vault,
                "企业知识销售",
                "auto",
                False,
                8,
                enforce_project=False,
                write_receipt=True,
            )
            self.assertTrue((vault / result["receipt_path"]).is_file())
            citation = result["results"][0]["path"]
            audit = AUDIT.run(
                vault,
                "企业知识销售",
                result["receipt_path"],
                [citation],
                False,
                persist_audit=False,
                enforce_project=False,
            )
            self.assertTrue(audit["ok"])
            self.assertEqual(audit["approved_citations"], [citation])
            self.assertFalse(audit["external_sources_used"])
            self.assertEqual(audit["retrieval_backend"], result["retrieval_backend"])
            self.assertIn("query_total_duration_ms", result["performance"])
            self.assertIn("workflow_elapsed_ms", audit["performance"])

            with self.assertRaises(AUDIT.ComplianceError):
                AUDIT.run(
                    vault,
                    "不同的问题",
                    result["receipt_path"],
                    [citation],
                    False,
                    persist_audit=False,
                    enforce_project=False,
                )
            with self.assertRaises(AUDIT.ComplianceError):
                AUDIT.run(
                    vault,
                    "企业知识销售",
                    result["receipt_path"],
                    ["20_知识/企业/不存在.md"],
                    False,
                    persist_audit=False,
                    enforce_project=False,
                )

    def test_fast_path_audits_clear_ranking_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            SETUP.initialize(vault, "local", "")
            self.make_vault(vault)
            target = vault / "20_知识/个人/fast-path.md"
            target.write_text(
                "# clear-fast-path-only\n\n"
                "clear-fast-path-only clear-fast-path-only clear-fast-path-only\n",
                encoding="utf-8",
            )
            result = FAST.run_fast_query(
                vault,
                "clear-fast-path-only",
                backend="rg",
                enforce_project=False,
                session_id="fast-path-task",
            )
            self.assertEqual(result["status"], "audited-fast-path")
            self.assertEqual(len(result["approved_citations"]), 1)
            self.assertTrue((vault / result["receipt_path"]).is_file())
            self.assertTrue((vault / result["audit_path"]).is_file())
            self.assertIn("packet_total_duration_ms", result["performance"])

    def test_fast_path_defers_ambiguous_rankings(self) -> None:
        selected, reason = FAST.select_high_confidence(
            [
                {"path": "one", "score": 100},
                {"path": "two", "score": 80},
                {"path": "three", "score": 70},
            ]
        )
        self.assertEqual(selected, [])
        self.assertEqual(reason, "too-many-strong-candidates")

    def test_fast_path_audits_zero_result_without_broadening(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            SETUP.initialize(vault, "local", "")
            self.make_vault(vault)
            result = FAST.run_fast_query(
                vault,
                "definitely-absent-evidence-marker",
                backend="rg",
                enforce_project=False,
                session_id="zero-result-task",
            )
            self.assertEqual(result["status"], "audited-no-result")
            self.assertEqual(result["approved_citations"], [])
            self.assertTrue(result["answer_contract"]["no_result"])

    def test_zero_result_requires_no_result_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = QUERY.run_query(
                vault,
                "量子海豚绝不存在词",
                "all",
                False,
                8,
                enforce_project=False,
                write_receipt=True,
            )
            self.assertEqual(result["result_count"], 0)
            with self.assertRaises(AUDIT.ComplianceError):
                AUDIT.run(
                    vault,
                    "量子海豚绝不存在词",
                    result["receipt_path"],
                    [],
                    False,
                    persist_audit=False,
                    enforce_project=False,
                )
            audit = AUDIT.run(
                vault,
                "量子海豚绝不存在词",
                result["receipt_path"],
                [],
                True,
                persist_audit=False,
                enforce_project=False,
            )
            self.assertTrue(audit["answer_contract"]["no_result"])

    def test_semantic_zero_result_requires_rejecting_every_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            target = next(vault.rglob("*.md")).parent / "candidate.md"
            target.write_text(
                "# generic-candidate\n\ngeneric-candidate\n",
                encoding="utf-8",
            )
            question = "generic-candidate"
            result = QUERY.run_query(
                vault,
                question,
                "all",
                False,
                8,
                backend="rg",
                enforce_project=False,
                write_receipt=True,
            )
            self.assertGreater(result["result_count"], 0)
            returned = [item["path"] for item in result["results"]]
            with self.assertRaises(AUDIT.ComplianceError):
                AUDIT.run(
                    vault,
                    question,
                    result["receipt_path"],
                    [],
                    True,
                    persist_audit=False,
                    enforce_project=False,
                    rejected_paths=returned[:-1],
                )
            audit = AUDIT.run(
                vault,
                question,
                result["receipt_path"],
                [],
                True,
                persist_audit=False,
                enforce_project=False,
                rejected_paths=returned,
            )
            self.assertTrue(audit["answer_contract"]["no_result"])
            self.assertEqual(set(audit["rejected_candidates"]), set(returned))

    def test_audit_rejects_evidence_changed_after_query(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            result = QUERY.run_query(
                vault,
                "销售",
                "enterprise",
                False,
                8,
                enforce_project=False,
                write_receipt=True,
            )
            citation = result["results"][0]["path"]
            (vault / citation).write_text("# 已变化\n", encoding="utf-8")
            with self.assertRaises(AUDIT.ComplianceError):
                AUDIT.run(
                    vault,
                    "销售",
                    result["receipt_path"],
                    [citation],
                    False,
                    persist_audit=False,
                    enforce_project=False,
                )

    def test_backend_router_prefers_obsidian_cli(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            roots = [vault / "20_知识/企业"]
            expected = [vault / "20_知识/企业/销售方法.md"]
            with (
                patch.object(
                    QUERY, "find_obsidian_cli", return_value=Path("Obsidian.com")
                ),
                patch.object(
                    QUERY,
                    "recall_with_obsidian_cli",
                    return_value=(expected, {"elapsed_ms": 1.0}),
                ) as cli_recall,
                patch.object(QUERY, "recall_with_rg") as rg_recall,
            ):
                candidates, backend, details = QUERY.retrieve_candidates(
                    vault,
                    roots,
                    ["销售"],
                    "auto",
                    allow_cli=True,
                )
            self.assertEqual(candidates, expected)
            self.assertEqual(backend, "obsidian-cli")
            self.assertEqual(details["elapsed_ms"], 1.0)
            cli_recall.assert_called_once()
            rg_recall.assert_not_called()

    def test_backend_router_falls_back_to_rg(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_vault(vault)
            roots = [vault / "20_知识/企业"]
            expected = [vault / "20_知识/企业/销售方法.md"]
            with (
                patch.object(
                    QUERY, "find_obsidian_cli", return_value=Path("Obsidian.com")
                ),
                patch.object(
                    QUERY,
                    "recall_with_obsidian_cli",
                    side_effect=QUERY.QueryError("CLI unavailable"),
                ),
                patch.object(
                    QUERY,
                    "recall_with_rg",
                    return_value=(expected, {"elapsed_ms": 2.0}),
                ) as rg_recall,
            ):
                candidates, backend, details = QUERY.retrieve_candidates(
                    vault,
                    roots,
                    ["销售"],
                    "auto",
                    allow_cli=True,
                )
            self.assertEqual(candidates, expected)
            self.assertEqual(backend, "rg")
            self.assertEqual(details["fallback_from"], "obsidian-cli")
            self.assertEqual(details["fallback_reason"], "CLI unavailable")
            rg_recall.assert_called_once()

    def test_global_query_and_audit_bind_to_configured_vault(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vault = root / "copied-vault"
            scripts = root / "installed-skill/scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(SCRIPT, scripts / SCRIPT.name)
            shutil.copy2(AUDIT_SCRIPT, scripts / AUDIT_SCRIPT.name)
            shutil.copy2(FAST_SCRIPT, scripts / FAST_SCRIPT.name)
            for name in (
                "vault_context.py",
                "kb_core.py",
                "membership_policy.py",
                "runtime_access.py",
                "company_sync_coordinator.py",
            ):
                shutil.copy2(SCRIPT.parent / name, scripts / name)
            self.make_vault(vault)
            (vault / ".kb").mkdir(parents=True)
            (vault / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            (vault / ".kb/config").mkdir(parents=True)
            bound_root = os.path.normcase(str(vault.resolve()))
            (vault / ".kb/config/project-binding.json").write_text(
                json.dumps(
                    {
                        "schema": "kb-project-binding/v1",
                        "binding_mode": "same-root",
                        "project_root": bound_root,
                        "vault_root": bound_root,
                    }
                ),
                encoding="utf-8",
            )
            (vault / ".kb/config/organization.json").write_text(
                json.dumps({"schema": "kb-organization/v3", "role": "local"}),
                encoding="utf-8",
            )
            (vault / "20_知识/个人/个人方法.md").write_text(
                "# portable-marker-only\n\n"
                "portable-marker-only portable-marker-only portable-marker-only\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["KB_VAULT_ROOT"] = str(vault)
            query = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(scripts / SCRIPT.name),
                    "--query",
                    "portable-marker-only",
                    "--backend",
                    "rg",
                ],
                cwd=vault,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(query.returncode, 0, query.stdout + query.stderr)
            result = json.loads(query.stdout)
            self.assertEqual(Path(result["vault"]), vault.resolve())
            self.assertEqual(result["result_count"], 1)

            audit = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(scripts / AUDIT_SCRIPT.name),
                    "--question",
                    "portable-marker-only",
                    "--receipt",
                    result["receipt_path"],
                    "--cited-path",
                    result["results"][0]["path"],
                ],
                cwd=vault,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
            self.assertTrue(json.loads(audit.stdout)["ok"])

            packet = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(scripts / FAST_SCRIPT.name),
                    "--query",
                    "portable-marker-only",
                    "--session-id",
                    "portable-task",
                    "--backend",
                    "rg",
                ],
                cwd=vault,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(packet.returncode, 0, packet.stdout + packet.stderr)
            self.assertEqual(
                json.loads(packet.stdout)["status"],
                "audited-fast-path",
            )


if __name__ == "__main__":
    unittest.main()

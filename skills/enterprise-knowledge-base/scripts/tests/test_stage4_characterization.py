import sys
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import company_sync_coordinator as coordinator
import query_answer_packet as packet
import setup_wizard as setup
from company_test_support import FakeKnowledgeReader, configure_admin, configure_employee


class Stage4CharacterizationTests(unittest.TestCase):
    def make_query_vault(self, vault: Path) -> None:
        setup.initialize(vault, "local", "")
        (vault / "20_知识/个人").mkdir(parents=True)
        (vault / "20_知识/企业").mkdir(parents=True)
        (vault / "30_导航").mkdir(parents=True, exist_ok=True)
        (vault / "20_知识/个人/复盘.md").write_text(
            "# 个人复盘\n\n个人知识复盘方法。\n",
            encoding="utf-8",
        )
        (vault / "20_知识/企业/制度.md").write_text(
            "# 企业制度\n\n企业知识审批制度。\n",
            encoding="utf-8",
        )

    def test_query_packet_output_and_citation_audit_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            self.make_query_vault(vault)
            result = packet.run_fast_query(
                vault,
                "个人知识复盘方法",
                backend="rg",
                enforce_project=False,
                session_id="characterization-task",
            )
            self.assertEqual(result["mode"], "current-vault-answer-packet")
            self.assertEqual(result["status"], "audited-fast-path")
            self.assertEqual(result["reason"], "high-confidence-ranking")
            self.assertEqual(
                result["results"][0]["path"],
                "20_知识/个人/复盘.md",
            )
            self.assertEqual(
                result["approved_citations"],
                ["20_知识/个人/复盘.md"],
            )
            self.assertFalse(result["external_sources_used"])
            self.assertEqual(result["sync"]["status"], "local-only")
            self.assertEqual(result["sync"]["remote_reads"], 0)
            receipt = json.loads(
                (vault / result["receipt_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["scope"], "all")
            self.assertIn("20_知识", receipt["searched_roots"])

    def test_company_query_sync_contract_is_once_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            reader = FakeKnowledgeReader()
            first = coordinator.before_knowledge_query(
                employee,
                "characterization-task",
                reader,
            )
            with patch.object(coordinator, "sha256_file", wraps=coordinator.sha256_file) as digest:
                second = coordinator.before_knowledge_query(
                    employee,
                    "characterization-task",
                    reader,
                )
            self.assertEqual(first["sync"], "incremental")
            self.assertEqual(second["sync"], "reused-current-session")
            self.assertEqual(second["remote_reads"], 0)
            self.assertEqual(reader.list_calls, 1)
            self.assertEqual(digest.call_count, 0)

    def test_formal_query_always_uses_scope_all_and_one_task_sync(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            personal = employee / "20_知识/个人/shared-personal-marker.md"
            enterprise = employee / "20_知识/企业/shared-enterprise-marker.md"
            personal.parent.mkdir(parents=True, exist_ok=True)
            enterprise.parent.mkdir(parents=True, exist_ok=True)
            personal.write_text(
                "# shared-personal-marker\n\n" + "shared-personal-marker " * 8,
                encoding="utf-8",
            )
            enterprise.write_text(
                "# shared-enterprise-marker\n\n" + "shared-enterprise-marker " * 8,
                encoding="utf-8",
            )
            reader = FakeKnowledgeReader()
            first = packet.run_fast_query(
                employee,
                "shared-personal-marker",
                backend="rg",
                enforce_project=False,
                session_id="all-knowledge-task",
                sync_reader=reader,
            )
            second = packet.run_fast_query(
                employee,
                "shared-enterprise-marker",
                backend="rg",
                enforce_project=False,
                session_id="all-knowledge-task",
                sync_reader=reader,
            )
            self.assertEqual(first["status"], "audited-fast-path")
            self.assertEqual(second["status"], "audited-fast-path")
            self.assertEqual(first["sync"]["status"], "incremental")
            self.assertEqual(second["sync"]["status"], "reused-current-session")
            self.assertEqual(reader.list_calls, 1)
            for result in (first, second):
                receipt = json.loads(
                    (employee / result["receipt_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["scope"], "all")
                self.assertTrue(receipt["managed_query"])
                self.assertTrue(receipt["sync_credential"]["credential_sha256"])


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import company_sync_coordinator as coordinator
import setup_wizard as setup
sync = coordinator
from company_test_support import (
    FakeKnowledgeReader,
    configure_admin,
    configure_employee,
    prepare_employee,
)


class CompanySyncTests(unittest.TestCase):
    def test_employee_setup_runs_first_full_sync_only_after_all_checks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = root / "employee"
            evidence = prepare_employee(admin, employee)
            reader = FakeKnowledgeReader()
            result = setup.verify_employee(
                employee,
                evidence,
                perform_initial_sync=True,
                sync_reader=reader,
            )
            self.assertEqual(result["initial_sync"]["trigger"], "employee-setup-complete")
            self.assertEqual(result["initial_sync"]["added"], 1)
            self.assertTrue((employee / sync.MIRROR_ROOT / "node-1.md").is_file())

    def test_failed_first_sync_rolls_back_employee_completion_and_writes_no_mirror(self) -> None:
        class BrokenReader(FakeKnowledgeReader):
            def list_tree(self, space_id: str):
                raise coordinator.CompanySyncError("read failed")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = root / "employee"
            evidence = prepare_employee(admin, employee)
            with self.assertRaises(coordinator.CompanySyncError):
                setup.verify_employee(
                    employee,
                    evidence,
                    perform_initial_sync=True,
                    sync_reader=BrokenReader(),
                )
            organization = setup.load_json(employee / ".kb/config/organization.json")
            self.assertFalse(organization["employee_access_verified"])
            self.assertFalse((employee / sync.MIRROR_ROOT).exists())

    def test_session_first_all_knowledge_query_syncs_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            reader = FakeKnowledgeReader()
            first = coordinator.before_knowledge_query(employee, "task-1", reader)
            second = coordinator.before_knowledge_query(employee, "task-1", reader)
            third = coordinator.before_knowledge_query(employee, "task-2", reader)
            self.assertEqual(first["sync"], "incremental")
            self.assertEqual(second["sync"], "reused-current-session")
            self.assertEqual(third["sync"], "incremental")
            self.assertEqual(reader.list_calls, 2)
            self.assertEqual(reader.fetch_calls, 1)

    def test_explicit_sync_is_incremental_idempotent_and_reports_summary(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            reader = FakeKnowledgeReader()
            first = coordinator.explicit_sync(employee, reader)
            second = coordinator.explicit_sync(employee, reader)
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["updated"], 0)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(second["local_authoritative"], 0)
            self.assertEqual(second["manual_review"], 0)

    def test_sync_metadata_fast_path_hashes_only_a_changed_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            reader = FakeKnowledgeReader()
            coordinator.explicit_sync(employee, reader)
            mirror = employee / coordinator.MIRROR_ROOT / "node-1.md"
            state = json.loads(
                (employee / ".kb/state/company_sync.json").read_text(encoding="utf-8")
            )
            record = state["records"]["node-1"]
            self.assertEqual(record["file_size"], mirror.stat().st_size)
            self.assertEqual(record["file_mtime_ns"], mirror.stat().st_mtime_ns)

            with mock.patch.object(
                coordinator,
                "sha256_file",
                wraps=coordinator.sha256_file,
            ) as digest:
                coordinator.explicit_sync(employee, reader)
            self.assertEqual(digest.call_count, 0)

            stat = mirror.stat()
            os.utime(
                mirror,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000),
            )
            with mock.patch.object(
                coordinator,
                "sha256_file",
                wraps=coordinator.sha256_file,
            ) as digest:
                result = coordinator.explicit_sync(employee, reader)
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(result["unchanged"], 1)

    def test_one_remote_change_fetches_exactly_one_body(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            reader = FakeKnowledgeReader()
            coordinator.explicit_sync(employee, reader)
            before = reader.fetch_calls
            reader.nodes[0]["obj_edit_time"] = "2"
            result = coordinator.explicit_sync(employee, reader)
            self.assertEqual(reader.fetch_calls - before, 1)
            self.assertEqual(result["updated"], 1)

    def test_remote_disappearance_local_edit_and_mapping_change_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            reader = FakeKnowledgeReader()
            coordinator.explicit_sync(employee, reader)
            mirror = employee / sync.MIRROR_ROOT / "node-1.md"
            mirror.write_text(mirror.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
            with mock.patch.object(
                coordinator, "atomic_write", wraps=coordinator.atomic_write
            ) as write:
                with self.assertRaises(coordinator.CompanySyncError):
                    coordinator.explicit_sync(employee, reader)
            self.assertEqual(write.call_count, 0)
            mirror.write_bytes(
                sync.mirror_bytes(
                    reader.nodes[0], reader.fetch_markdown("doc-1"), "123"
                )
            )
            reader.nodes = []
            with mock.patch.object(
                coordinator, "atomic_write", wraps=coordinator.atomic_write
            ) as write:
                with self.assertRaises(coordinator.CompanySyncError):
                    coordinator.explicit_sync(employee, reader)
            self.assertEqual(write.call_count, 0)

            reader.nodes = [
                {
                    "node_token": "node-1",
                    "obj_token": "doc-1",
                    "obj_type": "docx",
                    "title": "Policy",
                    "obj_edit_time": "1",
                }
            ]
            mapping_path = employee / ".kb/mappings/feishu_nodes.json"
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            mapping["nodes"][0]["node_name"] = "Changed"
            setup.atomic_write_json(mapping_path, mapping)
            with mock.patch.object(
                coordinator, "atomic_write", wraps=coordinator.atomic_write
            ) as write:
                with self.assertRaises(Exception):
                    coordinator.explicit_sync(employee, reader)
            self.assertEqual(write.call_count, 0)

    def test_publisher_converges_locally_without_background_push_to_other_employee(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            other = configure_employee(admin, root / "other")
            coordinator.explicit_sync(other, FakeKnowledgeReader())
            other_before = (other / ".kb/state/company_sync.json").read_bytes()

            note = admin / "20_知识/企业/published.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("---\nscope: enterprise\n---\nPublished\n", encoding="utf-8")
            setup.atomic_write_json(
                admin / ".kb/mappings/feishu_documents.json",
                {
                    "version": 2,
                    "documents": [
                        {
                            "node_token": "node-published",
                            "obj_token": "doc-published",
                            "local_path": "20_知识/企业/published.md",
                            "verified_content": True,
                            "last_synced_remote_hash": "d" * 64,
                        }
                    ],
                },
            )
            result = coordinator.record_publisher_convergence(admin)
            self.assertEqual(result["converged"], 1)
            self.assertEqual((other / ".kb/state/company_sync.json").read_bytes(), other_before)


if __name__ == "__main__":
    unittest.main()

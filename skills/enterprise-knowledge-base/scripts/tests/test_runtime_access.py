import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_access as access
import setup_wizard as setup
from company_test_support import configure_admin, configure_employee, prepare_employee


class RuntimeAccessTests(unittest.TestCase):
    def test_admin_and_employee_both_have_query_collect_publish_and_sync(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            for vault in (admin, employee):
                for action in ("query", "collect", "media", "publish", "sync"):
                    result = access.authorize(vault, action)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["capabilities"], ["query", "collect", "publish"])

    def test_oauth_or_import_without_space_membership_is_not_access(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = root / "employee"
            prepare_employee(admin, employee)
            with self.assertRaises(access.AccessError):
                access.authorize(employee, "query")

    def test_policy_downgrade_and_mapping_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            organization_path = employee / ".kb/config/organization.json"
            organization = setup.load_json(organization_path)
            organization["publish_policy"] = "admin-only"
            setup.atomic_write_json(organization_path, organization)
            with self.assertRaises(access.AccessError):
                access.authorize(employee, "query")

            organization["publish_policy"] = "members"
            setup.atomic_write_json(organization_path, organization)
            mapping_path = employee / ".kb/mappings/feishu_nodes.json"
            mapping = setup.load_json(mapping_path)
            mapping["nodes"][0]["node_name"] = "Changed"
            setup.atomic_write_json(mapping_path, mapping)
            with self.assertRaises(access.AccessError):
                access.authorize(employee, "sync")

    def test_local_mode_never_publishes_or_company_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "local"
            setup.initialize(vault, "local", "")
            self.assertTrue(access.authorize(vault, "query")["ok"])
            self.assertTrue(access.authorize(vault, "collect")["ok"])
            with self.assertRaises(access.AccessError):
                access.authorize(vault, "publish")
            with self.assertRaises(access.AccessError):
                access.authorize(vault, "sync")


if __name__ == "__main__":
    unittest.main()

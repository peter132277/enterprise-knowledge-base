import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import membership_policy as membership
import setup_wizard as setup


class FakeAdapter:
    def __init__(self) -> None:
        self.writes = 0
        self.allowed_admin_principal_hashes = set()

    def current_user_principal_hash(self) -> str:
        return "a" * 64

    def resolve_all_employees(self) -> dict:
        return {
            "kind": "organization-root",
            "selector_id": "root-1",
            "display_name": "All internal employees",
            "verified": True,
            "internal": True,
        }

    def apply_membership_plan(self, plan: dict) -> int:
        self.writes += 1
        return 1

    def member_list(self, space_id: str) -> dict:
        return {
            "schema": membership.MEMBERSHIP_READBACK_SCHEMA,
            "space_id": space_id,
            "remote_version": "v1",
            "external_sharing": False,
            "bindings": [{"selector_id": "root-1", "role": "member", "internal": True}],
            "members": [
                {
                    "principal_hash": "b" * 64,
                    "role": "member",
                    "internal": True,
                    "employee": True,
                    "deployer_admin": False,
                }
            ],
        }


class SetupMembershipAdapterTests(unittest.TestCase):
    def make_admin(self, root: Path) -> Path:
        vault = root / "admin"
        setup.initialize(vault, "admin", "Example")
        setup.atomic_write_json(
            vault / ".kb/mappings/feishu_nodes.json",
            {
                "version": 2,
                "space_name": "Knowledge",
                "space_id": "123",
                "nodes": [{"node_name": "Enterprise", "node_token": "n1", "verified": True}],
            },
        )
        return vault

    def test_preview_and_missing_confirmation_make_zero_member_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = self.make_admin(Path(folder))
            fake = FakeAdapter()
            preview = setup.preview_company_membership(vault, fake)
            denied = setup.apply_company_membership(vault, "not-confirmed", fake)
            self.assertEqual(preview["writes"], 0)
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["writes"], 0)
            self.assertEqual(fake.writes, 0)

    def test_exact_confirmation_applies_member_once_and_records_full_policy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = self.make_admin(Path(folder))
            fake = FakeAdapter()
            setup.preview_company_membership(vault, fake)
            result = setup.apply_company_membership(
                vault, membership.EXACT_CONFIRMATION, fake
            )
            self.assertTrue(result["membership_verified"])
            self.assertEqual(result["writes"], 1)
            organization = setup.load_json(vault / ".kb/config/organization.json")
            self.assertEqual(organization["share_scope"]["type"], "all-employees")
            self.assertEqual(organization["publish_policy"], "members")


if __name__ == "__main__":
    unittest.main()

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
        self.members: list[dict] = []
        self.last_plan: dict | None = None
        self.last_execution: dict | None = None
        self.fail_after_write = False

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

    def prepare_membership_plan(self, previously_managed: list[dict]) -> dict:
        scope = membership.resolve_share_scope("all-employees", [self.resolve_all_employees()])
        return membership.build_membership_plan(
            "123",
            scope,
            "members",
            current_members=self.members,
            previously_managed=previously_managed,
        )

    def apply_membership_plan(self, plan: dict) -> dict:
        self.last_plan = plan
        for operation in plan["operations"]:
            if operation["action"] == "add":
                exists = any(
                    item["member_id"] == operation["member_id"]
                    and item["member_type"] == operation["member_type"]
                    for item in self.members
                )
                if not exists:
                    self.members.append({
                        "member_id": operation["member_id"],
                        "member_type": operation["member_type"],
                        "member_role": "member",
                    })
                    self.writes += 1
                    if self.fail_after_write:
                        self.fail_after_write = False
                        self.last_execution = {"writes": self.writes, "operations": [], "outcome": "unknown"}
                        raise setup.CompanyAdapterError("unknown member-write outcome")
        self.last_execution = {"writes": self.writes, "operations": [], "outcome": "verified"}
        return self.last_execution

    def member_list(self, space_id: str) -> dict:
        return {
            "schema": membership.MEMBERSHIP_READBACK_SCHEMA,
            "space_id": space_id,
            "remote_version": "v1",
            "complete": True,
            "external_sharing": False,
            "members": [dict(item, internal=True, deployer_admin=False) for item in self.members],
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
            managed = setup.load_json(vault / ".kb/state/managed-members.json")
            self.assertEqual(managed["strategy"], "organization-root")

    def test_unknown_outcome_preserves_receipt_and_same_plan_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = self.make_admin(Path(folder))
            fake = FakeAdapter()
            setup.preview_company_membership(vault, fake)
            fake.fail_after_write = True
            with self.assertRaises(setup.SetupError):
                setup.apply_company_membership(vault, membership.EXACT_CONFIRMATION, fake)
            receipt = setup.load_json(vault / ".kb/state/membership-preview.json")
            self.assertEqual(receipt["status"], "outcome_unknown")
            with self.assertRaises(setup.SetupError):
                setup.preview_company_membership(vault, fake)
            recovered = setup.apply_company_membership(vault, membership.EXACT_CONFIRMATION, fake)
            self.assertTrue(recovered["membership_verified"])
            self.assertEqual(len(fake.members), 1)
            self.assertEqual(
                setup.load_json(vault / ".kb/state/membership-preview.json")["status"],
                "verified",
            )


if __name__ == "__main__":
    unittest.main()

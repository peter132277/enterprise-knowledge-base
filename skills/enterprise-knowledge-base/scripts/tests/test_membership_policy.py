import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import membership_policy as membership


def root_scope() -> dict:
    return membership.resolve_share_scope(
        "all-employees",
        [{
            "kind": "organization-root",
            "selector_id": "root-1",
            "display_name": "All internal employees",
            "verified": True,
            "internal": True,
        }],
    )


def active_user(member_id: str, **overrides: object) -> dict:
    value = {
        "member_id": member_id,
        "member_type": "openid",
        "display_name": "Employee",
        "internal": True,
        "external": False,
        "status": {
            "activated": True,
            "frozen": False,
            "resigned": False,
            "exited": False,
            "unjoined": False,
        },
    }
    value.update(overrides)
    return value


def directory(users: list[dict]) -> dict:
    return {
        "schema": membership.DIRECTORY_READBACK_SCHEMA,
        "complete": True,
        "all_employees_scope": True,
        "tenant_verified": True,
        "snapshot_version": "sha256:directory",
        "users": users,
    }


def readback(plan: dict, members: list[dict] | None = None) -> dict:
    if members is None:
        members = [
            {
                **item,
                "internal": True,
                "deployer_admin": False,
            }
            for item in plan["desired_members"]
        ]
    return {
        "schema": membership.MEMBERSHIP_READBACK_SCHEMA,
        "space_id": plan["space_id"],
        "remote_version": "member-version-7",
        "complete": True,
        "external_sharing": False,
        "members": members,
    }


class FakeAdapter:
    def __init__(self, result: dict):
        self.result = result
        self.writes = 0

    def apply_membership_plan(self, plan: dict) -> int:
        self.writes += 1
        return 1

    def member_list(self, space_id: str) -> dict:
        return self.result


class MembershipPolicyTests(unittest.TestCase):
    def test_root_scope_is_single_internal_department_binding(self) -> None:
        scope = root_scope()
        self.assertEqual(scope["strategy"], "organization-root")
        self.assertEqual(scope["subject_count"], 1)
        self.assertNotIn("selectors", scope)

    def test_ambiguous_root_fails_closed(self) -> None:
        candidates = [
            {"kind": "organization-root", "selector_id": value, "display_name": value, "verified": True, "internal": True}
            for value in ("one", "two")
        ]
        with self.assertRaises(membership.MembershipError):
            membership.resolve_share_scope("all-employees", candidates)

    def test_managed_fallback_filters_inactive_accounts_and_exports_no_roster(self) -> None:
        resigned = active_user("ou-left")
        resigned["status"]["resigned"] = True
        scope, desired, excluded = membership.resolve_managed_user_scope(
            directory([active_user("ou-active"), resigned])
        )
        self.assertEqual(scope["strategy"], "managed-users")
        self.assertEqual(scope["subject_count"], 1)
        self.assertEqual(excluded, 1)
        self.assertNotIn("ou-active", str(scope))
        self.assertEqual(desired[0]["member_id"], "ou-active")

    def test_incomplete_directory_or_status_fails_closed(self) -> None:
        value = directory([active_user("ou-active")])
        value["complete"] = False
        with self.assertRaises(membership.MembershipError):
            membership.resolve_managed_user_scope(value)
        value = directory([active_user("ou-active")])
        del value["users"][0]["status"]["unjoined"]
        with self.assertRaises(membership.MembershipError):
            membership.resolve_managed_user_scope(value)

    def test_plan_removes_only_previously_managed_and_preserves_manual_members(self) -> None:
        scope, desired, _ = membership.resolve_managed_user_scope(directory([active_user("ou-new")]))
        plan = membership.build_membership_plan(
            "123",
            scope,
            "members",
            desired_members=desired,
            current_members=[
                {"member_id": "ou-left", "member_type": "openid", "member_role": "member"},
                {"member_id": "ou-manual", "member_type": "openid", "member_role": "member"},
            ],
            previously_managed=[
                {"member_id": "ou-left", "member_type": "openid", "member_role": "member"}
            ],
        )
        self.assertEqual(
            [(item["action"], item["member_id"]) for item in plan["operations"]],
            [("remove", "ou-left"), ("add", "ou-new")],
        )
        self.assertNotIn("ou-manual", str(plan["operations"]))

    def test_managed_admin_is_never_downgraded_or_removed(self) -> None:
        scope, desired, _ = membership.resolve_managed_user_scope(directory([active_user("ou-active")]))
        with self.assertRaises(membership.MembershipError):
            membership.build_membership_plan(
                "123",
                scope,
                "members",
                desired_members=desired,
                current_members=[{"member_id": "ou-active", "member_type": "openid", "member_role": "admin"}],
            )

    def test_deployer_admin_remains_admin_in_managed_fallback(self) -> None:
        scope, desired, _ = membership.resolve_managed_user_scope(directory([active_user("ou-admin")]))
        plan = membership.build_membership_plan(
            "123",
            scope,
            "members",
            desired_members=desired,
            current_members=[{
                "member_id": "ou-admin",
                "member_type": "openid",
                "member_role": "admin",
                "deployer_admin": True,
            }],
        )
        self.assertEqual(plan["operations"], [])
        membership.verify_member_readback(
            plan,
            readback(plan, [{
                "member_id": "ou-admin",
                "member_type": "openid",
                "member_role": "admin",
                "internal": True,
                "deployer_admin": True,
            }]),
        )

    def test_preview_reports_strategy_counts_and_independent_confirmation(self) -> None:
        plan = membership.build_membership_plan("123", root_scope(), "members")
        preview = membership.human_preview(plan)
        self.assertEqual(preview["授权策略"], "根部门自动覆盖")
        self.assertEqual(preview["新增成员数"], 1)
        self.assertTrue(preview["需要单独确认"])

    def test_missing_confirmation_performs_zero_writes(self) -> None:
        plan = membership.build_membership_plan("123", root_scope(), "members")
        adapter = FakeAdapter(readback(plan))
        result = membership.execute_membership_change(adapter, plan, "")
        self.assertFalse(result["ok"])
        self.assertEqual(adapter.writes, 0)

    def test_full_readback_is_required(self) -> None:
        plan = membership.build_membership_plan("123", root_scope(), "members")
        value = readback(plan)
        value["complete"] = False
        with self.assertRaises(membership.MembershipError):
            membership.verify_member_readback(plan, value)
        value = readback(plan, [])
        with self.assertRaises(membership.MembershipError):
            membership.verify_member_readback(plan, value)

    def company_and_evidence(self) -> tuple[dict, dict]:
        plan = membership.build_membership_plan("123", root_scope(), "members")
        verification = membership.verify_member_readback(plan, readback(plan))
        company = {
            "tenant_key_hash": "b" * 64,
            "space_id": "123",
            "effective_employee_policy": "members",
            "membership_verification": verification,
        }
        evidence = {
            "schema": membership.EMPLOYEE_EVIDENCE_SCHEMA,
            "oauth_ok": True,
            "external_account": False,
            "tenant_key_hash": "b" * 64,
            "space_visible": True,
            "space_member": True,
            "space_id": "123",
            "inside_authorized_scope": True,
            "space_role": "member",
            "membership_version": verification["remote_version"],
            "membership_hash": verification["member_list_hash"],
        }
        return company, evidence

    def test_employee_oauth_still_requires_exact_membership(self) -> None:
        company, evidence = self.company_and_evidence()
        evidence["space_member"] = False
        with self.assertRaises(membership.MembershipError):
            membership.verify_employee_access(company, evidence)
        company, evidence = self.company_and_evidence()
        evidence["external_account"] = True
        with self.assertRaises(membership.MembershipError):
            membership.verify_employee_access(company, evidence)

    def test_employee_cannot_override_company_policy(self) -> None:
        company, evidence = self.company_and_evidence()
        with self.assertRaises(membership.MembershipError):
            membership.verify_employee_access(company, evidence, requested_policy="admin-only")


if __name__ == "__main__":
    unittest.main()

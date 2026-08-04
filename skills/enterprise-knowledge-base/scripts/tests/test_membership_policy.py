import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import membership_policy as membership


def candidate(kind: str, selector_id: str, name: str) -> dict:
    return {
        "kind": kind,
        "selector_id": selector_id,
        "display_name": name,
        "verified": True,
        "internal": True,
    }


def valid_scope(scope_type: str = "all-employees") -> dict:
    kinds = {
        "all-employees": "organization-root",
        "departments": "department",
        "groups": "group",
        "users": "user",
    }
    return membership.resolve_share_scope(
        scope_type, [candidate(kinds[scope_type], f"id-{scope_type}", "可读范围")]
    )


def valid_readback(plan: dict) -> dict:
    return {
        "schema": membership.MEMBERSHIP_READBACK_SCHEMA,
        "space_id": plan["space_id"],
        "remote_version": "member-version-7",
        "external_sharing": False,
        "bindings": [
            {
                "selector_id": selector["selector_id"],
                "role": "member",
                "internal": True,
            }
            for selector in plan["share_scope"]["selectors"]
        ],
        "members": [
            {
                "principal_hash": "a" * 64,
                "role": "member",
                "internal": True,
                "employee": True,
                "deployer_admin": False,
            }
        ],
    }


class FakeAdapter:
    def __init__(self, readback: dict):
        self.readback = readback
        self.writes = 0

    def apply_membership_plan(self, plan: dict) -> int:
        self.writes += 1
        return 1

    def member_list(self, space_id: str) -> dict:
        return self.readback


class MembershipPolicyTests(unittest.TestCase):
    def test_default_all_internal_employees_scope(self) -> None:
        scope = valid_scope()
        plan = membership.build_membership_plan("123", scope, "members")
        preview = membership.human_preview(plan)
        self.assertEqual(preview["共享范围"], "全公司内部员工")
        self.assertEqual(preview["员工功能"], "查询、收录并发布")
        self.assertEqual(preview["知识空间角色"], "成员")
        self.assertEqual(preview["外部分享"], "关闭")
        self.assertNotIn("id-all-employees", str(preview))

    def test_department_group_and_user_scopes_are_rejected(self) -> None:
        for scope_type in ("departments", "groups", "users"):
            with self.subTest(scope_type=scope_type):
                with self.assertRaises(membership.MembershipError):
                    valid_scope(scope_type)

    def test_ambiguous_all_employees_resolution_fails_closed(self) -> None:
        candidates = [
            candidate("organization-root", "one", "根一"),
            candidate("organization-root", "two", "根二"),
        ]
        with self.assertRaises(membership.MembershipError):
            membership.resolve_share_scope("all-employees", candidates)

    def test_missing_confirmation_performs_zero_writes(self) -> None:
        plan = membership.build_membership_plan("123", valid_scope(), "members")
        adapter = FakeAdapter(valid_readback(plan))
        result = membership.execute_membership_change(adapter, plan, "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["writes"], 0)
        self.assertEqual(adapter.writes, 0)

    def test_member_list_mismatch_never_completes(self) -> None:
        plan = membership.build_membership_plan("123", valid_scope(), "members")
        readback = valid_readback(plan)
        readback["bindings"] = []
        adapter = FakeAdapter(readback)
        with self.assertRaises(membership.MembershipError):
            membership.execute_membership_change(
                adapter, plan, membership.EXACT_CONFIRMATION
            )
        self.assertEqual(adapter.writes, 1)

    def test_normal_employee_cannot_become_space_admin(self) -> None:
        plan = membership.build_membership_plan("123", valid_scope(), "members")
        readback = valid_readback(plan)
        readback["members"][0]["role"] = "admin"
        with self.assertRaises(membership.MembershipError):
            membership.verify_member_readback(plan, readback)

    def company_and_evidence(self) -> tuple[dict, dict]:
        plan = membership.build_membership_plan("123", valid_scope(), "members")
        verification = membership.verify_member_readback(plan, valid_readback(plan))
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

    def test_oauth_without_space_membership_fails_closed(self) -> None:
        company, evidence = self.company_and_evidence()
        evidence["space_member"] = False
        with self.assertRaises(membership.MembershipError):
            membership.verify_employee_access(company, evidence)

    def test_external_or_out_of_scope_employee_fails_closed(self) -> None:
        for field in ("external_account", "inside_authorized_scope"):
            company, evidence = self.company_and_evidence()
            evidence[field] = True if field == "external_account" else False
            with self.subTest(field=field), self.assertRaises(membership.MembershipError):
                membership.verify_employee_access(company, evidence)

    def test_employee_cannot_override_publication_policy(self) -> None:
        company, evidence = self.company_and_evidence()
        with self.assertRaises(membership.MembershipError):
            membership.verify_employee_access(company, evidence, requested_policy="admin-only")


if __name__ == "__main__":
    unittest.main()

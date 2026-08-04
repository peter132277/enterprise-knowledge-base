import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import feishu_company_adapter as adapter
import membership_policy as membership


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.members: list[dict[str, str]] = []

    def __call__(self, argv: list[str]) -> dict:
        self.calls.append(argv)
        joined = " ".join(argv)
        if "auth status" in joined:
            return {
                "ok": True,
                "data": {
                    "identities": {
                        "user": {"status": "active", "openId": "ou-admin"}
                    }
                },
            }
        if "/open-apis/contact/v3/departments/0" in joined:
            return {
                "ok": True,
                "data": {
                    "department": {
                        "open_department_id": "root-1",
                        "name": "All employees",
                        "parent_department_id": "0",
                    }
                },
            }
        if "spaces get" in joined:
            return {
                "ok": True,
                "data": {
                    "space": {
                        "space_id": "123",
                        "space_type": "team",
                        "visibility": "private",
                        "open_sharing": "closed",
                    }
                },
            }
        if "+member-list" in argv:
            return {"ok": True, "data": {"has_more": False, "members": list(self.members)}}
        if "+member-add" in argv:
            self.members.append(
                {
                    "member_id": argv[argv.index("--member-id") + 1],
                    "member_type": argv[argv.index("--member-type") + 1],
                    "member_role": argv[argv.index("--member-role") + 1],
                }
            )
            return {"ok": True, "data": {}}
        if "+node-list" in argv:
            return {
                "ok": True,
                "data": {
                    "has_more": False,
                    "nodes": [
                        {
                            "node_token": "node-1",
                            "obj_token": "doc-1",
                            "obj_type": "docx",
                            "title": "Policy",
                            "has_child": False,
                        }
                    ],
                },
            }
        if "+fetch" in argv:
            return {
                "ok": True,
                "data": {
                    "document": {
                        "content": "# Policy",
                        "revision_id": "7",
                        "document_id": "doc-1",
                    }
                },
            }
        raise AssertionError(argv)


def full_plan() -> dict:
    scope = membership.resolve_share_scope(
        "all-employees",
        [
            {
                "kind": "organization-root",
                "selector_id": "root-1",
                "display_name": "All employees",
                "verified": True,
                "internal": True,
            }
        ],
    )
    return membership.build_membership_plan("123", scope, "members")


class FeishuCompanyAdapterTests(unittest.TestCase):
    def test_full_company_plan_uses_user_department_member_write_and_readback(self) -> None:
        runner = FakeRunner()
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        self.assertEqual(len(managed.current_user_principal_hash()), 64)
        self.assertEqual(managed.resolve_all_employees()["selector_id"], "root-1")
        self.assertEqual(managed.apply_membership_plan(full_plan()), 1)
        readback = managed.member_list("123")
        self.assertEqual(readback["bindings"][0]["role"], "member")
        add = next(call for call in runner.calls if "+member-add" in call)
        self.assertEqual(add[add.index("--member-type") + 1], "opendepartmentid")
        self.assertEqual(add[add.index("--member-role") + 1], "member")
        self.assertEqual(add[add.index("--as") + 1], "user")
        self.assertNotIn("bot", add)

    def test_matching_grant_is_idempotent_and_conflicting_admin_grant_fails(self) -> None:
        runner = FakeRunner()
        runner.members = [
            {"member_id": "root-1", "member_type": "opendepartmentid", "member_role": "member"}
        ]
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        self.assertEqual(managed.apply_membership_plan(full_plan()), 0)
        self.assertFalse(any("+member-add" in call for call in runner.calls))
        runner.members[0]["member_role"] = "admin"
        with self.assertRaises(adapter.CompanyAdapterError):
            managed.apply_membership_plan(full_plan())

    def test_public_or_open_space_fails_closed(self) -> None:
        runner = FakeRunner()
        original = runner.__call__

        def opened(argv: list[str]) -> dict:
            value = original(argv)
            if "spaces" in argv and "get" in argv:
                value["data"]["space"]["open_sharing"] = "open"
            return value

        managed = adapter.LarkMembershipAdapter("123", runner=opened)
        with self.assertRaises(adapter.CompanyAdapterError):
            managed.apply_membership_plan(full_plan())

    def test_knowledge_reader_is_read_only_user_scoped_and_complete(self) -> None:
        runner = FakeRunner()
        reader = adapter.LarkKnowledgeReader(runner=runner)
        nodes = reader.list_tree("123")
        fetched = reader.fetch_markdown("doc-1")
        self.assertEqual(nodes[0]["node_token"], "node-1")
        self.assertEqual(fetched["revision_id"], "7")
        for call in runner.calls:
            if any(value in call for value in ("+node-list", "+fetch")):
                self.assertEqual(call[call.index("--as") + 1], "user")
                self.assertFalse(any(value in call for value in ("+node-create", "+member-add", "upload")))


if __name__ == "__main__":
    unittest.main()

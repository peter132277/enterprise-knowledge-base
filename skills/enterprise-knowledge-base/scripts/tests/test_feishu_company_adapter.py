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
        self.root_available = True
        self.fail_add_after_write = False
        self.directory_users: list[dict] = []
        self.broken_directory_pagination = False
        self.spaces: list[dict[str, str]] = [
            {
                "space_id": "123",
                "name": "Knowledge",
                "description": "existing",
                "space_type": "team",
                "visibility": "private",
                "open_sharing": "closed",
            }
        ]
        self.wiki_nodes: list[dict[str, object]] = [
            {
                "space_id": "123",
                "node_token": "node-1",
                "obj_token": "doc-1",
                "obj_type": "docx",
                "title": "Policy",
                "parent_node_token": "",
                "has_child": False,
            }
        ]

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
        if "/open-apis/contact/v3/departments/0" in joined and "/children" not in joined:
            if not self.root_available:
                raise adapter.CompanyAdapterError("organization root unavailable")
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
        if "/open-apis/contact/v3/departments/" in joined and "/children" in joined:
            if self.broken_directory_pagination:
                return {"ok": True, "data": {"has_more": True, "items": []}}
            return {"ok": True, "data": {"has_more": False, "items": []}}
        if "/open-apis/contact/v3/users/find_by_department" in joined:
            return {"ok": True, "data": {"has_more": False, "items": list(self.directory_users)}}
        if "spaces get" in joined:
            space_id = argv[argv.index("--space-id") + 1]
            space = next(item for item in self.spaces if item["space_id"] == space_id)
            return {
                "ok": True,
                "data": {"space": dict(space)},
            }
        if "+space-list" in argv:
            return {
                "ok": True,
                "data": {"has_more": False, "spaces": list(self.spaces)},
            }
        if "+space-create" in argv:
            name = argv[argv.index("--name") + 1]
            description = (
                argv[argv.index("--description") + 1]
                if "--description" in argv
                else ""
            )
            created = {
                "space_id": "456",
                "name": name,
                "description": description,
                "space_type": "team",
                "visibility": "private",
                "open_sharing": "closed",
            }
            self.spaces.append(created)
            return {"ok": True, "data": dict(created)}
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
            if self.fail_add_after_write:
                self.fail_add_after_write = False
                raise adapter.CompanyAdapterError("transport outcome unknown")
            return {"ok": True, "data": {}}
        if "+member-remove" in argv:
            key = (
                argv[argv.index("--member-type") + 1],
                argv[argv.index("--member-id") + 1],
            )
            self.members = [
                item for item in self.members
                if (item["member_type"], item["member_id"]) != key
            ]
            return {"ok": True, "data": {}}
        if "+node-create" in argv:
            space_id = argv[argv.index("--space-id") + 1]
            parent = (
                argv[argv.index("--parent-node-token") + 1]
                if "--parent-node-token" in argv
                else ""
            )
            node = {
                "space_id": space_id,
                "node_token": f"node-{len(self.wiki_nodes) + 1}",
                "obj_token": f"doc-{len(self.wiki_nodes) + 1}",
                "obj_type": argv[argv.index("--obj-type") + 1],
                "title": argv[argv.index("--title") + 1],
                "parent_node_token": parent,
                "has_child": False,
            }
            self.wiki_nodes.append(node)
            return {"ok": True, "data": {"node": dict(node)}}
        if "+node-list" in argv:
            space_id = argv[argv.index("--space-id") + 1]
            parent = (
                argv[argv.index("--parent-node-token") + 1]
                if "--parent-node-token" in argv
                else ""
            )
            nodes = [
                dict(item)
                for item in self.wiki_nodes
                if item["space_id"] == space_id
                and item["parent_node_token"] == parent
            ]
            return {
                "ok": True,
                "data": {
                    "has_more": False,
                    "nodes": nodes,
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
    def test_space_setup_lists_creates_and_reads_back_with_user_identity(self) -> None:
        runner = FakeRunner()
        runner.spaces = []
        managed = adapter.LarkSpaceSetupAdapter(runner=runner)
        self.assertEqual(len(managed.current_user_principal_hash()), 64)
        self.assertEqual(managed.list_spaces(), [])
        created = managed.create_space("企业知识库", "公司共享知识")
        self.assertEqual(created["space_id"], "456")
        self.assertEqual(managed.verify_space("456"), created)
        create = next(call for call in runner.calls if "+space-create" in call)
        self.assertEqual(create[create.index("--as") + 1], "user")
        self.assertNotIn("bot", create)
        self.assertNotIn("+member-add", create)
        self.assertEqual(managed.list_tree("456"), [])
        node = managed.create_node("456", "00｜知识库首页")
        self.assertEqual(node["title"], "00｜知识库首页")
        self.assertEqual(len(managed.list_tree("456")), 1)
        node_create = next(call for call in runner.calls if "+node-create" in call)
        self.assertEqual(node_create[node_create.index("--as") + 1], "user")
        self.assertNotIn("+member-add", node_create)
        self.assertNotIn("content", " ".join(node_create).casefold())

    def test_current_user_accepts_only_verified_ready_status(self) -> None:
        def ready(argv: list[str]) -> dict:
            return {
                "verified": True,
                "identities": {
                    "user": {"status": "ready", "openId": "ou-admin"}
                },
            }

        self.assertEqual(len(adapter.current_user_principal_hash(ready)), 64)

        def unverified_ready(argv: list[str]) -> dict:
            return {
                "verified": False,
                "identities": {
                    "user": {"status": "ready", "openId": "ou-admin"}
                },
            }

        with self.assertRaises(adapter.CompanyAdapterError):
            adapter.current_user_principal_hash(unverified_ready)

    def test_space_setup_rejects_incomplete_pagination_and_open_space(self) -> None:
        runner = FakeRunner()
        original = runner.__call__

        def incomplete(argv: list[str]) -> dict:
            value = original(argv)
            if "+space-list" in argv:
                value["data"]["has_more"] = True
            return value

        with self.assertRaises(adapter.CompanyAdapterError):
            adapter.LarkSpaceSetupAdapter(runner=incomplete).list_spaces()

        def opened(argv: list[str]) -> dict:
            value = original(argv)
            if "spaces" in argv and "get" in argv:
                value["data"]["space"]["open_sharing"] = "open"
            return value

        with self.assertRaises(adapter.CompanyAdapterError):
            adapter.LarkSpaceSetupAdapter(runner=opened).verify_space("123")

    def test_full_company_plan_uses_user_department_member_write_and_readback(self) -> None:
        runner = FakeRunner()
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        self.assertEqual(len(managed.current_user_principal_hash()), 64)
        self.assertEqual(managed.resolve_all_employees()["selector_id"], "root-1")
        self.assertEqual(managed.apply_membership_plan(full_plan())["writes"], 1)
        readback = managed.member_list("123")
        self.assertEqual(readback["members"][0]["member_role"], "member")
        self.assertTrue(readback["complete"])
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
        self.assertEqual(managed.apply_membership_plan(full_plan())["writes"], 0)
        self.assertFalse(any("+member-add" in call for call in runner.calls))
        runner.members[0]["member_role"] = "admin"
        with self.assertRaises(adapter.CompanyAdapterError):
            managed.apply_membership_plan(full_plan())

    def test_root_failure_uses_complete_active_user_fallback(self) -> None:
        runner = FakeRunner()
        runner.root_available = False
        runner.directory_users = [
            {
                "open_id": "ou-active",
                "name": "Active",
                "status": {
                    "is_activated": True,
                    "is_frozen": False,
                    "is_resigned": False,
                    "is_exited": False,
                    "is_unjoin": False,
                },
            },
            {
                "open_id": "ou-left",
                "name": "Left",
                "status": {
                    "is_activated": False,
                    "is_frozen": False,
                    "is_resigned": True,
                    "is_exited": False,
                    "is_unjoin": False,
                },
            },
        ]
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        plan = managed.prepare_membership_plan()
        self.assertEqual(plan["share_scope"]["strategy"], "managed-users")
        self.assertEqual(plan["excluded_count"], 1)
        self.assertEqual([item["member_id"] for item in plan["desired_members"]], ["ou-active"])
        self.assertEqual(managed.apply_membership_plan(plan)["writes"], 1)

    def test_unknown_add_outcome_recovers_only_from_readback(self) -> None:
        runner = FakeRunner()
        runner.fail_add_after_write = True
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        result = managed.apply_membership_plan(full_plan())
        self.assertEqual(result["writes"], 1)
        self.assertEqual(result["operations"][0]["status"], "recovered-by-readback")
        self.assertEqual(len([call for call in runner.calls if "+member-add" in call]), 1)

    def test_fallback_rejects_incomplete_directory_pagination(self) -> None:
        runner = FakeRunner()
        runner.root_available = False
        runner.broken_directory_pagination = True
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        with self.assertRaises(adapter.CompanyAdapterError):
            managed.prepare_membership_plan()

    def test_root_transition_removes_only_previous_managed_user(self) -> None:
        runner = FakeRunner()
        runner.members = [
            {"member_id": "ou-left", "member_type": "openid", "member_role": "member"},
            {"member_id": "ou-manual", "member_type": "openid", "member_role": "member"},
        ]
        managed = adapter.LarkMembershipAdapter("123", runner=runner)
        plan = managed.prepare_membership_plan([
            {"member_id": "ou-left", "member_type": "openid", "member_role": "member"}
        ])
        self.assertEqual(
            [(item["action"], item["member_id"]) for item in plan["operations"]],
            [("remove", "ou-left"), ("add", "root-1")],
        )
        managed.apply_membership_plan(plan)
        remove = next(call for call in runner.calls if "+member-remove" in call)
        self.assertEqual(remove[remove.index("--member-role") + 1], "member")
        self.assertEqual(remove[remove.index("--as") + 1], "user")
        self.assertEqual(
            sorted(item["member_id"] for item in runner.members),
            ["ou-manual", "root-1"],
        )

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

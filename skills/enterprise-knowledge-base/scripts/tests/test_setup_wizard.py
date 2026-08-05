import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import membership_policy as membership
import setup_wizard as setup


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class FakeSpaceSetupAdapter:
    def __init__(self) -> None:
        self.identity = "a" * 64
        self.spaces: list[dict[str, str]] = []
        self.create_calls = 0
        self.create_error = False
        self.verify_failures = 0
        self.nodes: list[dict[str, object]] = []
        self.node_create_calls: list[dict[str, str]] = []
        self.node_error_before_write_at = 0
        self.node_error_after_write_at = 0
        self.tree_calls = 0
        self.tree_failure_at = 0

    def current_user_principal_hash(self) -> str:
        return self.identity

    def list_spaces(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.spaces]

    def create_space(self, name: str, description: str) -> dict[str, str]:
        self.create_calls += 1
        if self.create_error:
            raise setup.CompanyAdapterError("transport failed")
        created = {
            "space_id": "456",
            "name": name,
            "description": description,
            "space_type": "team",
            "visibility": "private",
            "open_sharing": "closed",
        }
        self.spaces.append(created)
        return dict(created)

    def verify_space(self, space_id: str) -> dict[str, str]:
        if self.verify_failures:
            self.verify_failures -= 1
            raise setup.CompanyAdapterError("readback unavailable")
        return dict(next(item for item in self.spaces if item["space_id"] == space_id))

    def list_tree(self, space_id: str) -> list[dict[str, object]]:
        self.tree_calls += 1
        if self.tree_failure_at == self.tree_calls:
            raise setup.CompanyAdapterError("tree readback unavailable")
        return [dict(item) for item in self.nodes]

    def create_node(
        self,
        space_id: str,
        title: str,
        parent_node_token: str = "",
        obj_type: str = "docx",
    ) -> dict[str, str]:
        call_number = len(self.node_create_calls) + 1
        call = {
            "space_id": space_id,
            "title": title,
            "parent_node_token": parent_node_token,
            "obj_type": obj_type,
        }
        self.node_create_calls.append(call)
        if self.node_error_before_write_at == call_number:
            raise setup.CompanyAdapterError("transport failed before observable write")
        created = {
            "space_id": space_id,
            "node_token": f"node-{call_number}",
            "obj_token": f"doc-{call_number}",
            "obj_type": obj_type,
            "title": title,
            "parent_node_token": parent_node_token,
            "has_child": False,
        }
        self.nodes.append(created)
        if self.node_error_after_write_at == call_number:
            self.node_error_after_write_at = 0
            raise setup.CompanyAdapterError("transport failed after remote write")
        return {
            key: str(created[key])
            for key in (
                "node_token",
                "obj_token",
                "obj_type",
                "title",
                "parent_node_token",
            )
        }


class SetupWizardTests(unittest.TestCase):
    def configure_admin(self, root: Path) -> tuple[Path, Path]:
        admin = root / "admin"
        setup.initialize(admin, "admin", "Example")
        organization = setup.load_json(admin / ".kb/config/organization.json")
        organization.update(
            {
                "app_id": "cli_abc123",
                "feishu_brand": "feishu",
                "tenant_key_hash": "b" * 64,
            }
        )
        setup.atomic_write_json(admin / ".kb/config/organization.json", organization)
        setup.atomic_write_json(
            admin / ".kb/mappings/feishu_nodes.json",
            {
                "version": 2,
                "space_name": "Knowledge",
                "space_id": "123",
                "nodes": [
                    {"node_name": "Enterprise", "node_token": "wikcn123", "verified": True}
                ],
            },
        )
        scope = membership.resolve_share_scope(
            "all-employees",
            [
                {
                    "kind": "organization-root",
                    "selector_id": "root-1",
                    "display_name": "全公司内部员工",
                    "verified": True,
                    "internal": True,
                }
            ],
        )
        plan = membership.build_membership_plan("123", scope, "members")
        readback = {
            "schema": membership.MEMBERSHIP_READBACK_SCHEMA,
            "space_id": "123",
            "remote_version": "version-9",
            "complete": True,
            "external_sharing": False,
            "members": [
                {
                    "member_id": "root-1",
                    "member_type": "opendepartmentid",
                    "member_role": "member",
                    "internal": True,
                    "deployer_admin": False,
                }
            ],
        }
        scope_file = root / "scope.json"
        readback_file = root / "readback.json"
        setup.atomic_write_json(scope_file, scope)
        setup.atomic_write_json(readback_file, readback)
        setup.record_membership_verification(admin, scope_file, readback_file, "members")
        package = root / "company.json"
        setup.export_company(admin, package)
        return admin, package

    def test_empty_current_project_previews_then_initializes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            with patch.object(setup, "current_project", return_value=vault):
                preview = setup.inspect_current_project()
                self.assertTrue(preview["ok"])
                self.assertEqual(preview["path_status"], "ready")
                self.assertEqual(snapshot(vault), {})
                result = setup.initialize_current_project("local", "", True)
            self.assertTrue(result["configured"])
            self.assertEqual(result["network_requests"], 0)
            self.assertEqual(result["system_software_installations"], 0)
            self.assertEqual(result["obsidian_writes"], 0)
            binding = setup.load_json(vault / ".kb/config/project-binding.json")
            self.assertEqual(binding["project_root"], setup.canonical_path(vault))
            self.assertEqual(binding["vault_root"], setup.canonical_path(vault))

    def test_confirmation_gate_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            with patch.object(setup, "current_project", return_value=vault):
                with self.assertRaises(setup.SetupError):
                    setup.initialize_current_project("local", "", False)
            self.assertEqual(snapshot(vault), {})

    def test_only_obsidian_vault_is_allowed_and_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            plugin_file = vault / ".obsidian/plugins/example/data.json"
            plugin_file.parent.mkdir(parents=True)
            plugin_file.write_bytes(b'{"preserve":true}\r\n')
            before = plugin_file.read_bytes()
            preview = setup.inspect_project(vault)
            self.assertTrue(preview["ok"], preview["conflicts"])
            setup.initialize(vault, "local", "")
            self.assertEqual(plugin_file.read_bytes(), before)
            self.assertFalse((vault / ".claudian").exists())

    def test_existing_notes_and_attachments_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            note = vault / "会议记录.md"
            attachment = vault / "附件/image.bin"
            attachment.parent.mkdir()
            note.write_bytes("# 原笔记\n不覆盖\n".encode("utf-8"))
            attachment.write_bytes(b"\x00\x01keep")
            before = {"note": note.read_bytes(), "attachment": attachment.read_bytes()}
            preview = setup.inspect_project(vault)
            self.assertTrue(preview["ok"], preview["conflicts"])
            self.assertIn("会议记录.md", preview["ordinary_entries_preserved"])
            setup.initialize(vault, "local", "")
            self.assertEqual(note.read_bytes(), before["note"])
            self.assertEqual(attachment.read_bytes(), before["attachment"])

    def test_unknown_agents_and_invalid_kb_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            unknown = root / "unknown-agents"
            unknown.mkdir()
            (unknown / "AGENTS.md").write_text("# user rules\n", encoding="utf-8")
            plan = setup.inspect_project(unknown)
            self.assertFalse(plan["ok"])
            self.assertIn("unknown-agents", {item["code"] for item in plan["conflicts"]})
            with self.assertRaises(setup.SetupError):
                setup.initialize(unknown, "local", "")

            invalid = root / "invalid-kb"
            invalid.mkdir()
            (invalid / ".kb").write_text("not a directory", encoding="utf-8")
            plan = setup.inspect_project(invalid)
            self.assertFalse(plan["ok"])
            self.assertIn("invalid-kb", {item["code"] for item in plan["conflicts"]})

    def test_reserved_directory_type_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            (vault / "20_知识").write_text("conflict", encoding="utf-8")
            plan = setup.inspect_project(vault)
            self.assertFalse(plan["ok"])
            self.assertIn(
                {"code": "reserved-path-type-conflict", "path": "20_知识"},
                plan["conflicts"],
            )
            with self.assertRaises(setup.SetupError):
                setup.initialize(vault, "local", "")

    def test_initialization_has_no_network_or_system_install_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(
            setup.subprocess, "run", side_effect=AssertionError("must not run")
        ):
            result = setup.initialize(Path(folder), "local", "")
            self.assertTrue(result["configured"])
            self.assertFalse(hasattr(setup, "install_obsidian"))
            self.assertFalse(hasattr(setup, "install_claudian"))
            self.assertFalse(hasattr(setup, "open_obsidian"))

    def test_existing_v041_project_reuses_state_and_updates_official_agents(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            setup.initialize(vault, "admin", "Example")
            state_path = vault / ".kb/state/processed_files.json"
            setup.atomic_write_json(state_path, {"version": 2, "files": [{"keep": True}]})
            old_agents = b"official-v0.4.1-template-fixture"
            (vault / "AGENTS.md").write_bytes(old_agents)
            real_hash = setup.sha256_file

            def recognized_hash(path: Path) -> str:
                if path.resolve() == (vault / "AGENTS.md").resolve():
                    return setup.V041_AGENTS_SHA256
                return real_hash(path)

            with patch.object(setup, "sha256_file", side_effect=recognized_hash):
                plan = setup.inspect_project(vault)
                self.assertTrue(plan["ok"], plan["conflicts"])
                setup.initialize(vault, "admin", "Example")
            self.assertEqual(
                setup.load_json(state_path),
                {"version": 2, "files": [{"keep": True}]},
            )
            self.assertEqual((vault / "AGENTS.md").read_bytes(), setup.template_agents().read_bytes())

    def test_legacy_three_skill_vault_is_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            (vault / ".kb/config").mkdir(parents=True)
            (vault / "AGENTS.md").write_text("# legacy rules\n", encoding="utf-8")
            setup.atomic_write_json(
                vault / ".kb/config/organization.json",
                {"schema": "legacy", "role": "admin", "company_name": "Example"},
            )
            for name in setup.LEGACY_SKILL_NAMES:
                skill = vault / ".agents/skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            plan = setup.inspect_project(vault)
            self.assertEqual(plan["path_status"], "legacy")
            result = setup.initialize(vault, "admin", "Example")
            self.assertTrue(result["legacy_migration"]["migrated"])
            backup = vault / result["legacy_migration"]["backup"]
            self.assertEqual((backup / "AGENTS.md").read_text(encoding="utf-8"), "# legacy rules\n")
            for name in setup.LEGACY_SKILL_NAMES:
                self.assertTrue((backup / name / "SKILL.md").is_file())

    def test_project_binding_rejects_a_different_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            other = Path(folder) / "other"
            setup.initialize(vault, "local", "")
            with self.assertRaises(setup.SetupError):
                setup.bind_project(vault, other)

    def test_default_company_sharing_and_employee_policy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            organization = setup.load_json(vault / ".kb/config/organization.json")
            self.assertEqual(organization["share_scope"]["type"], "all-employees")
            self.assertEqual(organization["publish_policy"], "members")
            self.assertFalse(organization["membership_verified"])

    def test_admin_can_preview_create_and_read_back_private_space(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            preview = setup.preview_space_creation(
                vault, "企业知识库", "公司共享知识", adapter
            )
            self.assertEqual(preview["confirmation"], "确认创建知识空间")
            self.assertEqual(preview["remote_writes"], 0)
            self.assertEqual(preview["preview"]["member_change"], "不包含；创建后单独预览和确认")
            result = setup.apply_space_creation(
                vault, "确认创建知识空间", adapter
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["remote_writes"], 1)
            self.assertEqual(result["membership_writes"], 0)
            self.assertEqual(result["next_confirmation"], "确认初始化知识库模板")
            mapping = setup.load_json(vault / ".kb/mappings/feishu_nodes.json")
            self.assertEqual(mapping["space_id"], "456")
            self.assertEqual(mapping["space_name"], "企业知识库")
            self.assertEqual(mapping["nodes"], [])
            template_preview = setup.preview_wiki_template(vault, adapter)
            self.assertEqual(template_preview["confirmation"], "确认初始化知识库模板")
            self.assertEqual(template_preview["remote_writes"], 0)
            self.assertEqual(template_preview["preview"]["node_count"], 7)
            templated = setup.apply_wiki_template(
                vault, "确认初始化知识库模板", adapter
            )
            self.assertEqual(templated["remote_writes"], 7)
            self.assertEqual(templated["content_writes"], 0)
            self.assertEqual(templated["membership_writes"], 0)
            self.assertEqual(templated["next_confirmation"], "确认更新知识空间成员")
            mapping = setup.load_json(vault / ".kb/mappings/feishu_nodes.json")
            self.assertEqual(len(mapping["nodes"]), 7)
            self.assertTrue(all(node["verified"] for node in mapping["nodes"]))
            self.assertEqual(
                adapter.node_create_calls[5]["parent_node_token"], "node-1"
            )
            repeated = setup.apply_space_creation(
                vault, "确认创建知识空间", adapter
            )
            self.assertEqual(repeated["status"], "already_verified")
            self.assertEqual(repeated["remote_writes"], 0)
            self.assertEqual(adapter.create_calls, 1)
            repeated_template = setup.apply_wiki_template(
                vault, "确认初始化知识库模板", adapter
            )
            self.assertEqual(repeated_template["status"], "already_verified")
            self.assertEqual(repeated_template["remote_writes"], 0)
            self.assertEqual(len(adapter.node_create_calls), 7)

    def test_space_create_requires_exact_confirmation_and_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            refused = setup.apply_space_creation(vault, "可以创建", adapter)
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["remote_writes"], 0)
            self.assertEqual(adapter.create_calls, 0)
            adapter.identity = "b" * 64
            with self.assertRaises(setup.SetupError):
                setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            self.assertEqual(adapter.create_calls, 0)

    def test_fixed_wiki_template_has_only_portable_structure(self) -> None:
        template = setup._load_wiki_template()
        self.assertEqual(template["template_id"], "obsidian-enterprise-knowledge-base")
        self.assertEqual(
            [node["title"] for node in template["nodes"]],
            [
                "00｜知识库首页",
                "01｜业务与产品",
                "02｜客户与增长",
                "03｜流程与交付",
                "04｜经营与合规",
                "91｜数据索引",
                "98｜同步记录",
            ],
        )
        encoded = json.dumps(template, ensure_ascii=False)
        for forbidden in ("space_id", "node_token", "obj_token", "飞书同步测试"):
            self.assertNotIn(forbidden, encoded)

    def test_template_requires_separate_exact_confirmation_and_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            setup.preview_wiki_template(vault, adapter)
            refused = setup.apply_wiki_template(vault, "可以初始化", adapter)
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["remote_writes"], 0)
            self.assertEqual(adapter.node_create_calls, [])
            adapter.identity = "b" * 64
            with self.assertRaises(setup.SetupError):
                setup.apply_wiki_template(vault, "确认初始化知识库模板", adapter)
            self.assertEqual(adapter.node_create_calls, [])

    def test_template_preview_rejects_nonempty_or_connected_space(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vault = root / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            adapter.nodes.append(
                {
                    "space_id": "456",
                    "node_token": "manual-node",
                    "obj_token": "manual-doc",
                    "obj_type": "docx",
                    "title": "人工节点",
                    "parent_node_token": "",
                    "has_child": False,
                }
            )
            with self.assertRaises(setup.SetupError):
                setup.preview_wiki_template(vault, adapter)

            connected = root / "connected"
            setup.initialize(connected, "admin", "Example")
            setup.atomic_write_json(
                connected / ".kb/mappings/feishu_nodes.json",
                {"version": 2, "space_name": "Existing", "space_id": "123", "nodes": []},
            )
            with self.assertRaises(setup.SetupError):
                setup.preview_wiki_template(connected, adapter)

    def test_template_recovers_unique_unknown_write_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            setup.preview_wiki_template(vault, adapter)
            adapter.node_error_after_write_at = 3
            with self.assertRaises(setup.SetupError):
                setup.apply_wiki_template(vault, "确认初始化知识库模板", adapter)
            receipt = setup.load_json(vault / ".kb/state/wiki-template-preview.json")
            self.assertEqual(receipt["status"], "outcome_unknown")
            recovered = setup.apply_wiki_template(
                vault, "确认初始化知识库模板", adapter
            )
            self.assertTrue(recovered["recovered"])
            self.assertEqual(len(adapter.node_create_calls), 7)
            self.assertEqual(len(adapter.nodes), 7)

    def test_template_unknown_no_write_fails_closed_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            setup.preview_wiki_template(vault, adapter)
            adapter.node_error_before_write_at = 2
            with self.assertRaises(setup.SetupError):
                setup.apply_wiki_template(vault, "确认初始化知识库模板", adapter)
            with self.assertRaises(setup.SetupError):
                setup.apply_wiki_template(vault, "确认初始化知识库模板", adapter)
            self.assertEqual(len(adapter.node_create_calls), 2)
            self.assertEqual(len(adapter.nodes), 1)

    def test_template_final_readback_recovers_without_more_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            setup.preview_wiki_template(vault, adapter)
            adapter.tree_failure_at = 3
            with self.assertRaises(setup.SetupError):
                setup.apply_wiki_template(vault, "确认初始化知识库模板", adapter)
            self.assertEqual(len(adapter.node_create_calls), 7)
            recovered = setup.apply_wiki_template(
                vault, "确认初始化知识库模板", adapter
            )
            self.assertEqual(recovered["remote_writes"], 0)
            self.assertEqual(len(adapter.node_create_calls), 7)

    def test_new_space_membership_waits_for_template_but_connected_space_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vault = root / "new"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            with self.assertRaises(setup.SetupError):
                setup.preview_company_membership(vault, object())

            connected = root / "connected"
            setup.initialize(connected, "admin", "Example")
            setup.atomic_write_json(
                connected / ".kb/mappings/feishu_nodes.json",
                {"version": 2, "space_name": "Existing", "space_id": "123", "nodes": []},
            )
            class PreviewAdapter:
                def current_user_principal_hash(self) -> str:
                    return "a" * 64
                def prepare_membership_plan(self, previous: list[dict]) -> dict:
                    return membership.build_membership_plan(
                        "123",
                        membership.resolve_share_scope(
                            "all-employees",
                            [{
                                "kind": "organization-root",
                                "selector_id": "root-1",
                                "display_name": "全公司内部员工",
                                "verified": True,
                                "internal": True,
                            }],
                        ),
                        "members",
                    )
            preview = setup.preview_company_membership(connected, PreviewAdapter())
            self.assertTrue(preview["ok"])

    def test_space_create_rejects_existing_exact_name_and_configured_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            adapter.spaces.append(
                {
                    "space_id": "123",
                    "name": "企业知识库",
                    "description": "existing",
                    "space_type": "team",
                    "visibility": "private",
                    "open_sharing": "closed",
                }
            )
            with self.assertRaises(setup.SetupError):
                setup.preview_space_creation(vault, "企业知识库", "", adapter)
            self.assertEqual(adapter.create_calls, 0)
            setup.atomic_write_json(
                vault / ".kb/mappings/feishu_nodes.json",
                {"version": 2, "space_name": "Existing", "space_id": "123", "nodes": []},
            )
            with self.assertRaises(setup.SetupError):
                setup.preview_space_creation(vault, "另一个知识库", "", adapter)

    def test_space_preview_allows_other_public_spaces_but_preserves_unresolved_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            adapter.spaces.append(
                {
                    "space_id": "123",
                    "name": "公开参考库",
                    "description": "unrelated",
                    "space_type": "team",
                    "visibility": "public",
                    "open_sharing": "open",
                }
            )
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            receipt_path = vault / ".kb/state/space-create-preview.json"
            receipt = setup.load_json(receipt_path)
            receipt["status"] = "outcome_unknown"
            setup.atomic_write_json(receipt_path, receipt)
            with self.assertRaises(setup.SetupError):
                setup.preview_space_creation(vault, "另一个知识库", "", adapter)

    def test_space_create_recovers_readback_without_duplicate_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            adapter.verify_failures = 1
            with self.assertRaises(setup.SetupError):
                setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            receipt = setup.load_json(vault / ".kb/state/space-create-preview.json")
            self.assertEqual(receipt["status"], "created_unverified")
            setup.atomic_write_json(
                vault / ".kb/mappings/feishu_nodes.json",
                {
                    "version": 2,
                    "space_name": "企业知识库",
                    "space_id": "456",
                    "nodes": [],
                },
            )
            recovered = setup.apply_space_creation(
                vault, "确认创建知识空间", adapter
            )
            self.assertTrue(recovered["recovered"])
            self.assertEqual(recovered["remote_writes"], 0)
            self.assertEqual(adapter.create_calls, 1)

    def test_unknown_space_create_outcome_fails_closed_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            adapter = FakeSpaceSetupAdapter()
            setup.preview_space_creation(vault, "企业知识库", "", adapter)
            adapter.create_error = True
            with self.assertRaises(setup.SetupError):
                setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            receipt = setup.load_json(vault / ".kb/state/space-create-preview.json")
            self.assertEqual(receipt["status"], "outcome_unknown")
            with self.assertRaises(setup.SetupError):
                setup.apply_space_creation(vault, "确认创建知识空间", adapter)
            self.assertEqual(adapter.create_calls, 1)

    def test_employee_import_rejects_any_secret(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vault = root / "vault"
            setup.initialize(vault, "employee", "")
            company = root / "company.json"
            company.write_text(
                json.dumps(
                    {
                        "schema": setup.COMPANY_SCHEMA,
                        "company_name": "Example",
                        "feishu_brand": "feishu",
                        "app_id": "cli_abc123",
                        "app_secret": "must-not-travel",
                        "space_name": "Knowledge",
                        "space_id": "123",
                        "nodes": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(setup.SetupError):
                setup.import_company(vault, company)

    def test_company_package_rejects_full_employee_roster(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, package = self.configure_admin(root)
            company = setup.load_json(package)
            company["members"] = [{"name": "不应进入配置包"}]
            with self.assertRaises(setup.SetupError):
                setup.validate_company(company)

    def test_admin_export_is_sanitized_and_employee_imports_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            employee = root / "employee"
            _, package = self.configure_admin(root)
            exported = setup.load_json(package)
            self.assertNotIn("app_secret", exported)
            self.assertNotIn("members", exported["membership_verification"])
            self.assertEqual(exported["minimum_plugin_version"], "0.7.1")
            setup.initialize(employee, "employee", "")
            result = setup.import_company(employee, package)
            self.assertTrue(result["imported"])
            self.assertTrue(result["requires_employee_oauth_and_membership_verification"])

    def test_employee_verification_still_checks_identity_and_membership(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, package = self.configure_admin(root)
            employee = root / "employee"
            setup.initialize(employee, "employee", "")
            setup.import_company(employee, package)
            company = setup.load_json(package)
            verification = company["membership_verification"]
            evidence = {
                "schema": membership.EMPLOYEE_EVIDENCE_SCHEMA,
                "oauth_ok": True,
                "external_account": False,
                "tenant_key_hash": company["tenant_key_hash"],
                "space_visible": True,
                "space_member": True,
                "space_id": company["space_id"],
                "inside_authorized_scope": True,
                "space_role": "member",
                "membership_version": verification["remote_version"],
                "membership_hash": verification["member_list_hash"],
            }
            evidence_file = root / "employee-evidence.json"
            setup.atomic_write_json(evidence_file, evidence)
            result = setup.verify_employee(employee, evidence_file)
            self.assertTrue(result["membership_verified"])
            organization = setup.load_json(employee / ".kb/config/organization.json")
            self.assertTrue(organization["employee_access_verified"])

    def test_employee_cannot_record_membership_or_export_policy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            employee = root / "employee"
            setup.initialize(employee, "employee", "")
            with self.assertRaises(setup.SetupError):
                setup.record_membership_verification(
                    employee, root / "scope.json", root / "readback.json", "members"
                )
            with self.assertRaises(setup.SetupError):
                setup.export_company(employee, root / "company.json")

    def test_lark_cli_remains_separately_confirmed(self) -> None:
        with patch.object(setup.shutil, "which", return_value=None):
            with self.assertRaises(setup.SetupError):
                setup.install_lark_cli(False)


if __name__ == "__main__":
    unittest.main()

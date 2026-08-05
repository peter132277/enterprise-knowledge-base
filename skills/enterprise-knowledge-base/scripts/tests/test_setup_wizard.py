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
            "external_sharing": False,
            "bindings": [
                {"selector_id": "root-1", "role": "member", "internal": True}
            ],
            "members": [
                {
                    "principal_hash": "c" * 64,
                    "role": "member",
                    "internal": True,
                    "employee": True,
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
            self.assertEqual(exported["minimum_plugin_version"], "0.5.0")
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

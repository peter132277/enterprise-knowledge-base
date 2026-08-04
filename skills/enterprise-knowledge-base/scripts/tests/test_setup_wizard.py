import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import setup_wizard as setup
import membership_policy as membership


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
        setup.record_membership_verification(
            admin, scope_file, readback_file, "members"
        )
        package = root / "company.json"
        setup.export_company(admin, package)
        return admin, package

    def test_initialize_creates_same_portable_vault_layout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "company-vault"
            result = setup.initialize(vault, "local", "Example")
            self.assertTrue(result["configured"])
            self.assertTrue((vault / "AGENTS.md").is_file())
            for relative in (*setup.VISIBLE_DIRECTORIES, *setup.SYSTEM_DIRECTORIES):
                self.assertTrue((vault / relative).is_dir(), relative)

    def test_default_company_sharing_and_employee_policy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for system in ("Windows", "Darwin"):
                vault = Path(folder) / system
                with patch.object(setup.platform, "system", return_value=system):
                    setup.initialize(vault, "admin", "Example")
                organization = setup.load_json(vault / ".kb/config/organization.json")
                self.assertEqual(organization["share_scope"]["type"], "all-employees")
                self.assertEqual(organization["share_scope"]["resolution_status"], "pending")
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
            self.assertEqual(exported["schema"], "kb-company-config/v3")
            self.assertEqual(exported["minimum_plugin_version"], setup.PLUGIN_VERSION)
            self.assertEqual(exported["effective_employee_policy"], "members")
            setup.initialize(employee, "employee", "")
            result = setup.import_company(employee, package)
            self.assertTrue(result["imported"])
            self.assertFalse(result["membership_verified"])
            self.assertTrue(result["requires_employee_oauth_and_membership_verification"])

    def test_employee_import_still_requires_identity_and_membership_verification(self) -> None:
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

    def test_employee_cannot_record_membership_or_export_company_policy(self) -> None:
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

    def test_export_fails_until_membership_readback_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "admin"
            setup.initialize(vault, "admin", "Example")
            with self.assertRaises(setup.SetupError):
                setup.export_company(vault, Path(folder) / "company.json")

    def test_windows_obsidian_uses_winget_after_confirmation(self) -> None:
        which = lambda name: "winget" if name == "winget" else None
        with patch.object(setup.platform, "system", return_value="Windows"), patch.object(
            setup.shutil, "which", side_effect=which
        ), patch.object(
            setup.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            result = setup.install_obsidian(True)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0][0], "winget")

    def test_macos_obsidian_uses_existing_homebrew_after_confirmation(self) -> None:
        which = lambda name: "brew" if name == "brew" else None
        with patch.object(setup.platform, "system", return_value="Darwin"), patch.object(
            setup.shutil, "which", side_effect=which
        ), patch.object(
            setup.Path, "home", return_value=Path("/nonexistent-user-home")
        ), patch.object(
            setup.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            result = setup.install_obsidian(True)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], ["brew", "install", "--cask", "obsidian"])

    def test_installers_never_run_without_confirmation(self) -> None:
        with patch.object(setup.shutil, "which", return_value=None):
            with self.assertRaises(setup.SetupError):
                setup.install_obsidian(False)
            with self.assertRaises(setup.SetupError):
                setup.install_lark_cli(False)


if __name__ == "__main__":
    unittest.main()

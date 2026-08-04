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
            binding = setup.load_json(vault / ".kb/config/project-binding.json")
            self.assertEqual(binding["binding_mode"], "same-root")
            self.assertEqual(binding["project_root"], setup.canonical_path(vault))
            self.assertEqual(binding["vault_root"], setup.canonical_path(vault))
            self.assertEqual(binding["skill_name"], "enterprise-knowledge-base")
            self.assertEqual(binding["skill_scope"], "project-only")
            for relative in (*setup.VISIBLE_DIRECTORIES, *setup.SYSTEM_DIRECTORIES):
                self.assertTrue((vault / relative).is_dir(), relative)

    def test_install_plan_uses_documents_knowledge_base_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(
            setup, "documents_directory", return_value=Path(folder)
        ), patch.object(setup, "tool_status", return_value={"obsidian": False}):
            target = Path(folder) / "知识库"
            result = setup.install_plan()
            self.assertEqual(Path(result["vault"]), target.resolve())
            self.assertEqual(result["codex_project_name"], "知识库")
            self.assertEqual(result["path_status"], "create")
            self.assertFalse(target.exists())

    def test_install_plan_fails_closed_for_unrelated_nonempty_default(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(
            setup, "documents_directory", return_value=Path(folder)
        ), patch.object(setup, "tool_status", return_value={}):
            target = Path(folder) / "知识库"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            result = setup.install_plan()
            self.assertFalse(result["ok"])
            self.assertEqual(result["path_status"], "conflict")
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_legacy_three_skill_vault_is_migratable_and_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "知识库"
            (target / ".kb/config").mkdir(parents=True)
            (target / "AGENTS.md").write_text("# legacy rules\n", encoding="utf-8")
            legacy_organization = {"schema": "legacy", "role": "admin"}
            setup.atomic_write_json(
                target / ".kb/config/organization.json", legacy_organization
            )
            for name in setup.LEGACY_SKILL_NAMES:
                skill = target / ".agents/skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            plan = setup.install_plan(target)
            self.assertTrue(plan["ok"])
            self.assertEqual(plan["path_status"], "legacy")
            setup.initialize(target, "local", "")
            self.assertEqual(setup.install_plan(target)["path_status"], "legacy")
            self.assertEqual(
                setup.load_json(target / ".kb/config/organization.json"),
                legacy_organization,
            )
            migration = setup.retire_legacy_skills(target)
            self.assertTrue(migration["migrated"])
            backup = target / migration["backup"]
            self.assertEqual(
                (backup / "AGENTS.md").read_text(encoding="utf-8"),
                "# legacy rules\n",
            )
            for name in setup.LEGACY_SKILL_NAMES:
                self.assertFalse((target / ".agents/skills" / name).exists())
                self.assertTrue((backup / name / "SKILL.md").is_file())
            self.assertEqual(
                (target / "AGENTS.md").read_bytes(),
                setup.template_agents().read_bytes(),
            )

    def test_macos_default_path_is_documents_knowledge_base(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(
            setup.platform, "system", return_value="Darwin"
        ), patch.object(setup.Path, "home", return_value=Path(folder)):
            self.assertEqual(
                setup.default_vault_path(),
                (Path(folder) / "Documents/知识库").resolve(),
            )

    def test_bootstrap_local_automates_vault_claudian_and_obsidian_open(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "知识库"
            codex = root / "codex.exe"
            codex.write_bytes(b"codex")
            assets = {
                "main.js": b"main",
                "manifest.json": json.dumps(
                    {
                        "id": setup.CLAUDIAN_PLUGIN_ID,
                        "name": "Claudian",
                        "version": setup.CLAUDIAN_VERSION,
                    }
                ).encode("utf-8"),
                "styles.css": b"styles",
            }
            with patch.object(
                setup, "install_obsidian", return_value={"ok": True, "installed": True}
            ), patch.object(
                setup, "download_claudian_asset", side_effect=lambda name: assets[name]
            ), patch.object(
                setup,
                "sha256_file",
                side_effect=lambda path: setup.CLAUDIAN_ASSETS[path.name],
            ), patch.object(
                setup.platform, "system", return_value="Windows"
            ), patch.object(
                setup.platform, "node", return_value="workstation"
            ), patch.object(
                setup.webbrowser, "open", return_value=True
            ), patch.object(
                setup, "current_project_matches", return_value=False
            ):
                result = setup.bootstrap_local(
                    target, "local", "", True, str(codex)
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["stage"], "open-codex-project")
            self.assertTrue(result["project_specific_skill"])
            self.assertTrue(result["requires_codex_project_open"])
            self.assertIn(str(target.resolve()), result["next_user_action"])
            self.assertTrue((target / "AGENTS.md").is_file())

    def test_bootstrap_local_confirmation_gate_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "知识库"
            with self.assertRaises(setup.SetupError):
                setup.bootstrap_local(target, "local", "", False)
            self.assertFalse(target.exists())

    def test_bootstrap_stops_before_vault_write_when_obsidian_needs_manual_installer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "知识库"
            with patch.object(
                setup,
                "install_obsidian",
                return_value={
                    "ok": False,
                    "requires_browser": True,
                    "url": "https://obsidian.md/download.html",
                },
            ):
                result = setup.bootstrap_local(target, "local", "", True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["writes"], 0)
            self.assertFalse(target.exists())

    def test_bootstrap_reuses_configured_vault_without_overwriting_role(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "知识库"
            setup.initialize(target, "admin", "Example")
            with patch.object(
                setup, "install_obsidian", return_value={"ok": True}
            ), patch.object(
                setup,
                "install_claudian",
                return_value={"enabled": True, "already_installed": True},
            ), patch.object(
                setup, "open_obsidian", return_value={"ok": True}
            ), patch.object(
                setup,
                "inspect",
                return_value={
                    "project_binding": {"project_specific": True},
                    "claudian": {"ready": True},
                    "role_options": [],
                },
            ), patch.object(setup, "current_project_matches", return_value=True):
                result = setup.bootstrap_local(target, "local", "", True)
            self.assertTrue(result["ok"])
            organization = setup.load_json(target / ".kb/config/organization.json")
            self.assertEqual(organization["role"], "admin")
            self.assertEqual(organization["company_name"], "Example")

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

    def test_claudian_is_installed_enabled_and_bound_to_codex_cli(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            codex = Path(folder) / "codex.exe"
            codex.write_bytes(b"codex")
            setup.initialize(vault, "local", "Example")
            assets = {
                "main.js": b"main",
                "manifest.json": json.dumps(
                    {
                        "id": setup.CLAUDIAN_PLUGIN_ID,
                        "name": "Claudian",
                        "version": setup.CLAUDIAN_VERSION,
                    }
                ).encode("utf-8"),
                "styles.css": b"styles",
            }
            with patch.object(
                setup, "download_claudian_asset", side_effect=lambda name: assets[name]
            ), patch.object(
                setup,
                "sha256_file",
                side_effect=lambda path: setup.CLAUDIAN_ASSETS[path.name],
            ), patch.object(
                setup.platform, "system", return_value="Windows"
            ), patch.object(
                setup.platform, "node", return_value="workstation"
            ):
                result = setup.install_claudian(vault, True, str(codex))
            self.assertTrue(result["installed"])
            plugin = vault / f".obsidian/plugins/{setup.CLAUDIAN_PLUGIN_ID}"
            self.assertTrue((plugin / "main.js").is_file())
            self.assertIn(
                setup.CLAUDIAN_PLUGIN_ID,
                setup.load_json(vault / ".obsidian/community-plugins.json"),
            )
            settings = setup.load_json(vault / ".claudian/claudian-settings.json")
            codex_settings = settings["providerConfigs"]["codex"]
            self.assertTrue(codex_settings["enabled"])
            self.assertEqual(
                codex_settings["cliPathsByHost"]["workstation"], str(codex.resolve())
            )
            integration = setup.load_json(
                vault / ".kb/config/obsidian-integration.json"
            )
            self.assertEqual(integration["project_root"], setup.canonical_path(vault))
            self.assertEqual(integration["vault_root"], setup.canonical_path(vault))

    def test_existing_claudian_is_reused_without_download_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            codex = Path(folder) / "codex.exe"
            codex.write_bytes(b"codex")
            setup.initialize(vault, "local", "Example")
            plugin = vault / f".obsidian/plugins/{setup.CLAUDIAN_PLUGIN_ID}"
            plugin.mkdir(parents=True)
            original = {
                "main.js": b"existing-main",
                "manifest.json": json.dumps(
                    {
                        "id": setup.CLAUDIAN_PLUGIN_ID,
                        "name": "Claudian",
                        "version": setup.CLAUDIAN_VERSION,
                    }
                ).encode("utf-8"),
                "styles.css": b"existing-styles",
            }
            for name, data in original.items():
                (plugin / name).write_bytes(data)
            existing_settings = {
                "unrelatedSetting": {"preserve": True},
                "providerConfigs": {"codex": {"safeMode": "read-only"}},
            }
            setup.atomic_write_json(
                vault / ".claudian/claudian-settings.json", existing_settings
            )
            with patch.object(
                setup, "download_claudian_asset"
            ) as download, patch.object(
                setup.platform, "system", return_value="Windows"
            ), patch.object(
                setup.platform, "node", return_value="configured-workstation"
            ):
                result = setup.install_claudian(vault, True, str(codex))
            download.assert_not_called()
            self.assertTrue(result["already_installed"])
            self.assertFalse(result["installed"])
            for name, data in original.items():
                self.assertEqual((plugin / name).read_bytes(), data)
            settings = setup.load_json(vault / ".claudian/claudian-settings.json")
            self.assertEqual(settings["unrelatedSetting"], {"preserve": True})
            self.assertEqual(
                settings["providerConfigs"]["codex"]["cliPathsByHost"][
                    "configured-workstation"
                ],
                str(codex.resolve()),
            )

    def test_inspect_reports_existing_claudian_ready_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            codex = Path(folder) / "codex"
            codex.write_bytes(b"codex")
            setup.initialize(vault, "local", "Example")
            plugin = vault / f".obsidian/plugins/{setup.CLAUDIAN_PLUGIN_ID}"
            plugin.mkdir(parents=True)
            (plugin / "main.js").write_bytes(b"existing-main")
            (plugin / "styles.css").write_bytes(b"existing-styles")
            setup.atomic_write_json(
                plugin / "manifest.json",
                {
                    "id": setup.CLAUDIAN_PLUGIN_ID,
                    "name": "Claudian",
                    "version": setup.CLAUDIAN_VERSION,
                },
            )
            setup.atomic_write_json(
                vault / ".obsidian/community-plugins.json",
                [setup.CLAUDIAN_PLUGIN_ID],
            )
            setup.atomic_write_json(
                vault / ".claudian/claudian-settings.json",
                {
                    "providerConfigs": {
                        "codex": {
                            "enabled": True,
                            "cliPathsByHost": {"existing-device": str(codex.resolve())},
                        }
                    }
                },
            )
            before = {
                path.relative_to(vault).as_posix(): path.read_bytes()
                for path in vault.rglob("*")
                if path.is_file()
            }
            result = setup.inspect(vault)
            after = {
                path.relative_to(vault).as_posix(): path.read_bytes()
                for path in vault.rglob("*")
                if path.is_file()
            }
            self.assertTrue(result["claudian"]["ready"])
            self.assertEqual(result["claudian"]["codex_cli_path"], str(codex.resolve()))
            self.assertEqual(before, after)

    def test_macos_claudian_config_uses_verified_codex_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            codex = Path(folder) / "codex"
            codex.write_bytes(b"codex")
            setup.initialize(vault, "local", "Example")
            plugin = vault / f".obsidian/plugins/{setup.CLAUDIAN_PLUGIN_ID}"
            plugin.mkdir(parents=True)
            for name, data in {
                "main.js": b"existing-main",
                "manifest.json": json.dumps(
                    {
                        "id": setup.CLAUDIAN_PLUGIN_ID,
                        "name": "Claudian",
                        "version": setup.CLAUDIAN_VERSION,
                    }
                ).encode("utf-8"),
                "styles.css": b"existing-styles",
            }.items():
                (plugin / name).write_bytes(data)
            with patch.object(
                setup.platform, "system", return_value="Darwin"
            ), patch.object(
                setup.platform, "node", return_value="mac-workstation"
            ), patch.object(setup, "download_claudian_asset") as download:
                result = setup.install_claudian(vault, True, str(codex))
            download.assert_not_called()
            self.assertTrue(result["already_installed"])
            settings = setup.load_json(vault / ".claudian/claudian-settings.json")
            codex_settings = settings["providerConfigs"]["codex"]
            self.assertEqual(
                codex_settings["cliPathsByHost"]["mac-workstation"],
                str(codex.resolve()),
            )
            self.assertNotIn("installationMethodsByHost", codex_settings)

    def test_project_binding_rejects_a_different_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            other = Path(folder) / "other"
            setup.initialize(vault, "local", "Example")
            with self.assertRaises(setup.SetupError):
                setup.bind_project(vault, other)

    def test_installers_never_run_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.object(
            setup.shutil, "which", return_value=None
        ):
            vault = Path(folder) / "vault"
            setup.initialize(vault, "local", "Example")
            with self.assertRaises(setup.SetupError):
                setup.install_obsidian(False)
            with self.assertRaises(setup.SetupError):
                setup.install_claudian(vault, False)
            with self.assertRaises(setup.SetupError):
                setup.install_lark_cli(False)


if __name__ == "__main__":
    unittest.main()

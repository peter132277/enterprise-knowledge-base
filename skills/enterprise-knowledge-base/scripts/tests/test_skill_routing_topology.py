import json
import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = SKILL_ROOT.parents[1]


def read(relative: str) -> str:
    return (SKILL_ROOT / relative).read_text(encoding="utf-8")


class SkillRoutingTopologyTests(unittest.TestCase):
    def test_plugin_exposes_exactly_one_skill(self) -> None:
        skills = list((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(skills, [SKILL_ROOT / "SKILL.md"])
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["interface"]["displayName"], "企业知识库")
        self.assertEqual(manifest["version"], "0.4.1")

    def test_single_entry_is_implicitly_invocable(self) -> None:
        yaml = read("agents/openai.yaml")
        self.assertRegex(yaml, r"allow_implicit_invocation:\s*true")
        self.assertIn("$enterprise-knowledge-base", yaml)

    def test_routes_remain_separate_inside_one_skill(self) -> None:
        router = read("SKILL.md")
        for fragment in (
            "query-workflow.md",
            "capture-modes.md",
            "media-transcription.md",
            "execute_publish.py",
            "Never contact Feishu from the collection route",
            "The transcription upload confirmation and later Wiki publication confirmation are independent",
        ):
            self.assertIn(fragment, router)

    def test_admin_label_is_exact_and_has_no_recommendation_suffix(self) -> None:
        setup = read("references/setup-workflow.md")
        self.assertIn("`为本公司创建企业自建应用`", setup)
        self.assertNotIn("试点推荐", setup + read("SKILL.md"))

    def test_windows_and_macos_setup_are_explicit(self) -> None:
        setup = read("references/setup-workflow.md")
        script = read("scripts/setup_wizard.py")
        for fragment in ("Windows", "macOS", "Winget", "Homebrew"):
            self.assertIn(fragment, setup)
        self.assertIn('system == "Windows"', script)
        self.assertIn('system == "Darwin"', script)
        self.assertIn("Obsidian.Obsidian", script)
        self.assertIn('"brew", "install", "--cask", "obsidian"', script)

    def test_membership_and_skill_policy_are_distinct_fail_closed_layers(self) -> None:
        router = read("SKILL.md")
        setup = read("references/setup-workflow.md")
        adapter = read("scripts/membership_policy.py")
        for fragment in (
            "Application installation, company configuration import, and employee OAuth never grant knowledge-space access",
            "Keep Feishu knowledge-space membership separate",
            "全公司内部员工",
            "查询、收录并发布",
            "确认更新知识空间成员",
            "kb-company-config/v3",
        ):
            self.assertIn(fragment, router + setup)
        self.assertNotIn("subprocess", adapter)
        self.assertNotIn("lark-cli", adapter)
        self.assertNotIn("requests", adapter)

    def test_no_project_local_three_skill_paths_remain_in_distribution(self) -> None:
        texts = []
        for folder in (SKILL_ROOT / "references", SKILL_ROOT / "scripts"):
            for path in folder.rglob("*"):
                if path.is_file() and "tests" not in path.parts:
                    try:
                        texts.append(path.read_text(encoding="utf-8"))
                    except UnicodeDecodeError:
                        pass
        combined = "\n".join(texts)
        self.assertNotIn(".agents/skills/manage-knowledge-base", combined)
        self.assertNotIn(".agents/skills/collect-local-knowledge", combined)
        self.assertNotIn(".agents/skills/publish-enterprise-knowledge", combined)

    def test_media_and_publication_fail_closed_contracts_remain(self) -> None:
        combined = read("SKILL.md") + read("references/media-transcription.md")
        for fragment in (
            "Default: Feishu Minutes",
            "Offline option",
            "Never use Tencent Cloud",
            "Reuse the exact Drive original",
            "Only exact `确认批量发布` authorizes",
        ):
            self.assertIn(fragment, combined)

    def test_company_sync_triggers_and_personal_boundary_are_explicit(self) -> None:
        combined = "\n".join(
            read(path)
            for path in (
                "SKILL.md",
                "references/runtime-access.md",
                "references/query-workflow.md",
                "references/capture-modes.md",
                "references/media-transcription.md",
                "references/publish-execution.md",
            )
        )
        for fragment in (
            "initial-employee",
            "before-query",
            "company_sync_coordinator.py explicit",
            "publisher",
            "scope: personal",
            "publish_to_feishu: false",
            "No background push or polling",
        ):
            self.assertIn(fragment, combined)
        self.assertTrue((SKILL_ROOT / "scripts/company_sync_coordinator.py").is_file())
        self.assertTrue((SKILL_ROOT / "scripts/feishu_company_adapter.py").is_file())
        self.assertTrue((PLUGIN_ROOT / "INSTALL.md").is_file())
        self.assertTrue((PLUGIN_ROOT / "INSTALL_REQUIREMENTS.md").is_file())
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        requirements = (PLUGIN_ROOT / "INSTALL_REQUIREMENTS.md").read_text(
            encoding="utf-8"
        )
        prompt_match = re.search(r"```text\n([^`]+)\n```", readme)
        self.assertIsNotNone(prompt_match)
        prompt = prompt_match.group(1).strip()
        self.assertEqual(len(prompt.splitlines()), 1)
        self.assertIn("INSTALL_REQUIREMENTS.md", prompt)
        self.assertIn("由 Codex 自动创建", prompt)
        self.assertNotIn("要求：", prompt)
        self.assertIn("不得要求重启 Codex", requirements)
        self.assertIn(
            "立即读取新安装的 `enterprise-knowledge-base/SKILL.md`", requirements
        )
        self.assertIn("我是飞书管理员，首次为公司部署", requirements)
        self.assertIn("尚未获得单独授权的真实飞书写入保持为零", requirements)
        self.assertNotIn("codex plugin add", readme)
        self.assertNotIn("codex plugin add", requirements)
        self.assertNotIn("查询企业知识：", readme)
        self.assertNotIn("查询我的个人知识：", readme)
        self.assertIn("固定查询当前 Vault 全部内容，无范围选项", readme)
        self.assertNotIn("查看待发布的企业知识", readme)
        self.assertIn("| 发布预览 | `发布到飞书` |", readme)
        self.assertNotIn("批量发布到飞书", readme)
        self.assertIn("realclaudian", requirements)
        self.assertIn("Codex CLI", requirements)
        self.assertIn("不得把运行时访问重定向到其他项目", requirements)
        self.assertIn("searches all personal and enterprise knowledge", combined)
        self.assertIn("scope: all", combined)
        self.assertNotIn("--scope <personal|enterprise|all>", combined)
        setup = read("references/setup-workflow.md")
        self.assertIn("bootstrap-local", setup)
        self.assertIn("project-binding.json", setup)
        self.assertIn("Documents folder", setup)
        self.assertIn("知识库", setup)
        self.assertIn("bootstrap-local", setup)
        self.assertIn(
            "company-config-schema.md", setup
        )


if __name__ == "__main__":
    unittest.main()

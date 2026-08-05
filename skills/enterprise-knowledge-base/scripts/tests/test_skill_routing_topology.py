import json
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
        self.assertEqual(manifest["version"], "0.7.1")

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
            "Keep transcription upload confirmation separate from Wiki publication confirmation",
        ):
            self.assertIn(fragment, router)

    def test_admin_label_is_exact_and_has_no_recommendation_suffix(self) -> None:
        setup = read("references/setup-workflow.md")
        self.assertIn("`为本公司创建企业自建应用`", setup)
        self.assertNotIn("试点推荐", setup + read("SKILL.md"))

    def test_current_project_setup_has_no_obsidian_runtime_or_installer(self) -> None:
        setup = read("references/setup-workflow.md")
        script = read("scripts/setup_wizard.py")
        self.assertIn("inspect-current-project", setup)
        self.assertIn("initialize-current-project", setup)
        self.assertIn("The command accepts no external Vault path", setup)
        for fragment in (
            "install-obsidian",
            "install-claudian",
            "open-obsidian",
            "plan-install",
            "bootstrap-local",
            "Obsidian.Obsidian",
        ):
            self.assertNotIn(fragment, script)

    def test_membership_and_skill_policy_are_distinct_fail_closed_layers(self) -> None:
        router = read("SKILL.md")
        setup = read("references/setup-workflow.md")
        adapter = read("scripts/membership_policy.py")
        for fragment in (
            "Application setup and OAuth do not grant space membership",
            "Keep knowledge-space membership separate from Skill policy",
            "全公司内部员工",
            "查询、收录并发布",
            "确认更新知识空间成员",
            "kb-company-config/v4",
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
        self.assertFalse((PLUGIN_ROOT / "INSTALL.md").exists())
        self.assertFalse((PLUGIN_ROOT / "INSTALL_REQUIREMENTS.md").exists())
        self.assertTrue((PLUGIN_ROOT / "docs/manual-setup.md").is_file())
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("codex plugin add", readme)
        self.assertNotIn("查询企业知识：", readme)
        self.assertNotIn("查询我的个人知识：", readme)
        self.assertIn("不提供个人／企业查询模式", readme)
        self.assertIn("`发布到飞书`", readme)
        self.assertIn("across personal and enterprise knowledge", combined)
        self.assertIn("scope: all", combined)
        self.assertNotIn("--scope <personal|enterprise|all>", combined)
        setup = read("references/setup-workflow.md")
        self.assertIn("project-binding.json", setup)
        self.assertIn("current project", setup)
        self.assertNotIn("Documents/知识库", setup)
        self.assertIn("新建飞书知识库", setup)
        self.assertIn("连接已有飞书知识库", setup)
        self.assertIn("确认创建知识空间", setup)
        self.assertIn("确认初始化知识库模板", setup)
        self.assertIn("确认更新知识空间成员", setup)
        self.assertIn("wiki:space:retrieve", setup)
        self.assertIn("wiki:space:write_only", setup)
        self.assertIn("wiki:node:retrieve", setup)
        self.assertIn("wiki:node:create", setup)
        self.assertLess(
            setup.index("确认创建知识空间"),
            setup.index("确认初始化知识库模板"),
        )
        self.assertLess(
            setup.index("确认初始化知识库模板"),
            setup.index("确认更新知识空间成员"),
        )


if __name__ == "__main__":
    unittest.main()

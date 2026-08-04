import ast
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kb_core


PUBLIC_CLI = {
    "setup_wizard.py",
    "query_answer_packet.py",
    "ingest_pipeline.py",
    "prepare_media_transcript.py",
    "company_sync_coordinator.py",
    "execute_publish.py",
    "check_vault_health.py",
}


class LightweightContractTests(unittest.TestCase):
    def test_public_cli_surface_is_exactly_seven(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        declaration = next(
            line for line in skill.splitlines() if "only public script entries" in line
        )
        declared = {
            token.strip("`")
            for token in declaration.replace(",", "").replace(";", "").split()
            if token.strip("`").endswith(".py")
        }
        self.assertEqual(declared, PUBLIC_CLI)
        self.assertLessEqual(len(skill.splitlines()), 80)
        routed = skill + "\n" + "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL_ROOT / "references").glob("*.md")
        )
        invoked = set(re.findall(r"scripts/([a-z_]+\.py)", routed))
        self.assertTrue(invoked.issubset(PUBLIC_CLI), invoked - PUBLIC_CLI)

    def test_reference_and_production_file_budgets(self) -> None:
        for path in (SKILL_ROOT / "references").glob("*.md"):
            self.assertLessEqual(
                len(path.read_text(encoding="utf-8").splitlines()),
                120,
                path.name,
            )
        self.assertLessEqual(len(list(SCRIPTS.glob("*.py"))), 26)

    def test_query_import_does_not_load_heavy_routes(self) -> None:
        code = (
            "import json,sys; import query_answer_packet; "
            "print(json.dumps(sorted(sys.modules)))"
        )
        run = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            cwd=SCRIPTS,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        loaded = set(json.loads(run.stdout))
        for forbidden in (
            "setup_wizard",
            "feishu_company_adapter",
            "prepare_media_transcript",
            "execute_publish",
            "publish_batch",
        ):
            self.assertNotIn(forbidden, loaded)

    def test_local_module_graph_has_no_cycle(self) -> None:
        modules = {path.stem: path for path in SCRIPTS.glob("*.py")}
        graph: dict[str, set[str]] = {name: set() for name in modules}
        for name, path in modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                candidates: list[str] = []
                if isinstance(node, ast.Import):
                    candidates = [item.name.split(".")[0] for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    candidates = [node.module.split(".")[0]]
                graph[name].update(item for item in candidates if item in modules)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                self.fail(f"Circular import includes {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in graph:
            visit(name)

    def test_core_is_strict_atomic_and_dependency_free(self) -> None:
        tree = ast.parse((SCRIPTS / "kb_core.py").read_text(encoding="utf-8"))
        imports = {
            node.names[0].name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }
        self.assertEqual(imports, {"hashlib", "json", "os", "re", "tempfile"})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "state.json"
            kb_core.atomic_write_json(target, {"ok": True})
            self.assertEqual(kb_core.load_json(target), {"ok": True})
            target.write_text("{broken", encoding="utf-8")
            with self.assertRaises(kb_core.CoreError):
                kb_core.load_json(target)


if __name__ == "__main__":
    unittest.main()

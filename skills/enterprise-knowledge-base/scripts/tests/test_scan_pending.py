import importlib.util
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scan_pending.py"
SPEC = importlib.util.spec_from_file_location("scan_pending", SCRIPT)
SCAN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SCAN)


def note(frontmatter: str, body: str = "# 正文\n") -> str:
    return f"---\n{frontmatter.strip()}\n---\n\n{body}"


class ScanPendingTests(unittest.TestCase):
    def run_scan(self, vault: Path) -> dict:
        output = StringIO()
        with patch("sys.argv", ["scan_pending.py", "--vault", str(vault)]):
            with redirect_stdout(output):
                SCAN.main()
        import json

        return json.loads(output.getvalue())

    def test_scans_ready_blocked_and_published_notes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            root = vault / "20_知识/企业"
            root.mkdir(parents=True)
            base = """
title: {title}
status: ready-to-publish
scope: enterprise
review_status: reviewed
publish_status: pending
sensitivity: {sensitivity}
feishu_parent_node_name: 03｜流程与交付
"""
            (root / "ready.md").write_text(
                note(base.format(title="可发布", sensitivity="public")),
                encoding="utf-8",
            )
            (root / "restricted.md").write_text(
                note(base.format(title="受限", sensitivity="restricted")),
                encoding="utf-8",
            )
            (root / "published.md").write_text(
                note(
                    base.format(title="已发布", sensitivity="internal")
                    .replace("ready-to-publish", "published")
                ),
                encoding="utf-8",
            )
            result = self.run_scan(vault)
            self.assertEqual(result["ready_count"], 1)
            self.assertEqual(result["blocked_count"], 1)
            self.assertEqual(result["published_count"], 1)
            self.assertEqual(result["ready"][0]["title"], "可发布")
            self.assertEqual(result["blocked"][0]["reason"], "sensitivity")

    def test_invalid_utf8_is_blocked_without_stopping_other_items(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            root = vault / "20_知识/企业"
            root.mkdir(parents=True)
            (root / "broken.md").write_bytes(b"\xff\xfe\xfa")
            result = self.run_scan(vault)
            self.assertEqual(result["ready_count"], 0)
            self.assertEqual(result["blocked_count"], 1)
            self.assertEqual(result["blocked"][0]["reason"], "encoding")

    def test_missing_enterprise_root_returns_the_same_count_schema(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_scan(Path(folder))
            self.assertEqual(result["ready_count"], 0)
            self.assertEqual(result["blocked_count"], 0)
            self.assertEqual(result["published_count"], 0)
            self.assertEqual(result["ready"], [])
            self.assertEqual(result["blocked"], [])


if __name__ == "__main__":
    unittest.main()

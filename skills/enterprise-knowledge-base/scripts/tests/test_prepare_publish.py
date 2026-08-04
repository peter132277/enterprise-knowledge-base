import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_publish.py"


class PreparePublishTests(unittest.TestCase):
    def make_vault(self, root: Path) -> Path:
        (root / "20_知识/企业").mkdir(parents=True)
        mapping = root / ".kb/mappings"
        mapping.mkdir(parents=True)
        (mapping / "feishu_nodes.json").write_text(
            json.dumps(
                {
                    "space_name": "测试空间",
                    "space_id": "space",
                    "nodes": [
                        {
                            "node_name": "03｜流程与交付",
                            "node_token": "parent-token",
                            "verified": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        note = root / "20_知识/企业/test.md"
        note.write_text(
            """---
title: 测试标题
status: ready-to-publish
scope: enterprise
sensitivity: internal
review_status: reviewed
publish_status: pending
publish_to_feishu: false
feishu_writer: lark-cli
feishu_parent_node_name: 03｜流程与交付
---

# 测试标题

引用 [[内部笔记|显示文字]]。
""",
            encoding="utf-8",
        )
        return note

    def run_prepare(self, vault: Path, note: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(SCRIPT),
                "--allow-pending",
                "--vault",
                str(vault),
                "--note",
                str(note),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_prepares_deterministic_payload_without_duplicate_title(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            note = self.make_vault(vault)
            first = self.run_prepare(vault, note)
            second = self.run_prepare(vault, note)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            one = json.loads(first.stdout)
            two = json.loads(second.stdout)
            self.assertEqual(one["payload_hash"], two["payload_hash"])
            self.assertEqual(one["payload_path"], two["payload_path"])
            payload = Path(one["payload_path"]).read_text(encoding="utf-8")
            self.assertNotIn("# 测试标题", payload)
            self.assertIn("显示文字", payload)

    def test_blocks_secret_and_local_attachment_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            note = self.make_vault(vault)
            original = note.read_text(encoding="utf-8")
            note.write_text(original + "\npassword=abcdefghijk\n", encoding="utf-8")
            secret = self.run_prepare(vault, note)
            self.assertNotEqual(secret.returncode, 0)
            self.assertIn("疑似凭证", secret.stdout)

            note.write_text(original + "\n![[local.png]]\n", encoding="utf-8")
            attachment = self.run_prepare(vault, note)
            self.assertNotEqual(attachment.returncode, 0)
            self.assertIn("本地附件", attachment.stdout)

    def test_rejects_note_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder) / "vault"
            vault.mkdir()
            self.make_vault(vault)
            outside = Path(folder) / "outside.md"
            outside.write_text("# outside\n", encoding="utf-8")
            result = self.run_prepare(vault, outside)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Vault 之外", result.stdout)


if __name__ == "__main__":
    unittest.main()

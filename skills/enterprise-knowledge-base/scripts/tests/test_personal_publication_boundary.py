import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import execute_publish as execute
import prepare_publish
import publish_batch as batch
import publish_state
import scan_pending
import setup_wizard as setup
from company_test_support import configure_admin, configure_employee


def write_note(path: Path, *, scope: str, title: str, publish: str = "false") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                "type: source-note",
                'imported_at: "2026-08-05T10:00:00+08:00"',
                f"scope: {scope}",
                "status: ready-to-publish",
                "review_status: reviewed",
                "sensitivity: internal",
                "publish_status: pending",
                f"publish_to_feishu: {publish}",
                "feishu_writer: lark-cli",
                "feishu_parent_node_name: Enterprise",
                "---",
                "",
                f"# {title}",
                "",
                "Content.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def scan(vault: Path) -> dict:
    output = io.StringIO()
    with patch.object(sys, "argv", ["scan_pending.py", "--vault", str(vault)]):
        with contextlib.redirect_stdout(output):
            scan_pending.main()
    return json.loads(output.getvalue())


class PersonalPublicationBoundaryTests(unittest.TestCase):
    def test_admin_and_employee_personal_attachment_document_media_and_local_notes_never_queue(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            admin = configure_admin(root / "admin")
            employee = configure_employee(admin, root / "employee")
            for vault in (admin, employee):
                for label, source_file in (
                    ("attachment", "attachment.pdf"),
                    ("document", "document.docx"),
                    ("media", "meeting.mp3"),
                    ("local", "local.txt"),
                ):
                    note = vault / f"20_知识/个人/{label}.md"
                    write_note(note, scope="personal", title=label, publish="false")
                    with note.open("a", encoding="utf-8") as handle:
                        handle.write(f"\nsource_file: {source_file}\n")
                result = scan(vault)
                self.assertEqual(result["ready_count"], 0)
                self.assertTrue(batch.create_preview(vault)["empty"])

    def test_misplaced_personal_note_and_managed_mirror_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = configure_admin(Path(folder) / "admin")
            write_note(
                vault / "20_知识/企业/personal-misplaced.md",
                scope="personal",
                title="Personal",
                publish="true",
            )
            mirror = vault / "20_知识/企业/共享镜像/node.md"
            write_note(mirror, scope="enterprise", title="Mirror", publish="false")
            text = mirror.read_text(encoding="utf-8").replace(
                "type: source-note", "type: company-mirror\nmanaged_mirror: true\npublication_excluded: true"
            )
            mirror.write_text(text, encoding="utf-8")
            result = scan(vault)
            self.assertEqual(result["ready_count"], 0)
            self.assertEqual(result["published_count"], 0)

    def test_personal_note_fails_preview_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = configure_admin(Path(folder) / "admin")
            note = vault / "20_知识/个人/private.md"
            write_note(note, scope="personal", title="Private", publish="true")
            with patch.object(
                sys,
                "argv",
                ["prepare_publish.py", "--vault", str(vault), "--note", str(note)],
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        prepare_publish.main()

    def test_confirmation_rejects_scope_tamper_and_executor_makes_zero_remote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = configure_admin(Path(folder) / "admin")
            note = vault / "20_知识/企业/enterprise.md"
            write_note(note, scope="enterprise", title="Enterprise")
            preview = batch.create_preview(vault)
            manifest_path = vault / preview["_internal"]["manifest_relative"]
            manifest = setup.load_json(manifest_path)
            manifest["items"][0]["scope"] = "personal"
            setup.atomic_write_json(manifest_path, manifest)
            with self.assertRaises(batch.BatchError):
                batch.confirm_batch(vault, manifest_path, batch.EXACT_CONFIRMATION)

            manifest["items"][0]["scope"] = "enterprise"
            setup.atomic_write_json(manifest_path, manifest)
            batch.confirm_batch(vault, manifest_path, batch.EXACT_CONFIRMATION)
            manifest = setup.load_json(manifest_path)
            manifest["items"][0]["scope"] = "personal"
            setup.atomic_write_json(manifest_path, manifest)
            calls: list[list[str]] = []

            def no_remote(argv: list[str]):
                calls.append(argv)
                raise AssertionError("remote runner must not be called")

            with self.assertRaises(execute.ExecutionError):
                execute.load_confirmed_context(vault, manifest_path, no_remote)
            self.assertEqual(calls, [])

    def test_publication_state_flags_personal_queue_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = configure_admin(Path(folder) / "admin")
            note = vault / "20_知识/个人/private.md"
            write_note(note, scope="personal", title="Private")
            relative = note.relative_to(vault).as_posix()
            setup.atomic_write_json(
                vault / ".kb/state/publish_queue.json",
                {"version": 2, "items": [{"local_path": relative}]},
            )
            setup.atomic_write_json(
                vault / ".kb/mappings/feishu_documents.json",
                {"version": 2, "documents": [{"local_path": relative}]},
            )
            result = publish_state.audit(vault)
            reasons = {item["reason"] for item in result["errors"]}
            self.assertIn("queue_note_not_pending", reasons)
            self.assertIn("personal_note_must_not_be_mapped", reasons)


if __name__ == "__main__":
    unittest.main()

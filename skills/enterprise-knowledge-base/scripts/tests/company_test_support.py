from __future__ import annotations

from pathlib import Path
from typing import Any

import membership_policy as membership
import setup_wizard as setup


SPACE_ID = "123"
NODE_TOKEN = "wikcn123"


def configure_admin(vault: Path) -> Path:
    setup.initialize(vault, "admin", "Example")
    organization = setup.load_json(vault / ".kb/config/organization.json")
    organization.update(
        {
            "app_id": "cli_abc123",
            "feishu_brand": "feishu",
            "tenant_key_hash": "b" * 64,
        }
    )
    setup.atomic_write_json(vault / ".kb/config/organization.json", organization)
    setup.atomic_write_json(
        vault / ".kb/mappings/feishu_nodes.json",
        {
            "version": 2,
            "space_name": "Knowledge",
            "space_id": SPACE_ID,
            "nodes": [
                {"node_name": "Enterprise", "node_token": NODE_TOKEN, "verified": True}
            ],
        },
    )
    scope = membership.resolve_share_scope(
        "all-employees",
        [
            {
                "kind": "organization-root",
                "selector_id": "root-1",
                "display_name": "All internal employees",
                "verified": True,
                "internal": True,
            }
        ],
    )
    readback = {
        "schema": membership.MEMBERSHIP_READBACK_SCHEMA,
        "space_id": SPACE_ID,
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
    scope_file = vault.parent / f"{vault.name}-scope.json"
    readback_file = vault.parent / f"{vault.name}-readback.json"
    setup.atomic_write_json(scope_file, scope)
    setup.atomic_write_json(readback_file, readback)
    setup.record_membership_verification(vault, scope_file, readback_file, "members")
    return vault


def prepare_employee(admin: Path, employee: Path) -> Path:
    package = admin.parent / f"{employee.name}-company.json"
    setup.export_company(admin, package)
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
    evidence_file = admin.parent / f"{employee.name}-evidence.json"
    setup.atomic_write_json(evidence_file, evidence)
    return evidence_file


def configure_employee(admin: Path, employee: Path) -> Path:
    evidence_file = prepare_employee(admin, employee)
    setup.verify_employee(employee, evidence_file)
    return employee


class FakeKnowledgeReader:
    def __init__(self, nodes: list[dict[str, Any]] | None = None) -> None:
        self.nodes = nodes or [
            {
                "node_token": "node-1",
                "obj_token": "doc-1",
                "obj_type": "docx",
                "title": "Policy",
                "obj_edit_time": "1",
                "has_child": False,
            }
        ]
        self.list_calls = 0
        self.fetch_calls = 0

    def list_tree(self, space_id: str) -> list[dict[str, Any]]:
        if space_id != SPACE_ID:
            raise AssertionError(space_id)
        self.list_calls += 1
        return [dict(item) for item in self.nodes]

    def fetch_markdown(self, doc_token: str) -> dict[str, Any]:
        self.fetch_calls += 1
        return {
            "content": f"# {doc_token}\n\nCompany knowledge.",
            "revision_id": "1",
            "document_id": doc_token,
        }

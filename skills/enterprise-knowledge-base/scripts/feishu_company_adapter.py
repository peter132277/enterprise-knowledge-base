#!/usr/bin/env python3
"""Managed lark-cli adapters for company membership and read-only Wiki content."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Callable

from membership_policy import canonical_hash, validate_plan


Runner = Callable[[list[str]], dict[str, Any]]
INTERNAL_MEMBER_TYPES = {
    "openid",
    "userid",
    "unionid",
    "openchat",
    "opendepartmentid",
    "appid",
}
SELECTOR_MEMBER_TYPES = {
    "organization-root": "opendepartmentid",
    "department": "opendepartmentid",
    "group": "openchat",
    "user": "openid",
}


class CompanyAdapterError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        raise CompanyAdapterError("lark-cli returned no structured JSON.")
    return candidates[-1]


def run_lark_cli(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    payload = _extract_json(completed.stdout if completed.returncode == 0 else completed.stderr)
    if completed.returncode != 0 or payload.get("ok") is False:
        error = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
        message = str(error.get("message") or error.get("hint") or "lark-cli request failed")
        raise CompanyAdapterError(message)
    return payload


def response_data(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is True:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise CompanyAdapterError("lark-cli returned an invalid success envelope.")
        return data
    return payload


def principal_hash(member_id: str) -> str:
    return hashlib.sha256(member_id.encode("utf-8")).hexdigest()


class LarkMembershipAdapter:
    def __init__(
        self,
        space_id: str,
        runner: Runner = run_lark_cli,
        allowed_admin_principal_hashes: set[str] | None = None,
    ) -> None:
        self.space_id = str(space_id)
        self.runner = runner
        self.allowed_admin_principal_hashes = allowed_admin_principal_hashes or set()
        self.last_plan: dict[str, Any] | None = None

    def current_user_principal_hash(self) -> str:
        payload = self.runner(["lark-cli", "auth", "status", "--json", "--verify"])
        data = response_data(payload)
        identities = data.get("identities", payload.get("identities", {}))
        user = identities.get("user", {}) if isinstance(identities, dict) else {}
        open_id = str(user.get("openId") or user.get("open_id") or "").strip()
        identity = data.get("identity")
        if not open_id and isinstance(identity, dict):
            open_id = str(identity.get("openId") or identity.get("open_id") or "").strip()
        if user.get("status") not in {
            "authenticated",
            "valid",
            "ok",
            "active",
            "configured",
        } or not open_id:
            raise CompanyAdapterError("Verified administrator user identity is unavailable.")
        return principal_hash(open_id)

    def resolve_all_employees(self) -> dict[str, Any]:
        payload = self.runner(
            [
                "lark-cli",
                "api",
                "GET",
                "/open-apis/contact/v3/departments/0",
                "--as",
                "user",
                "--params",
                '{"department_id_type":"open_department_id","user_id_type":"open_id"}',
                "--format",
                "json",
            ]
        )
        data = response_data(payload)
        department = data.get("department", data)
        if not isinstance(department, dict):
            raise CompanyAdapterError("The organization root was not returned.")
        department_id = str(
            department.get("open_department_id")
            or department.get("department_id")
            or ""
        ).strip()
        name = str(department.get("name", "")).strip()
        parent = str(department.get("parent_department_id", "")).strip()
        if not department_id or not name or parent not in {"", "0"}:
            raise CompanyAdapterError("The organization root could not be resolved uniquely.")
        return {
            "kind": "organization-root",
            "selector_id": department_id,
            "display_name": name,
            "verified": True,
            "internal": True,
        }

    def _space(self) -> dict[str, Any]:
        data = response_data(
            self.runner(
                [
                    "lark-cli",
                    "wiki",
                    "spaces",
                    "get",
                    "--space-id",
                    self.space_id,
                    "--as",
                    "user",
                    "--format",
                    "json",
                ]
            )
        )
        space = data.get("space", data)
        if not isinstance(space, dict):
            raise CompanyAdapterError("Knowledge-space readback is invalid.")
        if (
            str(space.get("space_id", "")) != self.space_id
            or space.get("space_type") != "team"
            or space.get("visibility") != "private"
            or space.get("open_sharing") != "closed"
        ):
            raise CompanyAdapterError("Knowledge space is not a private internal team space.")
        return space

    def _members(self) -> list[dict[str, Any]]:
        data = response_data(
            self.runner(
                [
                    "lark-cli",
                    "wiki",
                    "+member-list",
                    "--space-id",
                    self.space_id,
                    "--page-all",
                    "--page-limit",
                    "0",
                    "--as",
                    "user",
                    "--format",
                    "json",
                ]
            )
        )
        if data.get("has_more") is True:
            raise CompanyAdapterError("Member-list pagination did not complete.")
        members = data.get("members", [])
        if not isinstance(members, list) or any(not isinstance(item, dict) for item in members):
            raise CompanyAdapterError("Member-list readback is invalid.")
        return members

    def apply_membership_plan(self, plan: dict[str, Any]) -> int:
        validate_plan(plan)
        if (
            plan["space_id"] != self.space_id
            or plan["share_scope"]["type"] != "all-employees"
            or plan["employee_policy"] != "members"
        ):
            raise CompanyAdapterError("Membership plan is not the company-wide full-access plan.")
        self._space()
        existing = self._members()
        self.last_plan = plan
        writes = 0
        for selector in plan["share_scope"]["selectors"]:
            member_type = SELECTOR_MEMBER_TYPES.get(str(selector.get("kind", "")))
            member_id = str(selector.get("selector_id", ""))
            matches = [
                item
                for item in existing
                if str(item.get("member_id", "")) == member_id
                and item.get("member_type") == member_type
            ]
            if len(matches) > 1 or (matches and matches[0].get("member_role") != "member"):
                raise CompanyAdapterError("Existing space grant conflicts with the member plan.")
            if matches:
                continue
            self.runner(
                [
                    "lark-cli",
                    "wiki",
                    "+member-add",
                    "--space-id",
                    self.space_id,
                    "--member-id",
                    member_id,
                    "--member-type",
                    str(member_type),
                    "--member-role",
                    "member",
                    "--as",
                    "user",
                    "--format",
                    "json",
                ]
            )
            writes += 1
        return writes

    def member_list(self, space_id: str) -> dict[str, Any]:
        if str(space_id) != self.space_id or self.last_plan is None:
            raise CompanyAdapterError("No matching membership plan is active.")
        self._space()
        members = self._members()
        desired_ids = {
            item["selector_id"] for item in self.last_plan["share_scope"]["selectors"]
        }
        bindings: list[dict[str, Any]] = []
        normalized: list[dict[str, Any]] = []
        for item in members:
            member_id = str(item.get("member_id", ""))
            member_type = str(item.get("member_type", ""))
            role = str(item.get("member_role", ""))
            internal = member_type in INTERNAL_MEMBER_TYPES
            if member_id in desired_ids:
                bindings.append(
                    {"selector_id": member_id, "role": role, "internal": internal}
                )
            digest = principal_hash(member_id)
            normalized.append(
                {
                    "principal_hash": digest,
                    "role": role,
                    "internal": internal,
                    "employee": member_type in {"openid", "userid", "unionid", "email"},
                    "deployer_admin": digest in self.allowed_admin_principal_hashes,
                }
            )
        version = "sha256:" + canonical_hash(
            sorted(
                (str(item.get("member_id", "")), str(item.get("member_type", "")), str(item.get("member_role", "")))
                for item in members
            )
        )
        return {
            "schema": "kb-membership-readback/v1",
            "space_id": self.space_id,
            "remote_version": version,
            "external_sharing": False,
            "bindings": bindings,
            "members": normalized,
        }


class LarkKnowledgeReader:
    def __init__(self, runner: Runner = run_lark_cli) -> None:
        self.runner = runner

    def verify_space(self, space_id: str) -> dict[str, Any]:
        adapter = LarkMembershipAdapter(space_id, runner=self.runner)
        return adapter._space()

    def list_tree(self, space_id: str, max_nodes: int = 5000) -> list[dict[str, Any]]:
        self.verify_space(space_id)
        queue: deque[str | None] = deque([None])
        visited_parents: set[str] = set()
        nodes: dict[str, dict[str, Any]] = {}
        while queue:
            parent = queue.popleft()
            parent_key = parent or "<root>"
            if parent_key in visited_parents:
                continue
            visited_parents.add(parent_key)
            if len(visited_parents) > max_nodes + 1:
                raise CompanyAdapterError("Wiki tree exceeds the safe traversal limit.")
            argv = [
                "lark-cli",
                "wiki",
                "+node-list",
                "--space-id",
                str(space_id),
                "--page-all",
                "--page-limit",
                "0",
                "--as",
                "user",
                "--format",
                "json",
            ]
            if parent:
                argv.extend(["--parent-node-token", parent])
            data = response_data(self.runner(argv))
            if data.get("has_more") is True:
                raise CompanyAdapterError("Wiki node pagination did not complete.")
            items = data.get("nodes", data.get("items", []))
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise CompanyAdapterError("Wiki node-list readback is invalid.")
            for item in items:
                token = str(item.get("node_token", "")).strip()
                if not token:
                    raise CompanyAdapterError("Wiki node has no node token.")
                normalized = dict(item)
                normalized["parent_node_token"] = parent or ""
                nodes[token] = normalized
                if item.get("has_child") is True or item.get("has_child_nodes") is True:
                    queue.append(token)
                if len(nodes) > max_nodes:
                    raise CompanyAdapterError("Wiki tree exceeds the safe node limit.")
        return [nodes[key] for key in sorted(nodes)]

    def fetch_markdown(self, doc_token: str) -> dict[str, Any]:
        data = response_data(
            self.runner(
                [
                    "lark-cli",
                    "docs",
                    "+fetch",
                    "--doc",
                    doc_token,
                    "--doc-format",
                    "markdown",
                    "--detail",
                    "simple",
                    "--scope",
                    "full",
                    "--as",
                    "user",
                    "--format",
                    "json",
                ]
            )
        )
        document = data.get("document", data)
        if not isinstance(document, dict):
            raise CompanyAdapterError("Document readback is invalid.")
        content = document.get("content")
        revision = document.get("revision_id")
        if not isinstance(content, str) or revision is None:
            raise CompanyAdapterError("Document readback lacks content or revision.")
        return {
            "content": content,
            "revision_id": str(revision),
            "document_id": str(document.get("document_id", doc_token)),
        }

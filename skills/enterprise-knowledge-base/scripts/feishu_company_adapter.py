#!/usr/bin/env python3
"""Managed lark-cli adapters for company setup, membership, and Wiki reads."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Callable

from membership_policy import (
    DIRECTORY_READBACK_SCHEMA,
    INTERNAL_MEMBER_TYPES,
    build_membership_plan,
    canonical_hash,
    resolve_managed_user_scope,
    resolve_share_scope,
    validate_plan,
)


Runner = Callable[[list[str]], dict[str, Any]]
SELECTOR_MEMBER_TYPES = {
    "organization-root": "opendepartmentid",
    "department": "opendepartmentid",
    "group": "openchat",
    "user": "openid",
}


class CompanyAdapterError(RuntimeError):
    pass


class RootUnavailableError(CompanyAdapterError):
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


def current_user_principal_hash(runner: Runner) -> str:
    payload = runner(["lark-cli", "auth", "status", "--json", "--verify"])
    data = response_data(payload)
    identities = data.get("identities", payload.get("identities", {}))
    user = identities.get("user", {}) if isinstance(identities, dict) else {}
    open_id = str(user.get("openId") or user.get("open_id") or "").strip()
    identity = data.get("identity")
    if not open_id and isinstance(identity, dict):
        open_id = str(identity.get("openId") or identity.get("open_id") or "").strip()
    status = str(user.get("status", ""))
    accepted = status in {
        "authenticated",
        "valid",
        "ok",
        "active",
        "configured",
    }
    if status in {"ready", "needs_refresh"}:
        accepted = payload.get("verified") is True
    if not accepted or not open_id:
        raise CompanyAdapterError("Verified administrator user identity is unavailable.")
    return principal_hash(open_id)


def normalize_space(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise CompanyAdapterError("Knowledge-space readback is invalid.")
    normalized = {
        "space_id": str(value.get("space_id", "")).strip(),
        "name": str(value.get("name", "")).strip(),
        "description": str(value.get("description", "")),
        "space_type": str(value.get("space_type", "")),
        "visibility": str(value.get("visibility", "")),
        "open_sharing": str(value.get("open_sharing", "")),
    }
    if not normalized["space_id"].isdigit() or not normalized["name"]:
        raise CompanyAdapterError("Knowledge-space readback has no valid identity or name.")
    return normalized


def normalize_private_team_space(value: Any) -> dict[str, str]:
    normalized = normalize_space(value)
    if (
        normalized["space_type"] != "team"
        or normalized["visibility"] != "private"
        or normalized["open_sharing"] != "closed"
    ):
        raise CompanyAdapterError("Knowledge space is not a private internal team space.")
    return normalized


class LarkSpaceSetupAdapter:
    """User-scoped Wiki space and fixed-template setup with complete readback."""

    def __init__(self, runner: Runner = run_lark_cli) -> None:
        self.runner = runner

    def current_user_principal_hash(self) -> str:
        return current_user_principal_hash(self.runner)

    def list_spaces(self) -> list[dict[str, str]]:
        data = response_data(
            self.runner(
                [
                    "lark-cli",
                    "wiki",
                    "+space-list",
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
            raise CompanyAdapterError("Knowledge-space pagination did not complete.")
        spaces = data.get("spaces", [])
        if not isinstance(spaces, list):
            raise CompanyAdapterError("Knowledge-space list readback is invalid.")
        return [normalize_space(item) for item in spaces]

    def create_space(self, name: str, description: str) -> dict[str, str]:
        argv = [
            "lark-cli",
            "wiki",
            "+space-create",
            "--name",
            name,
        ]
        if description:
            argv.extend(["--description", description])
        argv.extend(["--as", "user", "--format", "json"])
        data = response_data(self.runner(argv))
        return normalize_private_team_space(data.get("space", data))

    def verify_space(self, space_id: str) -> dict[str, str]:
        data = response_data(
            self.runner(
                [
                    "lark-cli",
                    "wiki",
                    "spaces",
                    "get",
                    "--space-id",
                    str(space_id),
                    "--as",
                    "user",
                    "--format",
                    "json",
                ]
            )
        )
        return normalize_private_team_space(data.get("space", data))

    def list_tree(self, space_id: str) -> list[dict[str, Any]]:
        return LarkKnowledgeReader(runner=self.runner).list_tree(str(space_id))

    def create_node(
        self,
        space_id: str,
        title: str,
        parent_node_token: str = "",
        obj_type: str = "docx",
    ) -> dict[str, str]:
        argv = [
            "lark-cli",
            "wiki",
            "+node-create",
            "--space-id",
            str(space_id),
            "--obj-type",
            obj_type,
            "--title",
            title,
        ]
        if parent_node_token:
            argv.extend(["--parent-node-token", parent_node_token])
        argv.extend(["--as", "user", "--format", "json"])
        data = response_data(self.runner(argv))
        node = data.get("node", data)
        if not isinstance(node, dict):
            raise CompanyAdapterError("Created Wiki node response is invalid.")
        normalized = {
            "node_token": str(node.get("node_token", "")).strip(),
            "obj_token": str(node.get("obj_token", "")).strip(),
            "obj_type": str(node.get("obj_type", obj_type)).strip(),
            "title": str(node.get("title", title)).strip(),
            "parent_node_token": str(
                node.get("parent_node_token", parent_node_token)
            ).strip(),
        }
        if (
            not normalized["node_token"]
            or not normalized["obj_token"]
            or normalized["obj_type"] != obj_type
            or normalized["title"] != title
            or normalized["parent_node_token"] != parent_node_token
        ):
            raise CompanyAdapterError("Created Wiki node response is incomplete or mismatched.")
        return normalized


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
        self.last_execution: dict[str, Any] | None = None

    def current_user_principal_hash(self) -> str:
        return current_user_principal_hash(self.runner)

    def resolve_all_employees(self) -> dict[str, Any]:
        try:
            payload = self.runner(
                [
                    "lark-cli", "api", "GET", "/open-apis/contact/v3/departments/0",
                    "--as", "user", "--params",
                    '{"department_id_type":"open_department_id","user_id_type":"open_id"}',
                    "--format", "json",
                ]
            )
        except CompanyAdapterError as exc:
            message = str(exc).casefold()
            if any(marker in message for marker in (
                "not found", "permission", "authority", "unavailable", "invisible", "root"
            )):
                raise RootUnavailableError("The organization root is unavailable.") from exc
            raise
        data = response_data(payload)
        department = data.get("department", data)
        if not isinstance(department, dict):
            raise RootUnavailableError("The organization root was not returned.")
        department_id = str(
            department.get("open_department_id")
            or department.get("department_id")
            or ""
        ).strip()
        name = str(department.get("name", "")).strip()
        parent = str(department.get("parent_department_id", "")).strip()
        if not department_id or not name or parent not in {"", "0"}:
            raise RootUnavailableError("The organization root could not be resolved uniquely.")
        return {
            "kind": "organization-root",
            "selector_id": department_id,
            "display_name": name,
            "verified": True,
            "internal": True,
        }

    def _raw_page(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        data = response_data(
            self.runner(
                [
                    "lark-cli",
                    "api",
                    "GET",
                    endpoint,
                    "--as",
                    "user",
                    "--params",
                    json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                    "--format",
                    "json",
                ]
            )
        )
        if not isinstance(data, dict):
            raise CompanyAdapterError("Directory page is invalid.")
        return data

    def _paged_items(self, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        seen_tokens: set[str] = set()
        for _ in range(10000):
            request = dict(params)
            request["page_size"] = 50
            if page_token:
                request["page_token"] = page_token
            data = self._raw_page(endpoint, request)
            page = data.get("items", data.get("users", data.get("departments", [])))
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise CompanyAdapterError("Directory page items are invalid.")
            items.extend(page)
            if data.get("has_more") is not True:
                return items
            next_token = str(data.get("page_token", "")).strip()
            if not next_token or next_token == page_token or next_token in seen_tokens:
                raise CompanyAdapterError("Directory pagination did not make progress.")
            seen_tokens.add(next_token)
            page_token = next_token
        raise CompanyAdapterError("Directory pagination exceeded the safe limit.")

    def directory_snapshot(self) -> dict[str, Any]:
        """Traverse the full visible tenant directory with no retained remote state."""
        queue: deque[str] = deque(["0"])
        seen_departments: set[str] = set()
        users: dict[str, dict[str, Any]] = {}
        while queue:
            department_id = queue.popleft()
            if department_id in seen_departments:
                continue
            seen_departments.add(department_id)
            if len(seen_departments) > 10000:
                raise CompanyAdapterError("Directory exceeds the safe department limit.")
            base = {
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
            }
            children = self._paged_items(
                f"/open-apis/contact/v3/departments/{department_id}/children",
                base,
            )
            for item in children:
                child = str(item.get("open_department_id") or item.get("department_id") or "").strip()
                if not child or child == department_id:
                    raise CompanyAdapterError("Directory contains an invalid department identity.")
                queue.append(child)
            direct_users = self._paged_items(
                "/open-apis/contact/v3/users/find_by_department",
                {**base, "department_id": department_id},
            )
            for item in direct_users:
                member_id = str(item.get("open_id", "")).strip()
                status = item.get("status")
                if not member_id or not isinstance(status, dict):
                    raise CompanyAdapterError("Directory user identity or status is incomplete.")
                normalized = {
                    "member_id": member_id,
                    "member_type": "openid",
                    "display_name": str(item.get("name", "Employee")).strip() or "Employee",
                    "internal": item.get("is_external") is not True,
                    "external": item.get("is_external") is True,
                    "status": {
                        "activated": status.get("is_activated") is True,
                        "frozen": status.get("is_frozen") is True,
                        "resigned": status.get("is_resigned") is True,
                        "exited": status.get("is_exited") is True,
                        "unjoined": status.get("is_unjoin") is True,
                    },
                }
                previous = users.get(member_id)
                if previous is not None and previous != normalized:
                    raise CompanyAdapterError("Directory returned contradictory user records.")
                users[member_id] = normalized
        basis = [users[key] for key in sorted(users)]
        return {
            "schema": DIRECTORY_READBACK_SCHEMA,
            "complete": True,
            "all_employees_scope": "0" in seen_departments,
            "tenant_verified": True,
            "snapshot_version": "sha256:" + canonical_hash(basis),
            "users": basis,
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
        space = normalize_private_team_space(data.get("space", data))
        if space["space_id"] != self.space_id:
            raise CompanyAdapterError("Knowledge-space readback returned the wrong space.")
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

    def prepare_membership_plan(
        self, previously_managed: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        self._space()
        current = [
            {
                **item,
                "deployer_admin": principal_hash(str(item.get("member_id", "")))
                in self.allowed_admin_principal_hashes,
            }
            for item in self._members()
        ]
        excluded = 0
        try:
            scope = resolve_share_scope("all-employees", [self.resolve_all_employees()])
            desired = None
        except RootUnavailableError:
            scope, desired, excluded = resolve_managed_user_scope(self.directory_snapshot())
        return build_membership_plan(
            self.space_id,
            scope,
            "members",
            desired_members=desired,
            current_members=current,
            previously_managed=previously_managed or [],
            excluded_count=excluded,
        )

    def apply_membership_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        validate_plan(plan)
        if (
            plan["space_id"] != self.space_id
            or plan["share_scope"]["type"] != "all-employees"
            or plan["employee_policy"] != "members"
        ):
            raise CompanyAdapterError("Membership plan is not the company-wide full-access plan.")
        self._space()
        self.last_plan = plan
        writes = 0
        audit: list[dict[str, Any]] = []
        for operation in plan["operations"]:
            member_id = str(operation["member_id"])
            member_type = str(operation["member_type"])
            action = str(operation["action"])
            current = {
                (str(item.get("member_type", "")), str(item.get("member_id", ""))): item
                for item in self._members()
            }
            present = current.get((member_type, member_id))
            if action == "add" and present and present.get("member_role") == "member":
                audit.append({"action": action, "principal_hash": principal_hash(member_id), "status": "already-satisfied"})
                continue
            if action == "remove" and present is None:
                audit.append({"action": action, "principal_hash": principal_hash(member_id), "status": "already-satisfied"})
                continue
            if present and present.get("member_role") == "admin":
                raise CompanyAdapterError("Membership operation would change an administrator.")
            command = "+member-add" if action == "add" else "+member-remove"
            argv = [
                "lark-cli", "wiki", command,
                "--space-id", self.space_id,
                "--member-id", member_id,
                "--member-type", member_type,
            ]
            argv.extend(["--member-role", "member"])
            argv.extend(["--as", "user", "--format", "json"])
            try:
                self.runner(argv)
            except CompanyAdapterError as exc:
                after_error = {
                    (str(item.get("member_type", "")), str(item.get("member_id", ""))): item
                    for item in self._members()
                }
                satisfied = (
                    action == "add" and after_error.get((member_type, member_id), {}).get("member_role") == "member"
                ) or (action == "remove" and (member_type, member_id) not in after_error)
                if not satisfied:
                    self.last_execution = {"writes": writes, "operations": audit, "outcome": "unknown"}
                    raise CompanyAdapterError("Membership write outcome is unknown; readback did not converge.") from exc
                audit.append({"action": action, "principal_hash": principal_hash(member_id), "status": "recovered-by-readback"})
                writes += 1
                continue
            after = {
                (str(item.get("member_type", "")), str(item.get("member_id", ""))): item
                for item in self._members()
            }
            satisfied = (
                action == "add" and after.get((member_type, member_id), {}).get("member_role") == "member"
            ) or (action == "remove" and (member_type, member_id) not in after)
            if not satisfied:
                self.last_execution = {"writes": writes + 1, "operations": audit, "outcome": "unverified"}
                raise CompanyAdapterError("Membership write did not pass immediate readback.")
            audit.append({"action": action, "principal_hash": principal_hash(member_id), "status": "verified"})
            writes += 1
        self.last_execution = {"writes": writes, "operations": audit, "outcome": "verified"}
        return self.last_execution

    def member_list(self, space_id: str) -> dict[str, Any]:
        if str(space_id) != self.space_id or self.last_plan is None:
            raise CompanyAdapterError("No matching membership plan is active.")
        self._space()
        members = self._members()
        version = "sha256:" + canonical_hash(
            sorted(
                (str(item.get("member_id", "")), str(item.get("member_type", "")), str(item.get("member_role", "")))
                for item in members
            )
        )
        return {
            "schema": "kb-membership-readback/v2",
            "space_id": self.space_id,
            "remote_version": version,
            "complete": True,
            "external_sharing": False,
            "members": [
                {
                    "member_id": str(item.get("member_id", "")),
                    "member_type": str(item.get("member_type", "")),
                    "member_role": str(item.get("member_role", "")),
                    "internal": str(item.get("member_type", "")) in INTERNAL_MEMBER_TYPES,
                    "deployer_admin": principal_hash(str(item.get("member_id", ""))) in self.allowed_admin_principal_hashes,
                }
                for item in members
            ],
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

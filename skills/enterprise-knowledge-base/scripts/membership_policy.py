#!/usr/bin/env python3
"""Fail-closed membership planning and verification with no Feishu transport."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol


SHARE_SCOPE_SCHEMA = "kb-share-scope/v1"
MEMBERSHIP_PLAN_SCHEMA = "kb-membership-plan/v1"
MEMBERSHIP_READBACK_SCHEMA = "kb-membership-readback/v1"
MEMBERSHIP_VERIFICATION_SCHEMA = "kb-membership-verification/v1"
EMPLOYEE_EVIDENCE_SCHEMA = "kb-employee-access-evidence/v1"
EXACT_CONFIRMATION = "确认更新知识空间成员"
SHARE_TYPES = {"all-employees"}
SELECTOR_KINDS = {
    "all-employees": "organization-root",
}
POLICIES = {"members"}
POLICY_LABELS = {
    "members": "查询、收录并发布",
}
SCOPE_LABELS = {
    "all-employees": "全公司内部员工",
}


class MembershipError(RuntimeError):
    pass


class MembershipAdapter(Protocol):
    def apply_membership_plan(self, plan: dict[str, Any]) -> int: ...

    def member_list(self, space_id: str) -> dict[str, Any]: ...


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_policy(policy: str) -> str:
    if policy not in POLICIES:
        raise MembershipError("Unsupported employee policy.")
    return policy


def validate_share_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SHARE_SCOPE_SCHEMA:
        raise MembershipError("Unsupported share-scope schema.")
    scope_type = str(value.get("type", ""))
    if scope_type not in SHARE_TYPES:
        raise MembershipError("Unsupported share-scope type.")
    selectors = value.get("selectors")
    if not isinstance(selectors, list) or not selectors:
        raise MembershipError("Share scope has no verified selector.")
    expected_kind = SELECTOR_KINDS[scope_type]
    if scope_type == "all-employees" and len(selectors) != 1:
        raise MembershipError("All-employees scope must resolve to one organization root.")
    seen: set[str] = set()
    for selector in selectors:
        if not isinstance(selector, dict):
            raise MembershipError("Invalid share-scope selector.")
        if set(selector) - {
            "kind",
            "selector_id",
            "display_name",
            "verified",
            "internal",
        }:
            raise MembershipError("Share-scope selector contains unsupported identity data.")
        selector_id = str(selector.get("selector_id", "")).strip()
        if (
            selector.get("kind") != expected_kind
            or not selector_id
            or not str(selector.get("display_name", "")).strip()
            or selector.get("verified") is not True
            or selector.get("internal") is not True
        ):
            raise MembershipError("Share-scope selector is unverified or external.")
        if selector_id in seen:
            raise MembershipError("Duplicate share-scope selector.")
        seen.add(selector_id)
    return value


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema") != MEMBERSHIP_PLAN_SCHEMA:
        raise MembershipError("Unsupported membership plan.")
    scope = validate_share_scope(plan.get("share_scope"))
    validate_policy(str(plan.get("employee_policy", "")))
    basis = {
        "schema": MEMBERSHIP_PLAN_SCHEMA,
        "space_id": str(plan.get("space_id", "")),
        "share_scope": scope,
        "space_role": plan.get("space_role"),
        "external_sharing": plan.get("external_sharing"),
        "employee_policy": plan.get("employee_policy"),
        "share_scope_hash": plan.get("share_scope_hash"),
    }
    if (
        not str(plan.get("space_id", "")).strip()
        or plan.get("space_role") != "member"
        or plan.get("external_sharing") is not False
        or plan.get("share_scope_hash") != canonical_hash(scope)
        or plan.get("plan_hash") != canonical_hash(basis)
    ):
        raise MembershipError("Membership plan violates the internal member boundary.")
    return plan


def resolve_share_scope(scope_type: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if scope_type not in SHARE_TYPES:
        raise MembershipError("Unsupported share-scope type.")
    expected_kind = SELECTOR_KINDS[scope_type]
    eligible = [
        {
            "kind": expected_kind,
            "selector_id": str(item.get("selector_id", "")).strip(),
            "display_name": str(item.get("display_name", "")).strip(),
            "verified": item.get("verified") is True,
            "internal": item.get("internal") is True,
        }
        for item in candidates
        if item.get("kind") == expected_kind
        and item.get("verified") is True
        and item.get("internal") is True
    ]
    if scope_type == "all-employees" and len(eligible) != 1:
        raise MembershipError(
            "The organization-wide employee scope could not be resolved uniquely."
        )
    scope = {
        "schema": SHARE_SCOPE_SCHEMA,
        "type": scope_type,
        "selectors": eligible,
    }
    return validate_share_scope(scope)


def build_membership_plan(
    space_id: str,
    share_scope: dict[str, Any],
    employee_policy: str,
) -> dict[str, Any]:
    validate_share_scope(share_scope)
    validate_policy(employee_policy)
    if not str(space_id).strip():
        raise MembershipError("Knowledge-space identity is missing.")
    plan = {
        "schema": MEMBERSHIP_PLAN_SCHEMA,
        "space_id": str(space_id),
        "share_scope": share_scope,
        "space_role": "member",
        "external_sharing": False,
        "employee_policy": employee_policy,
        "share_scope_hash": canonical_hash(share_scope),
    }
    plan["plan_hash"] = canonical_hash(plan)
    return plan


def human_preview(plan: dict[str, Any]) -> dict[str, Any]:
    validate_plan(plan)
    scope = validate_share_scope(plan.get("share_scope"))
    policy = validate_policy(str(plan.get("employee_policy", "")))
    return {
        "共享范围": SCOPE_LABELS[scope["type"]],
        "对象": [item["display_name"] for item in scope["selectors"]],
        "知识空间角色": "成员",
        "员工功能": POLICY_LABELS[policy],
        "外部分享": "关闭",
        "需要单独确认": True,
    }


def verify_member_readback(
    plan: dict[str, Any], readback: dict[str, Any]
) -> dict[str, Any]:
    validate_plan(plan)
    if readback.get("schema") != MEMBERSHIP_READBACK_SCHEMA:
        raise MembershipError("Unsupported member-list readback schema.")
    if str(readback.get("space_id", "")) != str(plan.get("space_id", "")):
        raise MembershipError("Member-list readback is for a different space.")
    if readback.get("external_sharing") is not False:
        raise MembershipError("External sharing is not closed.")
    if not str(readback.get("remote_version", "")).strip():
        raise MembershipError("Member-list readback has no remote version.")

    desired = {
        item["selector_id"] for item in plan["share_scope"]["selectors"]
    }
    bindings = readback.get("bindings", [])
    if not isinstance(bindings, list):
        raise MembershipError("Invalid member binding readback.")
    covered = {
        str(item.get("selector_id", ""))
        for item in bindings
        if isinstance(item, dict)
        and item.get("role") == "member"
        and item.get("internal") is True
    }
    if covered != desired:
        raise MembershipError("Member-list readback does not match the authorized scope.")

    members = readback.get("members", [])
    if not isinstance(members, list):
        raise MembershipError("Invalid member-list readback.")
    for member in members:
        if not isinstance(member, dict) or member.get("internal") is not True:
            raise MembershipError("External or invalid member detected.")
        if (
            member.get("employee") is True
            and member.get("deployer_admin") is not True
            and member.get("role") == "admin"
        ):
            raise MembershipError("A normal employee was granted administrator role.")

    snapshot_basis = {
        "space_id": str(readback["space_id"]),
        "remote_version": str(readback["remote_version"]),
        "external_sharing": False,
        "bindings": sorted(
            (
                str(item.get("selector_id", "")),
                str(item.get("role", "")),
                bool(item.get("internal")),
            )
            for item in bindings
        ),
        "members": sorted(
            (
                str(item.get("principal_hash", "")),
                str(item.get("role", "")),
                bool(item.get("internal")),
                bool(item.get("employee")),
                bool(item.get("deployer_admin")),
            )
            for item in members
        ),
    }
    return {
        "schema": MEMBERSHIP_VERIFICATION_SCHEMA,
        "verified": True,
        "space_id": str(readback["space_id"]),
        "remote_version": str(readback["remote_version"]),
        "share_scope_hash": canonical_hash(plan["share_scope"]),
        "member_list_hash": canonical_hash(snapshot_basis),
        "external_members": 0,
        "employee_admin_members": 0,
    }


def execute_membership_change(
    adapter: MembershipAdapter,
    plan: dict[str, Any],
    confirmation: str,
) -> dict[str, Any]:
    validate_plan(plan)
    if confirmation != EXACT_CONFIRMATION:
        return {
            "ok": False,
            "authorized": False,
            "writes": 0,
            "reason": "explicit-confirmation-required",
        }
    writes = adapter.apply_membership_plan(plan)
    snapshot = verify_member_readback(
        plan, adapter.member_list(str(plan["space_id"]))
    )
    return {
        "ok": True,
        "authorized": True,
        "writes": int(writes),
        "verification": snapshot,
    }


def verify_employee_access(
    company: dict[str, Any], evidence: dict[str, Any], requested_policy: str | None = None
) -> dict[str, Any]:
    if evidence.get("schema") != EMPLOYEE_EVIDENCE_SCHEMA:
        raise MembershipError("Unsupported employee-access evidence schema.")
    if evidence.get("oauth_ok") is not True:
        raise MembershipError("Employee OAuth is incomplete.")
    if evidence.get("external_account") is True:
        raise MembershipError("External accounts are outside the authorized company scope.")
    if str(evidence.get("tenant_key_hash", "")) != str(company.get("tenant_key_hash", "")):
        raise MembershipError("Employee identity belongs to a different tenant.")
    if (
        evidence.get("space_visible") is not True
        or evidence.get("space_member") is not True
        or str(evidence.get("space_id", "")) != str(company.get("space_id", ""))
    ):
        raise MembershipError("OAuth succeeded but knowledge-space membership is missing.")
    if evidence.get("inside_authorized_scope") is not True:
        raise MembershipError("Employee is outside the administrator-authorized scope.")
    if evidence.get("space_role") != "member":
        raise MembershipError("Normal employees must have the knowledge-space member role.")
    verification = company.get("membership_verification", {})
    if (
        str(evidence.get("membership_version", ""))
        != str(verification.get("remote_version", ""))
        or str(evidence.get("membership_hash", ""))
        != str(verification.get("member_list_hash", ""))
    ):
        raise MembershipError("Employee membership evidence is stale or contradictory.")
    effective = validate_policy(str(company.get("effective_employee_policy", "")))
    if requested_policy is not None:
        validate_policy(requested_policy)
        if requested_policy != effective:
            raise MembershipError("An employee cannot override the company publication policy.")
    return {
        "ok": True,
        "space_id": str(company["space_id"]),
        "effective_employee_policy": effective,
        "membership_verified": True,
    }

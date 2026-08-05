#!/usr/bin/env python3
"""Fail-closed company membership planning with no Feishu transport."""

from __future__ import annotations

from typing import Any, Protocol

from kb_core import canonical_hash


SHARE_SCOPE_SCHEMA = "kb-share-scope/v2"
DIRECTORY_READBACK_SCHEMA = "kb-directory-readback/v1"
MEMBERSHIP_PLAN_SCHEMA = "kb-membership-plan/v2"
MEMBERSHIP_READBACK_SCHEMA = "kb-membership-readback/v2"
MEMBERSHIP_VERIFICATION_SCHEMA = "kb-membership-verification/v2"
EMPLOYEE_EVIDENCE_SCHEMA = "kb-employee-access-evidence/v2"
EXACT_CONFIRMATION = "确认更新知识空间成员"
SHARE_TYPES = {"all-employees"}
STRATEGIES = {"organization-root", "managed-users"}
POLICIES = {"members"}
USER_MEMBER_TYPES = {"openid", "userid", "unionid"}
INTERNAL_MEMBER_TYPES = USER_MEMBER_TYPES | {"opendepartmentid"}


class MembershipError(RuntimeError):
    pass


class MembershipAdapter(Protocol):
    def apply_membership_plan(self, plan: dict[str, Any]) -> Any: ...

    def member_list(self, space_id: str) -> dict[str, Any]: ...


def validate_policy(policy: str) -> str:
    if policy not in POLICIES:
        raise MembershipError("Unsupported employee policy.")
    return policy


def _subject(value: Any, *, allow_role: bool = True) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MembershipError("Invalid membership subject.")
    allowed = {"member_id", "member_type"} | ({"member_role"} if allow_role else set())
    if set(value) - allowed:
        raise MembershipError("Membership subject contains excessive identity data.")
    member_id = str(value.get("member_id", "")).strip()
    member_type = str(value.get("member_type", "")).strip()
    role = str(value.get("member_role", "member")).strip()
    if not member_id or member_type not in INTERNAL_MEMBER_TYPES:
        raise MembershipError("Membership subject is missing or external.")
    if allow_role and role not in {"member", "admin"}:
        raise MembershipError("Unsupported knowledge-space member role.")
    result = {"member_id": member_id, "member_type": member_type}
    if allow_role:
        result["member_role"] = role
    return result


def _subject_key(value: dict[str, Any]) -> tuple[str, str]:
    return str(value.get("member_type", "")), str(value.get("member_id", ""))


def _normalized_subjects(values: Any, *, role: str = "member") -> list[dict[str, str]]:
    if not isinstance(values, list):
        raise MembershipError("Membership subjects must be a list.")
    normalized: dict[tuple[str, str], dict[str, str]] = {}
    for value in values:
        item = _subject(value)
        item["member_role"] = role if role else item["member_role"]
        key = _subject_key(item)
        if key in normalized and normalized[key] != item:
            raise MembershipError("Duplicate membership subject has conflicting roles.")
        normalized[key] = item
    return [normalized[key] for key in sorted(normalized)]


def _current_subjects(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise MembershipError("Current members must be a list.")
    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or set(value) - {
            "member_id", "member_type", "member_role", "deployer_admin"
        }:
            raise MembershipError("Current member contains unsupported identity data.")
        item: dict[str, Any] = _subject(
            {key: value[key] for key in ("member_id", "member_type", "member_role") if key in value}
        )
        item["deployer_admin"] = value.get("deployer_admin") is True
        key = _subject_key(item)
        if key in normalized and normalized[key] != item:
            raise MembershipError("Duplicate current member is contradictory.")
        normalized[key] = item
    return [normalized[key] for key in sorted(normalized)]


def validate_share_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SHARE_SCOPE_SCHEMA:
        raise MembershipError("Unsupported share-scope schema.")
    if value.get("type") not in SHARE_TYPES or value.get("strategy") not in STRATEGIES:
        raise MembershipError("Unsupported all-employees strategy.")
    allowed = {"schema", "type", "strategy", "selector", "subject_count", "roster_hash"}
    if set(value) - allowed:
        raise MembershipError("Share scope contains excessive identity data.")
    strategy = value["strategy"]
    if strategy == "organization-root":
        selector = value.get("selector")
        if not isinstance(selector, dict) or set(selector) - {
            "kind", "selector_id", "display_name", "verified", "internal"
        }:
            raise MembershipError("Invalid organization-root selector.")
        if (
            selector.get("kind") != "organization-root"
            or not str(selector.get("selector_id", "")).strip()
            or not str(selector.get("display_name", "")).strip()
            or selector.get("verified") is not True
            or selector.get("internal") is not True
            or value.get("subject_count") != 1
        ):
            raise MembershipError("Organization root is unverified or ambiguous.")
        expected = canonical_hash(
            {"member_type": "opendepartmentid", "member_id": selector["selector_id"]}
        )
    else:
        if "selector" in value:
            raise MembershipError("Managed-user scope must not export a roster selector.")
        count = value.get("subject_count")
        if not isinstance(count, int) or count < 1:
            raise MembershipError("Managed-user scope has no eligible employee.")
        expected = str(value.get("roster_hash", ""))
    if value.get("roster_hash") != expected or len(expected) != 64:
        raise MembershipError("Share-scope roster proof is invalid.")
    return value


def resolve_share_scope(scope_type: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve the preferred single organization-root strategy."""
    if scope_type != "all-employees":
        raise MembershipError("Unsupported share-scope type.")
    eligible = [
        {
            "kind": "organization-root",
            "selector_id": str(item.get("selector_id", "")).strip(),
            "display_name": str(item.get("display_name", "")).strip(),
            "verified": item.get("verified") is True,
            "internal": item.get("internal") is True,
        }
        for item in candidates
        if isinstance(item, dict)
        and item.get("kind") == "organization-root"
        and item.get("verified") is True
        and item.get("internal") is True
        and str(item.get("selector_id", "")).strip()
        and str(item.get("display_name", "")).strip()
    ]
    if len(eligible) != 1:
        raise MembershipError("The organization-wide employee root could not be resolved uniquely.")
    subject = {
        "member_type": "opendepartmentid",
        "member_id": eligible[0]["selector_id"],
    }
    return validate_share_scope(
        {
            "schema": SHARE_SCOPE_SCHEMA,
            "type": "all-employees",
            "strategy": "organization-root",
            "selector": eligible[0],
            "subject_count": 1,
            "roster_hash": canonical_hash(subject),
        }
    )


def resolve_managed_user_scope(snapshot: Any) -> tuple[dict[str, Any], list[dict[str, str]], int]:
    """Validate a complete directory traversal and return only eligible user subjects."""
    if not isinstance(snapshot, dict) or snapshot.get("schema") != DIRECTORY_READBACK_SCHEMA:
        raise MembershipError("Unsupported directory readback.")
    if (
        snapshot.get("complete") is not True
        or snapshot.get("all_employees_scope") is not True
        or snapshot.get("tenant_verified") is not True
        or not str(snapshot.get("snapshot_version", "")).strip()
    ):
        raise MembershipError("The all-employees directory traversal is incomplete.")
    users = snapshot.get("users")
    if not isinstance(users, list):
        raise MembershipError("Directory users are invalid.")
    eligible: dict[tuple[str, str], dict[str, str]] = {}
    excluded = 0
    for user in users:
        if not isinstance(user, dict):
            raise MembershipError("Directory user is invalid.")
        allowed = {"member_id", "member_type", "display_name", "internal", "external", "status"}
        if set(user) - allowed:
            raise MembershipError("Directory user contains excessive data.")
        status = user.get("status")
        required_status = {"activated", "frozen", "resigned", "exited", "unjoined"}
        if not isinstance(status, dict) or set(status) != required_status:
            raise MembershipError("Directory user status is incomplete.")
        if any(not isinstance(status[key], bool) for key in required_status):
            raise MembershipError("Directory user status is ambiguous.")
        active = (
            user.get("internal") is True
            and user.get("external") is False
            and status["activated"] is True
            and not status["frozen"]
            and not status["resigned"]
            and not status["exited"]
            and not status["unjoined"]
        )
        if not active:
            excluded += 1
            continue
        subject = _subject(
            {
                "member_id": user.get("member_id"),
                "member_type": user.get("member_type"),
                "member_role": "member",
            }
        )
        if subject["member_type"] not in USER_MEMBER_TYPES:
            raise MembershipError("Managed fallback accepts internal user principals only.")
        key = _subject_key(subject)
        if key in eligible:
            continue
        eligible[key] = subject
    desired = [eligible[key] for key in sorted(eligible)]
    if not desired:
        raise MembershipError("No active internal employee was resolved.")
    roster_basis = [
        {"member_type": item["member_type"], "member_id": item["member_id"]}
        for item in desired
    ]
    scope = validate_share_scope(
        {
            "schema": SHARE_SCOPE_SCHEMA,
            "type": "all-employees",
            "strategy": "managed-users",
            "subject_count": len(desired),
            "roster_hash": canonical_hash(roster_basis),
        }
    )
    return scope, desired, excluded


def build_membership_plan(
    space_id: str,
    share_scope: dict[str, Any],
    employee_policy: str,
    *,
    desired_members: list[dict[str, Any]] | None = None,
    current_members: list[dict[str, Any]] | None = None,
    previously_managed: list[dict[str, Any]] | None = None,
    excluded_count: int = 0,
) -> dict[str, Any]:
    scope = validate_share_scope(share_scope)
    validate_policy(employee_policy)
    if not str(space_id).strip():
        raise MembershipError("Knowledge-space identity is missing.")
    if scope["strategy"] == "organization-root":
        desired = [
            {
                "member_id": scope["selector"]["selector_id"],
                "member_type": "opendepartmentid",
                "member_role": "member",
            }
        ]
    else:
        desired = _normalized_subjects(desired_members or [])
        if len(desired) != scope["subject_count"]:
            raise MembershipError("Managed roster does not match its exported scope proof.")
        basis = [
            {"member_type": item["member_type"], "member_id": item["member_id"]}
            for item in desired
        ]
        if canonical_hash(basis) != scope["roster_hash"]:
            raise MembershipError("Managed roster hash changed.")
    current = _current_subjects(current_members or [])
    previous = _normalized_subjects(previously_managed or [])
    current_by_key = {_subject_key(item): item for item in current}
    desired_by_key = {_subject_key(item): item for item in desired}
    previous_by_key = {_subject_key(item): item for item in previous}
    operations: list[dict[str, str]] = []
    for key in sorted(set(previous_by_key) - set(desired_by_key)):
        remote = current_by_key.get(key)
        if remote is None:
            continue
        if remote["member_role"] == "admin":
            raise MembershipError("A managed principal became an administrator; removal is blocked.")
        operations.append({"action": "remove", **previous_by_key[key]})
    for key in sorted(desired_by_key):
        remote = current_by_key.get(key)
        if remote is None:
            operations.append({"action": "add", **desired_by_key[key]})
        elif remote["member_role"] == "admin" and remote.get("deployer_admin") is True:
            continue
        elif remote["member_role"] == "admin":
            raise MembershipError("An eligible employee is already an administrator; review is required.")
        elif remote["member_role"] != "member":
            raise MembershipError("Existing membership conflicts with the managed plan.")
    plan = {
        "schema": MEMBERSHIP_PLAN_SCHEMA,
        "space_id": str(space_id),
        "share_scope": scope,
        "space_role": "member",
        "external_sharing": False,
        "employee_policy": employee_policy,
        "desired_members": desired,
        "previously_managed": previous,
        "operations": operations,
        "excluded_count": int(excluded_count),
        "share_scope_hash": canonical_hash(scope),
    }
    plan["plan_hash"] = canonical_hash(plan)
    return validate_plan(plan)


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema") != MEMBERSHIP_PLAN_SCHEMA:
        raise MembershipError("Unsupported membership plan.")
    allowed = {
        "schema", "space_id", "share_scope", "space_role", "external_sharing",
        "employee_policy", "desired_members", "previously_managed", "operations",
        "excluded_count", "share_scope_hash", "plan_hash",
    }
    if set(plan) - allowed:
        raise MembershipError("Membership plan contains unsupported data.")
    scope = validate_share_scope(plan.get("share_scope"))
    validate_policy(str(plan.get("employee_policy", "")))
    desired = _normalized_subjects(plan.get("desired_members", []))
    previous = _normalized_subjects(plan.get("previously_managed", []))
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise MembershipError("Membership operations are invalid.")
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("action") not in {"add", "remove"}:
            raise MembershipError("Membership operation is invalid.")
        _subject({key: value for key, value in operation.items() if key != "action"})
    basis = dict(plan)
    supplied_hash = basis.pop("plan_hash", None)
    if (
        not str(plan.get("space_id", "")).strip()
        or plan.get("space_role") != "member"
        or plan.get("external_sharing") is not False
        or not isinstance(plan.get("excluded_count"), int)
        or plan["excluded_count"] < 0
        or plan.get("share_scope_hash") != canonical_hash(scope)
        or supplied_hash != canonical_hash(basis)
        or desired != plan.get("desired_members")
        or previous != plan.get("previously_managed")
    ):
        raise MembershipError("Membership plan violates the internal member boundary.")
    return plan


def human_preview(plan: dict[str, Any]) -> dict[str, Any]:
    validate_plan(plan)
    additions = sum(item["action"] == "add" for item in plan["operations"])
    removals = sum(item["action"] == "remove" for item in plan["operations"])
    return {
        "共享范围": "全公司内部员工",
        "授权策略": "根部门自动覆盖" if plan["share_scope"]["strategy"] == "organization-root" else "受控逐人授权",
        "合格员工数": plan["share_scope"]["subject_count"],
        "排除账号数": plan["excluded_count"],
        "新增成员数": additions,
        "移除成员数": removals,
        "知识空间角色": "成员",
        "员工功能": "查询、收录并发布",
        "外部分享": "关闭",
        "需要单独确认": True,
    }


def verify_member_readback(plan: dict[str, Any], readback: dict[str, Any]) -> dict[str, Any]:
    validate_plan(plan)
    if readback.get("schema") != MEMBERSHIP_READBACK_SCHEMA:
        raise MembershipError("Unsupported member-list readback schema.")
    if str(readback.get("space_id", "")) != str(plan.get("space_id", "")):
        raise MembershipError("Member-list readback is for a different space.")
    if readback.get("complete") is not True or readback.get("external_sharing") is not False:
        raise MembershipError("Member-list readback is incomplete or external sharing is open.")
    if not str(readback.get("remote_version", "")).strip():
        raise MembershipError("Member-list readback has no remote version.")
    members = readback.get("members")
    if not isinstance(members, list):
        raise MembershipError("Invalid member-list readback.")
    remote: dict[tuple[str, str], dict[str, Any]] = {}
    external = 0
    employee_admins = 0
    for member in members:
        if not isinstance(member, dict):
            raise MembershipError("Invalid member readback item.")
        member_id = str(member.get("member_id", "")).strip()
        member_type = str(member.get("member_type", "")).strip()
        role = str(member.get("member_role", "")).strip()
        internal = member.get("internal") is True
        if not member_id or role not in {"member", "admin"}:
            raise MembershipError("Invalid member readback item.")
        if not internal:
            external += 1
        if member_type in USER_MEMBER_TYPES and role == "admin" and member.get("deployer_admin") is not True:
            employee_admins += 1
        remote[(member_type, member_id)] = member
    if external or employee_admins:
        raise MembershipError("External member or unauthorized employee administrator detected.")
    for desired in plan["desired_members"]:
        item = remote.get(_subject_key(desired))
        if item is None or not (
            item.get("member_role") == "member"
            or (item.get("member_role") == "admin" and item.get("deployer_admin") is True)
        ):
            raise MembershipError("Member-list readback does not cover every intended employee.")
    for previous in plan["previously_managed"]:
        if _subject_key(previous) not in {_subject_key(item) for item in plan["desired_members"]}:
            if _subject_key(previous) in remote:
                raise MembershipError("A removed managed principal is still present.")
    snapshot_basis = {
        "space_id": str(readback["space_id"]),
        "remote_version": str(readback["remote_version"]),
        "external_sharing": False,
        "members": sorted(
            (str(item.get("member_type", "")), str(item.get("member_id", "")), str(item.get("member_role", "")))
            for item in members
        ),
    }
    return {
        "schema": MEMBERSHIP_VERIFICATION_SCHEMA,
        "verified": True,
        "strategy": plan["share_scope"]["strategy"],
        "space_id": str(readback["space_id"]),
        "remote_version": str(readback["remote_version"]),
        "share_scope_hash": canonical_hash(plan["share_scope"]),
        "member_list_hash": canonical_hash(snapshot_basis),
        "managed_roster_hash": plan["share_scope"]["roster_hash"],
        "managed_subject_count": plan["share_scope"]["subject_count"],
        "external_members": 0,
        "employee_admin_members": 0,
    }


def execute_membership_change(adapter: MembershipAdapter, plan: dict[str, Any], confirmation: str) -> dict[str, Any]:
    validate_plan(plan)
    if confirmation != EXACT_CONFIRMATION:
        return {"ok": False, "authorized": False, "writes": 0, "reason": "explicit-confirmation-required"}
    result = adapter.apply_membership_plan(plan)
    writes = int(result.get("writes", 0)) if isinstance(result, dict) else int(result)
    snapshot = verify_member_readback(plan, adapter.member_list(str(plan["space_id"])))
    return {"ok": True, "authorized": True, "writes": writes, "execution": result, "verification": snapshot}


def verify_employee_access(company: dict[str, Any], evidence: dict[str, Any], requested_policy: str | None = None) -> dict[str, Any]:
    if evidence.get("schema") != EMPLOYEE_EVIDENCE_SCHEMA:
        raise MembershipError("Unsupported employee-access evidence schema.")
    if evidence.get("oauth_ok") is not True or evidence.get("external_account") is True:
        raise MembershipError("Employee OAuth is incomplete or external.")
    if str(evidence.get("tenant_key_hash", "")) != str(company.get("tenant_key_hash", "")):
        raise MembershipError("Employee identity belongs to a different tenant.")
    if (
        evidence.get("space_visible") is not True
        or evidence.get("space_member") is not True
        or str(evidence.get("space_id", "")) != str(company.get("space_id", ""))
        or evidence.get("inside_authorized_scope") is not True
        or evidence.get("space_role") != "member"
    ):
        raise MembershipError("Employee space membership or company scope is missing.")
    verification = company.get("membership_verification", {})
    if (
        str(evidence.get("membership_version", "")) != str(verification.get("remote_version", ""))
        or str(evidence.get("membership_hash", "")) != str(verification.get("member_list_hash", ""))
    ):
        raise MembershipError("Employee membership evidence is stale or contradictory.")
    effective = validate_policy(str(company.get("effective_employee_policy", "")))
    if requested_policy is not None:
        validate_policy(requested_policy)
        if requested_policy != effective:
            raise MembershipError("An employee cannot override the company publication policy.")
    return {"ok": True, "space_id": str(company["space_id"]), "effective_employee_policy": effective, "membership_verified": True}

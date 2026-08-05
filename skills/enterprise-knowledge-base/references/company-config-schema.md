# Company configuration schema v4

Export `kb-company-config/v4` only after complete membership readback. Keep the package portable and secret-free:

```json
{
  "schema": "kb-company-config/v4",
  "minimum_plugin_version": "0.7.1",
  "company_name": "Readable company name",
  "feishu_brand": "feishu",
  "app_id": "cli_...",
  "tenant_key_hash": "sha256",
  "space_name": "Readable exact space name",
  "space_id": "numeric exact space id",
  "nodes": [
    {"node_name": "Readable node", "node_token": "verified token", "verified": true}
  ],
  "space_mapping_hash": "sha256",
  "share_scope": {
    "schema": "kb-share-scope/v2",
    "type": "all-employees",
    "strategy": "organization-root or managed-users",
    "subject_count": 1,
    "roster_hash": "sha256"
  },
  "effective_employee_policy": "members",
  "membership_verification": {
    "schema": "kb-membership-verification/v2",
    "verified": true,
    "strategy": "organization-root or managed-users",
    "space_id": "numeric exact space id",
    "remote_version": "readback version",
    "share_scope_hash": "sha256",
    "member_list_hash": "sha256",
    "managed_roster_hash": "sha256",
    "managed_subject_count": 1,
    "mapping_hash": "sha256",
    "effective_employee_policy": "members",
    "external_members": 0,
    "employee_admin_members": 0
  }
}
```

For `organization-root`, `share_scope` also contains one verified internal root selector. For `managed-users`, it contains only count and hash evidence; exact managed user IDs stay in the administrator Vault under `.kb/state/managed-members.json` and never enter this package.

`members` means every verified internal employee can query, collect, and publish enterprise knowledge while normal employees remain Wiki members. Never export an App Secret, token, Cookie, password, authorization URL, device code, employee content, name list, email list, phone list, or full employee roster. Import never replaces personal OAuth, exact-space visibility, current membership, mapping, or policy verification.

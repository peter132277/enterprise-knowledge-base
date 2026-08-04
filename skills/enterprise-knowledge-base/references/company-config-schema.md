# Company configuration schema v3

The exported employee package uses `kb-company-config/v3` and contains only portable, non-secret configuration:

```json
{
  "schema": "kb-company-config/v3",
  "minimum_plugin_version": "0.4.0",
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
    "schema": "kb-share-scope/v1",
    "type": "all-employees",
    "selectors": [
      {
        "kind": "organization-root",
        "selector_id": "verified internal root",
        "display_name": "All internal employees",
        "verified": true,
        "internal": true
      }
    ]
  },
  "effective_employee_policy": "members",
  "membership_verification": {
    "schema": "kb-membership-verification/v1",
    "verified": true,
    "space_id": "numeric exact space id",
    "remote_version": "readback version",
    "share_scope_hash": "sha256",
    "member_list_hash": "sha256",
    "mapping_hash": "sha256",
    "effective_employee_policy": "members",
    "external_members": 0,
    "employee_admin_members": 0
  }
}
```

`members` means every verified internal employee can query, collect, and publish enterprise knowledge while remaining a normal Wiki member. No department, group, user exception, administrator-only publication mode, or weaker employee mode is accepted by v3.

Never export an App Secret, access token, refresh token, Cookie, password, authorization URL, device code, employee content, or full employee roster. Import never replaces OAuth, exact-space visibility, current membership, mapping, or policy verification.

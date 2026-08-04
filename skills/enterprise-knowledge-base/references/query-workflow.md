# Managed query workflow

Use only for a factual, explanatory, comparative, summary, recommendation, or lookup request. Search only this Vault; never use another workspace, backup, public web, connected drive, or model memory. The only Feishu access allowed here is the managed read-only company mirror refresh before an enterprise query.

Before retrieval, run the `query` access gate. For `personal`, do not contact Feishu. For `enterprise` or `all`, run exactly one session coordinator check using a stable identifier for the current Codex task:

```text
python <skill-root>/scripts/company_sync_coordinator.py before-query --vault <current-vault> --scope <personal|enterprise|all> --session-id <current-task-id>
```

A new session performs one incremental foreground pull before its first enterprise query. Later enterprise queries in the same session reuse the verified local mirror. Never invent a timer, run a background poller, or copy an administrator's Vault.

Run the one-command fast path first:

```powershell
python <skill-root>/scripts/query_answer_packet.py `
  --query "<exact user question>" `
  --scope auto
```

Use `--scope personal` or `enterprise` when explicit and `all` only when both are explicit. Add `--include-sources` only for complete wording or provenance. Never replace the managed script with direct Obsidian CLI or `rg`.

- `status: audited-fast-path`: read only the approved local files needed for the answer, then answer only from `approved_citations`. If they do not actually support the question, use the manual path instead.
- `status: audited-no-result`: state `当前知识库无相关内容` and do not broaden.
- `status: manual-audit-required`: inspect the returned candidates, then audit the exact question and every intended citation:

```powershell
python <skill-root>/scripts/check_query_compliance.py `
  --question "<exact user question>" `
  --receipt "<receipt_path from query output>" `
  --cited-path "<returned local path>"
```

Repeat `--cited-path` as needed. For zero results, omit citations and pass `--no-result`. Do not answer unless the audit returns `ok: true`; fail closed on receipt mismatch, changed or unreturned evidence, or external sources.

If lexical candidates exist but every one is semantically irrelevant, pass `--no-result` plus one `--reject-path "<returned-path>"` for every candidate. The audit permits semantic zero-result only when the rejected set exactly matches the receipt.

- `检索范围：当前知识库`
- `检索凭证：<query_id>`
- `合规审计：<audit_id>`
- `外部来源：未使用`
- clickable local evidence links approved by the audit

Use only audited paths. For zero results, state `当前知识库无相关内容` with the same receipt fields and do not broaden the query.

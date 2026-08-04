# Managed all-knowledge query

Use only for factual, explanatory, comparative, summary, recommendation, or lookup requests. Every formal query fixes internal `scope: all` and searches all personal and enterprise knowledge in the current bound Vault; never ask the user to choose a knowledge type and never broaden to another workspace, backup, public web, connected drive, or model memory.

Run the single public entry with a stable identifier for the current Codex task:

```powershell
python <skill-root>/scripts/query_answer_packet.py `
  --query "<exact user question>" `
  --session-id "<current-task-id>"
```

The entry authorizes access, performs the new-task foreground company sync when configured, retrieves locally, hashes bounded results, writes the query receipt, and audits clear citations. A new task reads the mapped Wiki tree once; later queries in the same task validate the hashed sync credential, mapping, access evidence, and state tree with zero remote reads and without rehashing the full mirror. Local-only mode records a zero-remote local credential. Never add a timer, background poller, or cross-task freshness cache.

- `audited-fast-path`: answer only from `approved_citations`.
- `audited-no-result`: state `当前知识库无相关内容` and do not broaden.
- `manual-audit-required`: inspect returned candidates, then call the same entry with `--receipt` and each intended `--cited-path`. For semantic no-result, add `--no-result` and one `--reject-path` for every candidate.

The internal compliance component verifies both the query receipt and current sync credential, then recalculates SHA-256 only for cited or rejected files. Do not answer on a stale task credential, changed evidence, unreturned citation, mapping or permission conflict, or any external source.

Return `query_id`, `audit_id`, `检索范围：当前知识库`, `外部来源：未使用`, and clickable approved local evidence. Read [query-retrieval.md](query-retrieval.md) only to diagnose a failed backend; never call an internal retrieval or compliance script as the formal query entry.

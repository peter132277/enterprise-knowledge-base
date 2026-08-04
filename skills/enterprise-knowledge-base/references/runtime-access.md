# Runtime access gate

Every public entry calls the internal deterministic access gate before a normal action. Stop when it fails. Never invoke `runtime_access.py` as a separate user workflow, and never treat application installation, OAuth, an imported company package, or a visible Wiki URL as sufficient authorization.

For an administrator, require the current all-employees membership readback, full employee policy, and exact mapping hash. For an employee, additionally require verified personal OAuth evidence, exact tenant and space, current membership version/hash, and local employee-access state.

The company policy is fixed: every verified internal employee can query, collect, and publish. Normal employees remain Wiki `member`, never space `admin`. Local-only mode can query and collect locally but cannot company-sync or publish to Feishu.

Action order:

1. Query: check `query`. The formal entry fixes `scope: all` and always searches all personal and enterprise knowledge. Before the first query in each Codex task, its coordinator performs one incremental foreground company sync when configured and records a hashed session receipt; later queries in the same task reuse it with zero remote reads. Local-only mode remains zero-remote.
2. Collect: check `collect`, then run local preflight and transactional commit. Do not publish automatically.
3. Media: check `media`; keep Minutes upload and later Wiki publication confirmations separate.
4. Publish preview, confirmation, retry, or recovery: check `publish` each time, then use the existing immutable publication state machine.
5. Explicit `同步公司知识`: check `sync` and run `company_sync_coordinator.py explicit`; return added, updated, unchanged, local-authoritative, and manual-review counts.
6. After verified publication: update the publisher's local authoritative and sync state immediately. Other employees receive the change only on their next task-first query or explicit sync.

Employee setup completion runs `company_sync_coordinator.py initial-employee` only after tenant, exact space, mapping, OAuth identity, membership, and full policy all pass. Any failure writes no mirror. The shared mirror contains only published content from the verified private company Wiki, is marked publication-excluded, and never copies an administrator's personal Vault.

Do not bypass the gate by calling internal scripts directly.

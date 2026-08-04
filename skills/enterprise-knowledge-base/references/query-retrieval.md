# Managed retrieval diagnostics

The public entry point remains `scripts/query_current_vault.py`.

1. Prefer the official Obsidian CLI for candidate recall inside approved roots.
2. Query distinct roots with bounded parallelism.
3. Fall back to `rg` when the CLI is missing, disabled, times out, or returns invalid output.
4. Fall back to the Python filesystem path only when both faster backends are unavailable.
5. Rank locally, hash the bounded evidence set, and keep the compliance audit unchanged.

Inspect these output fields when diagnosing retrieval:

- `retrieval_backend`
- `candidate_count`
- `backend_details`

Do not use a backend directly for a knowledge answer. Direct `rg` remains allowed only for deterministic inventory or structural inspection that does not produce a substantive knowledge answer.

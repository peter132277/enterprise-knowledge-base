# Internal retrieval diagnostics

Use only `scripts/query_answer_packet.py` as the public query entry. Keep `query_current_vault.py` and `check_query_compliance.py` internal.

Use `rg` for bounded candidate recall. When `rg` is missing or fails in `auto` mode, enumerate the approved Markdown roots with the Python standard library. Let Codex read the bounded candidates and synthesize the answer only after receipt and citation audit. Do not require Obsidian, its CLI, a database, an index daemon, or a background process.

Inspect `retrieval_backend`, `candidate_count`, and `backend_details` only for diagnostics. Never invoke an internal backend directly to bypass the formal answer packet.

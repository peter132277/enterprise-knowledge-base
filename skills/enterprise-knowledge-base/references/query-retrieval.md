# Internal retrieval diagnostics

The only public query entry is `scripts/query_answer_packet.py`; `query_current_vault.py` and `check_query_compliance.py` are internal components.

Candidate recall prefers the official Obsidian CLI, queries approved roots with bounded parallelism, falls back to `rg`, then uses the Python filesystem only when both faster backends fail. Inspect `retrieval_backend`, `candidate_count`, and `backend_details` when diagnosing a failure. Do not invoke a backend directly to produce a knowledge answer.

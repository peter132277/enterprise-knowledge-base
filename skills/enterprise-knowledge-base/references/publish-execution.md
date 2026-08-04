# Confirmed publication diagnostics

Load this reference only when the public `execute_publish.py` entry rejects a previewed or confirmed batch, reports a failed item, or requires transport-contract diagnosis. Routine preview and confirmation use the same entry's `preview` and `confirm` commands; `publish_batch.py`, `prepare_publish.py`, `source_link.py`, and `scan_pending.py` are internal libraries, never separate Skill entry points.

Run the internal `publish` access gate again. Reject personal content and managed mirrors at every scan, preview, confirmation, executor, audit, mapping, and convergence boundary. A personal or publication-excluded item must cause zero Wiki and Drive writes even for an administrator.

The runner owns the full transaction:

1. Revalidate the immutable latest-batch pointer, exact confirmation phrase, identity, tenant, space, mapping, membership, policy, local inputs, payload hashes, parent, and current company-mirror health.
2. Resolve each parent once. No title match creates one Docx child; one match with a verified mapping updates it; any unowned or multiple match fails as a collision.
3. Before update, compare current remote content with the last verified synchronized payload. Any manual remote difference is a conflict.
4. Reuse the exact verified Drive original from a prior Minutes stage. Otherwise upload the preserved original once to the user's Drive root, verify its name, size, URL, type, and that it is outside the Wiki tree, then journal it before continuing.
5. The internal source-link component injects one managed `### 原文件` subsection and is idempotent. Never publish a base payload that omits the preserved original.
6. Write the final UTF-8 Markdown with user OAuth. Accept `partial_success` only when a full content read-back matches.
7. Re-read title, parent, content, first and last sections, source filename and URL, document object token, reader URL, revision, and remote content hash.
8. Journal each outcome immediately. A verified success atomically finalizes the note, document mapping, collection log, queue, and batch manifest; local publisher convergence happens only after the full batch succeeds.

For rate limits or transport failures, retry the same immutable item and reuse every journaled node or Drive URL. Never create an alternate node, upload a duplicate original, downgrade a previous success, delete a remote object, change membership, or cross spaces. `transient-failure`, `collision`, `conflict`, and `failed` remain non-success states with zero local success finalization.

If a source-bearing item lacks proof that its Drive file is outside Wiki, or lacks the final linked payload hash, document token, reader URL, or read-back content hash, fail closed. Report the failed stage and preserved recovery evidence without exposing secrets, raw tokens, hashes, or internal manifest paths to the user.

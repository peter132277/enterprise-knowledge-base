# Confirmed publication diagnostics

Load this reference only when the public `execute_publish.py` entry rejects a previewed or confirmed batch, reports a failed item, or requires transport-contract diagnosis. Routine preview and confirmation use the same entry's `preview` and `confirm` commands; `publish_batch.py`, `prepare_publish.py`, `source_link.py`, and `scan_pending.py` are internal libraries, never separate Skill entry points.

Run the internal `publish` access gate again. Reject personal content and managed mirrors at every scan, preview, confirmation, executor, audit, mapping, and convergence boundary. A personal or publication-excluded item must cause zero Wiki and Drive writes even for an administrator.

The runner owns the full transaction:

1. Revalidate the immutable latest-batch pointer, exact confirmation phrase, identity, tenant, space, mapping, membership, policy, local inputs, payload hashes, parent, and current company-mirror health.
2. Resolve each parent once. No title match creates one Docx child; one match with a verified mapping updates it; any unowned or multiple match fails as a collision.
3. Before update, compare current remote content with the last verified synchronized payload. Any manual remote difference is a conflict.
4. Reuse the exact verified Drive original from a prior Minutes stage. Otherwise upload the preserved original once to the user's Drive root, verify its name, size, URL, type, and that it is outside the Wiki tree, then journal it before continuing.
5. Require the locked, timezone-aware local `imported_at` property and render it visibly as `收录时间` in the Feishu payload. The internal source-link component injects one managed `### 原文件` subsection and is idempotent. Never publish a base payload that omits the collection time or preserved original.
6. Write the final UTF-8 Markdown with user OAuth. Accept `partial_success` only when a full content read-back matches.
7. Re-read title, parent, content, first and last sections, source filename and URL, document object token, reader URL, revision, and remote content hash.
8. Journal each outcome immediately. A verified success atomically finalizes the note, document mapping, collection log, queue, and batch manifest; local publisher convergence happens only after the full batch succeeds.

For rate limits or transport failures, retry the same immutable item and reuse every journaled node or Drive URL. Never create an alternate node, upload a duplicate original, downgrade a previous success, delete a remote object, change membership, or cross spaces. `transient-failure`, `collision`, `conflict`, and `failed` remain non-success states with zero local success finalization.

For a `failed` item whose journal already contains the existing Wiki node and document object, keep recovery inside this public entry and the same immutable batch:

1. Run `execute_publish.py diagnose --batch ... --item-id ...`. It may authenticate, list the locked parent, inspect the existing Drive source, and fetch the existing document, but it must write nothing locally or remotely.
2. Compare strictly first, then use the conservative Markdown block model for headings, paragraphs, lists, tables, code, and links. Treat format-only equivalence as verified; treat raw HTML, unclosed fences, ragged tables, or unknown structures as unsupported and fail closed. Verify the source section separately.
3. Run `recover-preview` to store the action, revision, remote hash, final payload hash, and diagnosis hash under the existing item journal. Never overwrite an unresolved or changed preview.
4. Accept only exact `确认恢复本次发布` with `recover-confirm`. Re-read all evidence. A matching or format-only document performs zero remote writes and only finalizes locally. A recoverable semantic mismatch overwrites the existing object at most once, then requires complete read-back before local convergence.
5. If the overwrite result is unknown, store `outcome-unknown` and stop. The next attempt must diagnose and preview again; if read-back already converged, finalize with zero writes, otherwise do not retry blindly.

Recovery never creates a Wiki node, uploads or replaces a Drive original, changes members, changes visibility, starts another batch, or bypasses the original immutable input and mapping checks.

If a source-bearing item lacks proof that its Drive file is outside Wiki, or lacks the final linked payload hash, document token, reader URL, or read-back content hash, fail closed. Report the failed stage and preserved recovery evidence without exposing secrets, raw tokens, hashes, or internal manifest paths to the user.

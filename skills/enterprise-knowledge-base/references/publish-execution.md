# Confirmed batch diagnostics

Use this reference only after the user confirms the current immutable batch and the deterministic runner rejects the batch, returns a failed item, or requires CLI-contract diagnosis. A routine successful run should not load this file.

Run the `publish` access gate again before confirmation, execution, retry, or recovery. Reject any item whose current note or immutable manifest is not explicitly `scope: enterprise`; administrators cannot override this content boundary. Managed mirror files and personal content must cause zero Wiki or Drive writes. After a fully verified success, the runner records publisher convergence locally; it does not push to other employees in the background.

## Load current Feishu rules once

```powershell
lark-cli skills read lark-shared
lark-cli skills read lark-drive
lark-cli skills read lark-wiki references/lark-wiki-node-create.md
lark-cli skills read lark-doc references/lark-doc-update.md
lark-cli skills read lark-doc references/lark-doc-md.md
lark-cli skills read lark-doc references/lark-doc-fetch.md
lark-cli skills read lark-doc references/style/lark-doc-style.md
```

Verify `lark-cli --as user` once. Resolve each distinct verified parent once and list its children once.

Run the confirmed immutable batch with:

```powershell
python <skill-root>/scripts/execute_publish.py `
  --batch "<manifest-relative-path>"
```

The runner owns the sequence below, uses UTF-8 subprocess capture instead of a PowerShell JSON pipeline, accepts `partial_success` only when final read-back matches, and finalizes all local state transactionally. Treat the remaining steps as its fail-closed contract and manual diagnostic reference, not as instructions to re-orchestrate a normal batch.

## Publish each item

1. Re-run preparation and require every hash to match the confirmed manifest.
2. Check the exact title under the resolved parent:
   - no match: create one `docx` child;
   - one match plus matching local mapping: update;
   - match without mapping or multiple matches: block as a collision.
3. Before an update, compare current Feishu content with the last synchronized payload. Treat any remote difference as a conflict.
4. Upload the preserved original only when it exists and has no verified Feishu source URL. Use SHA-256 to prevent duplicate uploads.
   - A pending note may carry a `source_feishu_file_url` produced by the confirmed Feishu Minutes transcription route. The immutable batch locks it as `source_preuploaded_url`; inspect and verify that URL instead of uploading again.
   - Upload to the caller's Feishu Drive root with `drive +upload`.
   - Do not pass `--wiki-token` or `--folder-token`; the original must not become a Wiki-tree node.
   - Inspect the returned URL and require a readable object of type `file`.
   - Query the file token with `wiki +node-get --obj-type file` against the enterprise space. Require error code `131005` (`document is not in wiki`). A successful node lookup means the file is visible in Wiki and must not be reused.
   - If an old mapping resolves to a Wiki node, block normal publication. Upload a new Drive-root copy only within the authorized publish scope; removing the old Wiki node requires explicit repair or deletion authority.

```powershell
lark-cli drive +upload `
  --as user `
  --file "<relative-source-path>" `
  --name "<original-filename>"
```

5. Inject the verified Drive file URL into the source section and write a separate final payload:

```powershell
python <skill-root>/scripts/source_link.py inject `
  --payload "<base-payload-relative-path>" `
  --source "<relative-source-path>" `
  --source-name "<uploaded-original-filename>" `
  --source-url "<verified-feishu-file-url>"
```

The injector is authoritative for the managed `### 原文件` subsection. It removes obsolete local-path-only source lines, preserves other provenance, and is idempotent. Never publish the base payload when a preserved original exists.

6. Write the final UTF-8 payload from a path relative to the Vault:

```powershell
lark-cli docs +update `
  --command overwrite `
  --doc-format markdown `
  --content @<final-payload-relative-path>
```

7. Require `ok: true`, `result: success`, and no material warning.
8. Fetch the document and verify its title, readable Chinese, first and last sections, and parent.
9. Verify that the source section visibly contains the exact original file name and Drive URL:

```powershell
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

lark-cli docs +fetch `
  --doc "<document-url-or-token>" `
  --doc-format markdown `
  --detail simple `
  --format json |
python <skill-root>/scripts/source_link.py verify `
  --source "<relative-source-path>" `
  --source-name "<uploaded-original-filename>" `
  --source-url "<verified-feishu-file-url>"
```

Also use `drive +inspect` to verify the file name, size, URL, and type. The Drive file has no Wiki parent by design.

Record each item immediately:

```powershell
python <skill-root>/scripts/publish_batch.py record `
  --batch "<manifest-relative-path>" `
  --item-id "<item-id>" `
  --outcome success `
  --verified `
  --node-token "<verified-node-token>" `
  --source-file-url "<verified-source-url-if-any>" `
  --source-outside-wiki-verified `
  --final-payload-hash "<hash-returned-by-source-link-inject>" `
  --obj-token "<verified-document-object-token>" `
  --feishu-url "<verified-document-url>" `
  --last-synced-remote-hash "<verified-remote-content-hash>"
```

A source-bearing item cannot be recorded as success without the verified source file URL, proof that the file is outside Wiki, and the final linked payload hash. A success record also requires the document object token, reader-facing URL, and verified remote content hash. The command atomically finalizes the source note, mapping, collection log, queue, and batch manifest; never patch them separately.

Use `transient-failure`, `collision`, `conflict`, or `failed` for non-success outcomes and add a short `--reason`. The journal preserves any node token or source URL already created.

For a rate limit or transport error, retry the same immutable item from its journal. Reuse an existing node token or source URL; never create a different node or upload a duplicate source. A previously verified success is idempotent and cannot be replaced by a later failure.

# Public web capture

Use this reference only for public HTTP(S) links.

1. Extract raw URLs and Markdown-link targets.
2. Remove fragments and known tracking parameters such as `utm_*`; retain meaningful query parameters.
3. Use `agent-reach` to inspect the source. For GitHub, resolve the default branch, commit, and relevant subtree. For documentation sites, prefer the declared official repository source.
4. Reject local, private-network, credential-bearing, unreadable, truncated, or unsupported URLs.
5. Create deterministic evidence under `.kb/temp/`.

For one page:

```powershell
python <skill-root>/scripts/ingest_pipeline.py capture-link `
  --vault <current-vault> `
  --url "<url>" `
  --output-dir ".kb\temp\link-capture"
```

For an official GitHub documentation subtree:

```powershell
python <skill-root>/scripts/ingest_pipeline.py capture-github-subtree `
  --vault <current-vault> `
  --repo "owner/repository" `
  --ref "<branch>" `
  --subpath "<language-or-docs-directory>" `
  --source-url "<user-url>" `
  --output-dir ".kb\temp\link-capture"
```

6. Run `ingest_pipeline.py preflight` on the capture. Keep the extraction under `.kb/temp/` until commit and record its path and SHA-256 in the manifest.
7. Record original, canonical, and final URLs or repository/ref/commit; capture time; coverage; and limitations.
8. Preserve a multi-page corpus as one deterministic snapshot with its hierarchy, one concise corpus index, and one knowledge/index note. Do not merge the site into one giant Markdown file or create hundreds of top-level knowledge notes.
9. After committing a repository archive, use `ingest_pipeline.py expand-web-corpus` in preview and then write mode. Its internal expander requires archive and content-tree hashes, creates a backup, and updates `.kb/state/web_corpora.json`.
10. For deterministic renamed-title, translation, or ambiguous Wiki-link errors, use `ingest_pipeline.py repair-web-corpus` with reviewed rules. Keep the ZIP unchanged and preserve explicit extensions for non-Markdown attachments.
11. Third-party documentation saved as private reference defaults to `personal`. Do not queue it for enterprise publication without an explicit enterprise request and redistribution review.

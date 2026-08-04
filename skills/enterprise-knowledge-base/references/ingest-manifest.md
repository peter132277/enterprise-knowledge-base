# Ingest manifest

Write one temporary JSON manifest under `.kb/temp/`:

```json
{
  "schema_version": 1,
  "route": "enterprise",
  "classification_review": {
    "phase": "post-extraction-semantic-pass",
    "route": "enterprise",
    "filename_used": false,
    "additional_confirmation": false,
    "evidence": [
      {"locator": "00:04:12.000–00:04:38.000", "reason": "Reusable operating procedure"}
    ]
  },
  "source_note": "20_知识/企业/03_业务流程/标题.md",
  "source": {
    "path": "<absolute-input-source-path>",
    "sha256": "<preflight-sha256>",
    "stored_original": "10_来源/原件/文档/YYYY-MM-DD_源文件.txt",
    "extraction": "10_来源/提取/文档正文/YYYY-MM-DD_源文件.md",
    "copy_source_to_extraction": false,
    "extraction_source_path": "<absolute-path-under-vault-.kb/temp>",
    "extraction_sha256": "<sha256-of-utf8-markdown-extraction>",
    "extraction_profile": {
      "source_kind": "pdf",
      "method": "native-text",
      "provider": "bundled-runtime",
      "locator_type": "page",
      "units_total": 12,
      "units_processed": 12,
      "complete": true,
      "review_required": false,
      "warnings": []
    }
  },
  "metadata": {"processed_at": "YYYY-MM-DD"},
  "outputs": [
    {"path": "20_知识/企业/03_业务流程/标题.md", "mode": "create", "content": "<source-note>"},
    {"path": "20_知识/原子/方法/方法.md", "mode": "create", "content": "<atomic-note>"},
    {
      "path": "30_导航/主题/企业主题/主题.md",
      "mode": "append_once",
      "heading": "## 核心资料",
      "content": "- [[20_知识/企业/03_业务流程/标题]]"
    },
    {"path": ".kb/logs/收录/YYYY-MM-DD_源文件.md", "mode": "create", "content": "<log>"}
  ]
}
```

Prefer `append_once` for an existing index. Use `replace` only with the current file SHA-256 in `expected_sha256`.

TXT and Markdown may set `copy_source_to_extraction: true` and omit `extraction_profile`. Word, PDF, image, audio, and video sources must use a separate UTF-8 Markdown extraction under `.kb/temp`, provide its SHA-256, and include a complete profile. For audio and video, preserve evidence emitted by `prepare_media_transcript.py`: approved provider, timestamps, provider reference and engine, duration, source hash, transcript hash, normalization state, and provider-specific evidence. Also require `classification_review` after extraction: its route must equal the manifest route, it must reject filename evidence and extra confirmation, and every evidence locator must occur in the verified extraction. `feishu-minutes` additionally requires its verified Drive file URL and Minute URL; put the same Drive URL in source-note Frontmatter as `source_feishu_file_url`. `local-whisper-cpp` requires model and runtime hashes and must not set a remote source URL. An audio sidecar is commit-ready; a video sidecar remains incomplete until scene-change/key-slide evidence is merged.

Enterprise source notes must include:

```yaml
status: ready-to-publish
scope: enterprise
review_status: reviewed
sensitivity: internal
publish_status: pending
publish_to_feishu: false
feishu_writer: lark-cli
feishu_space_name: <verified-space-name-from-.kb/mappings/feishu_nodes.json>
feishu_parent_node_name: "resolved category node"
```

Store `source_path` in note Frontmatter as the Vault-relative preserved original path under `10_来源/原件`, never as a machine-specific absolute Vault path.
Require `feishu_space_name` to equal the non-empty `space_name` in the current Vault's verified node mapping. Never copy a space name from an example or another project.

Use `review_status: needs-verification` when a complete extraction still requires human review, and set `extraction_profile.review_required: true`. Such notes remain local and do not enter the publication queue. Never commit materially incomplete extraction or place `restricted` or `personal-sensitive` notes in the queue.

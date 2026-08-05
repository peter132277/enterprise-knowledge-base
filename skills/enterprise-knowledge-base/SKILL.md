---
name: enterprise-knowledge-base
description: Initialize, query, collect, synchronize, and safely publish an enterprise knowledge Vault opened as the current Codex project. Use for `初始化当前知识库`, any attachment, current-Vault questions, media transcription, health checks, company sync, and explicit Feishu publication.
---

# Enterprise knowledge base

Use this Skill as the only visible entry. Keep initialization, query, collection, media, membership, and publication as separate internal safety routes.

## Initialize the current project

For `初始化当前知识库` or missing configuration, read [setup-workflow.md](references/setup-workflow.md). Inspect only the current working directory, show the preview, ask for the role, then run the confirmed current-project initializer. Never request `--vault`, an absolute path, a download, or a system-software installation.

Treat Codex as the only AI and automation runtime. Read and write Vault Markdown and attachments directly. Treat Obsidian only as an optional human browser/editor; never require its CLI, modify `.obsidian`, install a community plugin, or configure Codex through Obsidian.

Show the role choices exactly:

- `我是飞书管理员，首次为公司部署`
- `我是普通员工，加入公司已有知识库`
- `暂时只使用本地知识库`

## Route normal work

Before normal query, collection, media, sync, preview, confirmation, retry, or publication, read [runtime-access.md](references/runtime-access.md). Setup inspection and local health diagnostics are exceptions.

The only public script entries are `setup_wizard.py`, `query_answer_packet.py`, `ingest_pipeline.py`, `prepare_media_transcript.py`, `company_sync_coordinator.py`, `execute_publish.py`, `check_vault_health.py`

- Treat an attachment-only message as a complete collection request. Route audio/video through [media-transcription.md](references/media-transcription.md); route other attachments through collection.
- For any normal question, follow [query-workflow.md](references/query-workflow.md). Use fixed internal `scope: all` across personal and enterprise knowledge without asking for a range. Answer only after query, sync-credential, and citation audits pass.
- For a file, local path, public link, `收录`, `整理`, or saved text, read [capture-modes.md](references/capture-modes.md); read [adaptive-processing.md](references/adaptive-processing.md) only when needed. Never contact Feishu from the collection route.
- For `发布到飞书`, create the immutable read-only preview immediately and ask once for `确认批量发布`.
- Only exact `确认批量发布` authorizes the latest immutable batch. Never extend it to another batch, space, upload, deletion, or permission change.
- For `检查知识库健康度`, use `check_vault_health.py` on the current bound Vault.

## Preserve local transaction boundaries

Run one collection preflight, one semantic pass, and one transactional commit. Preserve originals under `10_来源`, reusable notes under `20_知识`, navigation under `30_导航`, and state under `.kb`. Use SHA-256 plus the managed Vault path as source identity.

Default media to one explicitly confirmed Feishu Minutes transcription. Use an already installed local `whisper.cpp` only when explicitly requested. Never install a model, converter, FFmpeg, or background service. Keep transcription upload confirmation separate from Wiki publication confirmation. Reuse the exact Drive original during later publication.

## Preserve Feishu safety boundaries

Publish only reviewed enterprise notes. Show readable title, destination, and original upload/reuse status. Revalidate identity, mapping, inputs, parent, remote revision, source file, and read-back content. Stop on collisions, remote edits, wrong spaces, permission failures, or incomplete evidence. Use `execute_publish.py`; never reconstruct its lark-cli sequence.

Keep knowledge-space membership separate from Skill policy. Resolve membership read-only, show a human preview, require fresh exact confirmation, apply only the managed member plan, then read every member page back. Keep normal employees as members, external sharing closed, and company policy fixed to query, collect, and publish.

Treat lark-cli as a conditional dependency only when Feishu features are enabled. Do not contact Feishu during local initialization or ordinary local collection.

## Keep one company mirror and strict personal isolation

Use `company_sync_coordinator.py before-query` once before the first all-knowledge query in each Codex task when company access is configured; use `initial-employee` after employee verification, `explicit` for user-requested sync, and local convergence for the publisher after verified publication. Reuse the verified task credential afterward with zero remote reads and no full-mirror rehash. No background push or polling is allowed.

Never overwrite a locally edited mirror, accept a disappeared remote node, cross spaces, or sync unsupported nodes. Keep managed mirrors publication-excluded.

Keep personal knowledge purely local for every role. Require `scope: personal` and `publish_to_feishu: false`; block personal content from queue, preview, confirmation, executor, Drive, Wiki, mapping, shared mirror, and sync state at every layer.

## Discover the Vault safely

Resolve only the nearest current-directory ancestor containing `AGENTS.md`, `.kb`, and a valid same-root binding. Reject every explicit path or `KB_VAULT_ROOT` redirect that differs from the active bound project. Never derive the data Vault from the installed plugin path.

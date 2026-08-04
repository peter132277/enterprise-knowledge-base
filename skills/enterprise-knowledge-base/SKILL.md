---
name: enterprise-knowledge-base
description: Install, configure, query, collect, and publish a local Codex and Obsidian enterprise knowledge base. Use for first-time setup, any attachment even without written instructions, current-Vault questions, media transcription, health checks, and explicit Feishu publication.
---

# Enterprise knowledge base

Use this Skill as the only visible and implicit entry for the configured knowledge Vault. Keep query, collection, media, and publication as separate internal safety routes.

## Start or resume setup

For `开始安装企业知识库`, `继续安装`, missing Vault configuration, or an environment failure, read [setup-workflow.md](references/setup-workflow.md). Plan the OS Documents/知识库 default read-only, then let `setup_wizard.py` install or reuse Obsidian, create the same-root Vault and project-only binding, configure verified Claudian with the local Codex CLI, and open Obsidian after one local confirmation. Ask only for an unavoidable OS prompt or one Codex `打开文件夹` action. Never ask the user to download, create, type a path, or copy a command, JSON, hash, ID, token, secret, or URL.

The first role choice must be exactly:

- `我是飞书管理员，首次为公司部署`
- `我是普通员工，加入公司已有知识库`
- `暂时只使用本地知识库`

Use `为本公司创建企业自建应用` without any recommendation suffix.

Define the enterprise knowledge base as a Feishu administrator deployment shared only with company-internal employees inside the administrator's explicitly authorized scope. Application installation, company configuration import, and employee OAuth never grant knowledge-space access by themselves.

Configure exactly `全公司内部员工` with `查询、收录并发布`. Do not create editor subgroups or weaker employee policies. If the whole-company scope cannot be resolved to one verified internal organization root, fail closed and require the administrator to repair directory visibility or authorization; never narrow the company silently.

## Route normal work
Before every normal query, collection, media, sync, preview, confirmation, retry, or publication action, read and follow [runtime-access.md](references/runtime-access.md). Setup inspection and local health diagnostics are the only exceptions.
The only public script entries are `setup_wizard.py`, `query_answer_packet.py`, `ingest_pipeline.py`, `prepare_media_transcript.py`, `company_sync_coordinator.py`, `execute_publish.py`, and `check_vault_health.py`; all other scripts are internal components.

- Attachment-only message: treat it as a complete collection request. Route audio and video through [media-transcription.md](references/media-transcription.md); route every other attachment through the local collection workflow below.
- Factual, explanatory, comparative, summary, recommendation, or lookup question: follow [query-workflow.md](references/query-workflow.md). Every direct question uses fixed internal `scope: all` to search all personal and enterprise knowledge without a command prefix or a knowledge-type choice. Answer only after the current-Vault query and sync receipts plus citation audit pass.
- File, local path, public link, `收录`, `整理`, `处理`, or saved text: read [capture-modes.md](references/capture-modes.md), then [adaptive-processing.md](references/adaptive-processing.md) only when the source requires it. Never contact Feishu from the collection route.
- Clear publication intent such as `批量发布`, `发布飞书`, or `发布到飞书`: create the read-only immutable preview immediately using the publication scripts. Ask once for `确认批量发布`.
- `确认批量发布` or exact-batch recovery: follow the publication contract below. Never extend that authorization to another batch, space, upload, deletion, or permission change.
- Publication-state inspection without publication intent: use the health entry's local publication audit; do not contact Feishu.
- `检查知识库健康度`: run `check_vault_health.py --vault <current-vault>`.
- Explicit public-web research: keep it separate. Do not save or mix its findings without a separate collection request.

## Preserve the internal collection boundary

Run one `ingest_pipeline.py preflight`, one semantic pass, and one transactional `commit`. Preserve the original under `10_来源`, reusable notes under `20_知识`, navigation under `30_导航`, and state under `.kb`.

Use SHA-256 plus the managed Vault path as source identity. Treat the original filename only as a display value. Ask one question only when the source is unreadable or file-level personal versus enterprise classification remains genuinely ambiguous.

Never log in to Feishu, list Wiki nodes, upload a file, or create a publication preview from the collection route. A router-prepared Feishu Minutes profile is input evidence, not collection authority.

## Preserve the media boundary

Default to one explicitly confirmed Feishu Minutes transcription using the current Vault's exact verified space mapping. Use an already installed local `whisper.cpp` only when the user explicitly requests offline processing. Never install a model, converter, FFmpeg, or background service from the media route.

The transcription upload confirmation and later Wiki publication confirmation are independent. Reuse the exact Drive original from the Minutes stage during publication.

## Preserve the publication boundary

Include only reviewed, eligible enterprise notes. Show a readable preview containing title, destination, and original upload/reuse status. Keep batch IDs, manifest paths, hashes, tokens, and implementation labels internal.

Only exact `确认批量发布` authorizes the most recently previewed immutable batch. Revalidate identity, mapping, inputs, parent, remote version, source file, and read-back content. Stop on collisions, remote manual edits, wrong spaces, permission failures, or incomplete evidence.

Use the deterministic `execute_publish.py` runner. Never recreate its lark-cli sequence manually. Reuse journaled nodes and Drive files, and finalize local state only after verified read-back.

## Preserve the membership boundary

Keep Feishu knowledge-space membership separate from the Skill's query, collect, and publish policy. A normal employee may only have the space `member` role and may never raise the company policy. Keep external sharing closed and reject external identities.

Resolve membership targets read-only, then show a human preview with scope, member role, employee policy, and external-sharing state. Hide IDs, tokens, hashes, and secrets. Member changes are a separate remote write and require exact fresh confirmation. After a confirmed managed change, re-read member-list and verify scope coverage, internal-only membership, normal-employee roles, mapping, and policy before recording completion or exporting company configuration.

Use `feishu_company_adapter.py` as the only member transport. It resolves the verified organization root with user identity, skips an already matching grant, applies only the immutable `member` grant after exact confirmation, reads every member page, and verifies a private team space with external sharing closed. Never reconstruct its lark-cli commands manually.

## Keep one shared company knowledge layer
Treat the verified private Feishu Wiki as the shared enterprise publication layer and the current Vault as the audited local working copy. The query entry calls `company_sync_coordinator.py before-query` once per Codex task before its first all-knowledge query; employee setup uses `initial-employee`, and `同步公司知识` uses `explicit`. It reads the exact mapped Wiki with user identity and mirrors only published Docx content under `20_知识/企业/共享镜像` before local retrieval. Same-task later queries use zero remote reads and no full-mirror rehash.

Never overwrite a locally edited mirror, silently accept a disappeared remote node, cross spaces, or sync an unsupported node type. Managed mirrors are publication-excluded and never re-enter the queue. Reuse an existing verified local publication as authoritative on the publishing device to avoid duplicate query evidence. Collection remains transactional locally; publication still requires its own immutable preview and `确认批量发布`. After verified publication, update the publisher's authoritative and sync state locally; other employees receive it only on their next task-first query or explicit sync. No background push or polling is allowed.

Personal knowledge is purely local for administrators and employees alike. Every personal note, attachment, document, and media-derived note must use `scope: personal` and `publish_to_feishu: false`; it may not enter the queue, preview, confirmed manifest, executor, mapping, shared mirror, or company sync state. Role never overrides this content boundary.

Read [destination-rules.md](references/destination-rules.md) only for an unresolved category. Read [publish-execution.md](references/publish-execution.md) only after a failed confirmed item.

## Discover the Vault safely

Resolve the Vault only as the nearest current-directory ancestor containing `AGENTS.md`, `.kb`, and a valid same-root `.kb/config/project-binding.json`. A supplied path or `KB_VAULT_ROOT` is valid only when it equals that bound current project; it may never redirect access elsewhere. Never derive the data Vault from the globally installed plugin path. Fail closed when the binding is absent, moved, contradictory, or outside the active project.

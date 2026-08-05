# Current-project setup workflow

Keep setup conversational and local-first. Never ask for a Vault path, command, token, node ID, JSON value, App Secret, or authorization URL.

## 1. Inspect the current Codex project

Run `setup_wizard.py inspect-current-project` from the current project root. Do not pass `--vault` or change the working directory.

Accept an empty project, a project containing only `.obsidian`, or a Vault with ordinary notes and attachments. Preserve every pre-existing ordinary file byte-for-byte. Treat `.obsidian` and legacy `.claudian` as user-owned: never create, modify, delete, scan for runtime readiness, or depend on them.

Reuse a valid v0.4.1 `.kb/config/project-binding.json` same-root binding and `.kb` state. Update `AGENTS.md` only when it exactly matches a known official template. If it was edited by the user, stop and explain the conflict. Recognize the exact old three-Skill layout; after confirmation, back it up under `.kb/legacy-skill-backup/v0.4.1` before activating the single Skill.

Fail closed on a non-directory reserved path, malformed or unrecognized `.kb`, unknown `AGENTS.md`, a managed file represented by a directory, or any planned overwrite. Do not delete, rename, or repair a conflicting path automatically.

## 2. Select the role and confirm initialization

Show exactly:

- `我是飞书管理员，首次为公司部署`
- `我是普通员工，加入公司已有知识库`
- `暂时只使用本地知识库`

Show the readable preview and ask once to initialize this already-open project. This authorizes only local managed directories, the same-root binding, the official project rules, and missing `.kb` defaults. It never authorizes network access, system software installation, `.obsidian` changes, Feishu access, publication, member changes, permission changes, or deletion.

Run `setup_wizard.py initialize-current-project --role <role> --yes` with the current project as `cwd`. The command accepts no external Vault path. Require `network_requests: 0`, `system_software_installations: 0`, and `obsidian_writes: 0` in the result.

Codex is the runtime and reads/writes Markdown and attachments directly. Obsidian is only an optional human interface installed and opened by the user according to the repository manual. Do not require Obsidian or its CLI to finish initialization.

## 3. Configure Feishu only when enabled

For local-only role, stop after local health checks. Keep publication and company sync unavailable until a separately requested Feishu setup.

If `lark-cli` is absent for an administrator or employee flow, offer `安装飞书配置工具` or `暂不配置飞书`. Run `setup_wizard.py install-lark-cli --yes` only after the first choice. Node/npm and lark-cli are conditional Feishu dependencies, not local initialization requirements.

### Administrator

Offer `为本公司创建企业自建应用`, `连接已存在的企业应用`, or `安装跨企业正式版应用` without recommendation labels. Complete administrator OAuth in the in-app browser and never request or display the App Secret in chat.

After OAuth, offer exactly `新建飞书知识库` or `连接已有飞书知识库`; do not confuse this choice with creating a Feishu application.

For `连接已有飞书知识库`, list every accessible space with user identity, select one exact private team space, and build mappings only from re-read verified nodes.

For `新建飞书知识库`, require only the missing user scopes `wiki:space:retrieve` and `wiki:space:write_only`; never broaden scopes automatically. Ask for a readable space name and optional description. Run `setup_wizard.py preview-space-create --vault . --name <name> [--description <text>]`, show its preview, and wait for exact `确认创建知识空间`. Only then run `setup_wizard.py apply-space-create --vault . --confirmation 确认创建知识空间`. Require administrator user OAuth, a private team-space response, closed external sharing, exact name/description, and complete space readback before recording the empty verified mapping. A failed or unknown create outcome must never be retried automatically.

Knowledge-space creation never authorizes membership, node, publication, deletion, or sharing writes. Application setup and OAuth do not grant space membership. Space creation also grants no company membership.

After a new space is verified, require only the missing user scopes `wiki:node:retrieve` and `wiki:node:create` for the packaged `Obsidian企业知识库` structure. Run `setup_wizard.py preview-wiki-template --vault .`, show all seven empty structural nodes, and wait for exact `确认初始化知识库模板`. Only then run `setup_wizard.py apply-wiki-template --vault . --confirmation 确认初始化知识库模板`. The template contains five numbered root pages plus `91｜数据索引` and `98｜同步记录` under `00｜知识库首页`; it contains no live IDs, business documents, sample content, publication, or member writes. Require an empty new space, stable administrator identity, per-node journal, complete recursive readback, and exact topology before recording verified node mappings. Recover an unknown node write only when fresh readback identifies exactly one matching pending node; otherwise fail closed without retry. Do not offer this initializer for a connected existing space.

Template initialization never authorizes membership or publication. Only after its complete readback may the separate company-membership preview begin.

Resolve and verify the internal organization root representing `全公司内部员工` first. When that root is unavailable or cannot be resolved uniquely, use the same foreground membership transaction to traverse the complete all-employees directory and manage only active, joined, non-frozen, non-resigned internal user principals. Incomplete pages, a narrowed directory scope, contradictory users, or an empty eligible roster fail closed. Never make the space public as fallback.

Generate one readable member preview with strategy, eligible/excluded counts, and add/remove counts but no IDs, tokens, hashes, or secrets. Remove only principals recorded by a previous successful managed-user transaction; preserve manual members and every administrator. Require exact fresh `确认更新知识空间成员`; otherwise write nothing. Keep an unresolved receipt after a partial or unknown outcome, refuse a replacement preview, retry only the same immutable plan, and converge each operation by readback. After the managed change, read every member page and verify scope, roles, internal-only membership, mapping, policy, and closed external sharing.

Export only `kb-company-config/v4` after readback succeeds. Exclude App Secret, tokens, cookies, employee content, exact managed IDs, and full employee rosters.

### Employee

Import only a validated `kb-company-config/v4`. Reject secrets, tokens, cookies, executable content, employee content, full rosters, unsupported strategies, and policies other than `members` for all employees.

Complete OAuth with the employee's identity, then verify tenant, exact space, mappings, current membership version/hash, authorized scope, `成员` role, and effective policy. External, out-of-scope, invisible, stale, or contradictory evidence fails closed. After verification, run the existing first foreground company sync transaction; roll back completion if it fails.

## 4. Finish

Run role-aware environment and Vault-health checks. Report the current project/Vault root, same-root binding, local readiness, preserved pre-existing content, and Feishu identity/space readiness only when enabled. Do not perform a test upload or Wiki write.

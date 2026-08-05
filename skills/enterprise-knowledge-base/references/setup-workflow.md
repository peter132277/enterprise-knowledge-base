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

List spaces with user identity, select one exact private space, and build mappings only from re-read verified nodes. Application setup and OAuth do not grant space membership.

Resolve exactly one verified internal organization root representing `全公司内部员工`. Fix the Skill policy to `查询、收录并发布`, keep normal employees as `成员`, and keep external sharing closed. Missing, ambiguous, external, narrowed, or contradictory scope evidence fails closed.

Generate a readable member preview with no IDs, tokens, hashes, or secrets. Require exact fresh `确认更新知识空间成员`; otherwise write nothing. After the managed change, read the complete member list and verify scope, roles, internal-only membership, mapping, policy, and external-sharing state before recording completion or exporting company configuration.

Export only `kb-company-config/v3` after read-back succeeds. Exclude App Secret, tokens, cookies, employee content, and full employee rosters.

### Employee

Import only a validated `kb-company-config/v3`. Reject secrets, tokens, cookies, executable content, employee content, full rosters, unsupported scopes, and policies other than `members` for all employees.

Complete OAuth with the employee's identity, then verify tenant, exact space, mappings, current membership version/hash, authorized scope, `成员` role, and effective policy. External, out-of-scope, invisible, stale, or contradictory evidence fails closed. After verification, run the existing first foreground company sync transaction; roll back completion if it fails.

## 4. Finish

Run role-aware environment and Vault-health checks. Report the current project/Vault root, same-root binding, local readiness, preserved pre-existing content, and Feishu identity/space readiness only when enabled. Do not perform a test upload or Wiki write.

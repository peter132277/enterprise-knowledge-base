# Guided setup workflow

Keep setup conversational. Show one decision at a time and never ask a novice to copy a command, token, node ID, JSON value, App Secret, or authorization URL.

## 1. Inspect before changing anything

Resolve the current Codex project root and run `setup_wizard.py inspect --vault <current-project-root>`. The current Codex project is the only allowed Vault candidate. Show:

- `使用当前 Codex 项目`

Do not create a Vault elsewhere, accept an external Vault path, or use `KB_VAULT_ROOT` to redirect runtime access. If the current project is an unrelated non-empty directory, stop and ask for the single Codex UI action `打开要作为知识库的文件夹`; do not claim that Codex saved-project registration was automatic. Resume only after that folder is the active Codex project.

## 2. Select the role

Show exactly:

- `我是飞书管理员，首次为公司部署`
- `我是普通员工，加入公司已有知识库`
- `暂时只使用本地知识库`

Initialize only after the user selects a role. Use `setup_wizard.py initialize`; it writes a same-root project binding under `.kb/config/project-binding.json`. Preserve an existing compatible Vault and never overwrite an unrelated non-empty directory. Every runtime route must resolve the nearest bound current project and reject a different explicit path or environment override.

## 3. Reuse the configured desktop or complete new-machine setup

Treat Obsidian, Claudian, and the Claudian Codex provider as one new-machine setup capability. Inspect them read-only first. When `inspect` reports the existing Claudian integration as ready, reuse it and do not show an installation choice, download assets, rewrite its settings, or reinstall anything.

If Obsidian is absent, show:

- `自动安装并打开`
- `暂不安装，稍后再说`

Software installation requires the user's explicit selection. Run `setup_wizard.py install-obsidian --yes` only after `自动安装并打开`. On Windows prefer the verified Winget package `Obsidian.Obsidian`. On macOS prefer an existing Obsidian app, then Homebrew cask `obsidian` when Homebrew is already available. If the platform package manager is unavailable, open the official download page in the in-app browser. Do not install Homebrew as a hidden prerequisite.

On a new or incomplete machine, the same `自动安装并打开` selection also authorizes the required local Claudian setup. Run `setup_wizard.py install-claudian --vault <current-project-root> --yes`. Reuse an existing compatible `YishenTu/claudian` installation first; install the pinned SHA-256-verified release only when absent. The required plugin ID is `realclaudian`. Enable it in `.obsidian/community-plugins.json`, detect the real local Codex CLI executable, and preserve or update only the Codex provider path in `.claudian/claudian-settings.json`. Do not install a similarly named plugin, accept a hash mismatch, overwrite unrelated plugin files, or report success when the Codex CLI cannot be found.

Then run `setup_wizard.py open-obsidian --vault <current-project-root>`. The command must reject a missing or mismatched project binding. The Obsidian URI opens the already-bound Codex project as its Vault; it does not register a separate Codex project.

## 4. Configure Feishu as an administrator

If `lark-cli` is absent, offer `安装飞书配置工具` or `暂不配置飞书`. Run `setup_wizard.py install-lark-cli --yes` only after the first choice. This uses the existing Node/npm runtime on both Windows and macOS; if Node/npm is absent, stop and offer a separately confirmed Node LTS installation or local-only setup.

Show:

- `为本公司创建企业自建应用`
- `连接已存在的企业应用`
- `安装跨企业正式版应用`

Do not add recommendation labels.

For a self-built app, run `lark-cli config init --new` as a background operation, extract its setup URL, generate the required QR code, and open the exact URL in the Codex in-app browser. The administrator completes all developer-console and approval actions personally. Never request or display the App Secret in chat.

After application configuration, request only the user domains needed by enabled features: docs, drive, wiki, and minutes. Initiate `lark-cli auth login --no-wait --json`, open the returned URL in the in-app browser, and complete `--device-code` only after the user says authorization is complete. Do not cache authorization URLs or device codes.

List spaces with user identity and show readable exact names. Offer existing-space selection or a separately confirmed standard-space creation. Build `.kb/mappings/feishu_nodes.json` only from re-read, verified remote nodes.

Application setup and OAuth are not knowledge-space membership. After selecting or creating the exact space, set the sharing range to `全公司内部员工`. Resolve the actual organization root read-only. It is valid only when exactly one verified internal organization root represents all employees. If resolution is missing, ambiguous, or unsupported for the current identity, stop and require the administrator to repair directory visibility or authorization. Never narrow the range, treat app installation as access, guess a root, enable external sharing, or make the space public.

Set the employee policy to `查询、收录并发布` for every internal employee. Do not create editor departments, group exceptions, employee exceptions, or administrator-only publication policy.

Use `membership_policy.py` to create a human preview showing only sharing range, readable target names, `成员` role, employee feature policy, and `外部分享：关闭`. Do not display internal IDs, tokens, hashes, or secrets.

The member change is an independent real remote write. Ask separately for `确认更新知识空间成员`; without that exact fresh confirmation, perform zero writes. After confirmation, only a managed member adapter may apply the immutable plan. Immediately re-run member-list and verify exact authorized bindings, normal employees as members rather than administrators, no external accounts, external sharing closed, and unchanged space mapping and employee policy. A mismatch fails closed and setup remains incomplete.

Any space creation, member change, application approval, or permission change is a separate real remote write and requires explicit confirmation.

Only after membership, mapping, and policy all pass read-back verification, record the compact verification snapshot and create the shareable configuration with `setup_wizard.py export-company`. It contains the app ID, exact verified space mapping, verified scope selectors, effective employee policy, minimum plugin version, and membership version/hash. It never contains an App Secret, token, Cookie, employee content, or a complete employee roster.

## 5. Configure Feishu as an employee

Show:

- `使用公司配置文件`
- `联系管理员完成企业应用配置`

Import only a `kb-company-config/v3` document through `setup_wizard.py import-company`. Reject secrets, access tokens, refresh tokens, cookies, employee content, full employee rosters, and unknown executable content.

Validate the exact portable shape in [company-config-schema.md](company-config-schema.md). Version 3 accepts only `all-employees` with effective policy `members`; department, group, user, administrator-only, and query-only variants fail closed.

The company App Secret must already be provisioned securely on the device by an administrator or enterprise device-management system. A pure local self-built-app deployment cannot make that secret disappear. Do not ask the employee to paste it into Codex.

Import does not complete setup. Initiate employee OAuth in the in-app browser, then verify the exact tenant, space visibility, directory mapping, current membership version/hash, whether the employee is inside the administrator-authorized scope, the `成员` role, and the effective policy. External accounts, out-of-scope employees, invisible spaces, stale membership evidence, or policy contradictions fail closed. Never let an employee change `share_scope`, change space roles, or increase the administrator policy.

After every employee verification passes, `setup_wizard.py verify-employee` performs the first complete foreground company sync through `company_sync_coordinator.py initial-employee`. If identity, membership, mapping, policy, Wiki read, or local transaction validation fails, setup remains incomplete and no managed mirror is written.

## 6. Local-only mode

Create and validate the bound Vault, Obsidian, Claudian, and Codex CLI path without installing lark-cli or creating Feishu mappings. Keep media on the explicit offline path and keep publication unavailable until an administrator configures Feishu later.

## 7. Finish

Run environment, Vault-health, and publication-state checks appropriate to the enabled mode. Report only readable results:

- Codex project location
- Obsidian Vault location
- project/Vault binding status
- Claudian plugin version and enabled status
- verified Codex CLI path used by Claudian
- local knowledge readiness
- Feishu identity and space readiness, when enabled
- effective employee publication policy

Do not perform a test upload or Wiki write during setup.

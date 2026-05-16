# jira-cli (Python)

Single-file terminal CLI for Jira using the **Atlassian Rovo MCP** server. Authentication is **OAuth 2.1** via `mcp-remote` (no Jira API token in config files).

Works alongside **Cursor’s Atlassian plugin**: use the same Atlassian account; OAuth may be shared after you log in once through either Cursor MCP or `jira auth`.

## Requirements

- Python 3.10+
- Node.js 18+ (`npx` for `mcp-remote`)
- Atlassian Cloud site (e.g. paytmpayments.atlassian.net)
- Network access to `https://mcp.atlassian.com`

## Install (one command)

From a git checkout (share this with your team):

```bash
bash /path/to/jira-cli/setup.sh
```

Or clone + install:

```bash
git clone --depth 1 <YOUR_REPO_URL> /tmp/jira-cli-install && bash /tmp/jira-cli-install/jira-cli/setup.sh
```

Then:

```bash
source ~/.zshrc
jira auth
```

See [SHARE.md](SHARE.md) for curl/git one-liners to host for remote teammates.

## Commands

| Command | Description |
|---------|-------------|
| `jira auth` | OAuth check; saves `cloudId` and site to `~/.config/jira-cli/config.json` |
| `jira cursor-login` | Run Cursor agent OAuth (finds `Cursor.app` even when `cursor` is not on your PATH) |
| `jira fields` | Lists required **create** fields (components / selects) and prints sample **`jiraAdditionalFields`** JSON |
| `jira log "…" --time 2h` | Create **TMD** Task: **Cursor (default)** suggests concise **summary** + **1–2 line description** from your note, then creates issue + worklog |
| `jira log "…" --time 2h --plain` | Skip Cursor; your text is both summary (truncated) and description |
| `jira status TMD-123 Done` | Transition issue (resolves transition id per workflow) |
| `jira eod` | Interactive: pick open issues (excludes **Done** / **Invalid**) and set status |
| `jira eod --done TMD-1,TMD-2` | Non-interactive EOD |
| `jira eod --list` | List open issues this week only (not Done / Invalid); optional browser open |
| `jira eod --open` | Open issue(s) in browser (with `--list`, all listed; interactive, after you pick) |

## Short titles & descriptions (Cursor, default on)

By default, `jira log` calls the **Cursor CLI agent** with your rough note and expects JSON: a short **issue title** (`summary`) and **one or two sentences** (`description`). If Cursor is missing or fails, the CLI falls back to your raw text.

```bash
jira log "java21 jenkins promo-admin" --time 1h
jira log "quick storm check staging" --time 20m --plain
```

Set `"expandLogWithCursor": false` in `config.json` to default to `--plain` (still use `--cursor` once to opt in).

## Config

`~/.config/jira-cli/config.json` (see `config.example.json`):

- `defaultProjectKey`: `TMD`
- `cloudId`, `siteHost`, `mcpUrl`
- `expandLogWithCursor`: use Cursor for summary + short description (default **true**; use `--plain` to skip)
- `jiraAdditionalFields`: object passed to MCP `createJiraIssue` `additional_fields` (components, labels, required custom fields — see below)
- `jiraDisableCustomfieldOptionArrayWrap`: default **false** — wraps select-like `customfield_*` payloads shaped as `{"value":"…"}` / `{"id":"…"}` into **`[{…}]`** for create (many Jira Cloud fields require an array); set **true** only if you must send a bare object
- `jiraAdditionalFieldsKeyAliases`: map mistaken JSON keys to REST ids (e.g. `"Fin_Business Cost Center": "customfield_10578"`) before create
- `jiraEodExcludeStatuses`: statuses hidden from **`jira eod`** lists (default **DONE**, **Invalid**; matching is case-insensitive)
- `jiraEodTargetStatuses`: menu of target statuses at interactive EOD (must match your Jira workflow names exactly)
- `jiraEodDefaultStatus`: default for **`jira eod --done`** and when you press Enter at the prompt (default **DONE**)
- `jiraRewriteOptionValueOnlyToName`: default **true** — after array-wrap, **`[{"value":"…"}]`** on **`customfield_*`** becomes **`[{"name":"…"}]`** when the object has only `value` (Jira often requires **name** or **id**, not **value**)
- No secrets or API tokens

### Required components / custom fields (HTTP 400)

Some projects require **Components** or finance/custom fields on create. Discover allowed values and get a starter JSON object:

```bash
jira fields
jira fields --json    # only the jiraAdditionalFields blob
```

You may use single-object option shapes for custom fields; unless **`jiraDisableCustomfieldOptionArrayWrap`** is **true**, the CLI converts them to the **array** form Jira expects, then (by default) rewrites **`value`-only** options to **`name`** for **`customfield_*`** so create accepts them.

```json
"jiraAdditionalFields": {
  "components": [{ "name": "Backend" }],
  "customfield_10578": { "value": "Exact option label from Jira" },
  "customfield_10574": { "value": "Exact option label from Jira" }
}
```

Option shapes vary by field type (`id`, `value`, or nested objects). Use Jira’s field metadata or create one issue in the UI and inspect the API payload.

For **migrated** / finicky selects, use **`[{"name": "Exact label"}]`** (Jira often rejects **`value`** and errors with *Specify a valid 'id' or 'name'*). With **`jiraRewriteOptionValueOnlyToName`** **true** (default), `[{"value":"…"}]` in config is rewritten to **`name`** automatically before create.

**Environment shortcuts** (merged with config; JSON env wins last):

- `JIRA_COMPONENT` — sets `components` to `[{"name": "<value>"}]` if `components` is not already set in config.
- `JIRA_ADDITIONAL_FIELDS_JSON` — full JSON object merged on top (e.g. one-off overrides).

Local state: `~/.local/share/jira-cli/week.json` (Monday–Saturday work week). Legacy `today.json` entries are migrated when still inside the current week.

**`jira eod`** merges:

1. All keys logged this week via `jira log`
2. Jira search: issues assigned to you **updated any day Monday–Saturday** of the current calendar week (Sunday uses the week that just ended Sat).

Only issues **not** in **Done** or **Invalid** are shown (override with `"jiraEodExcludeStatuses"` in config).

## Cursor + Atlassian MCP

1. Install the **Atlassian** plugin in Cursor and complete MCP authentication (`mcp_auth`).
2. This CLI uses the **same remote MCP endpoint** (`https://mcp.atlassian.com/v1/mcp`) through `npx mcp-remote`, not a separate Jira PAT.
3. Optional: if `~/.cursor/mcp.json` defines an Atlassian server URL, `jira auth` can pick it up.

## Troubleshooting

- **401 / not authorized**: Run `jira auth` again (browser OAuth).
- **Site admin must authorize**: Atlassian admin must approve MCP for your org.
- **npm / npx missing**: Install Node 18+.
- **pip / mcp missing**: Re-run `./install.sh` or `pip install mcp` in `.venv`.
- **`cursor` not found with `--cursor`**: Enable Cursor’s shell command (Cursor Command Palette → *Install shell command*) or set `CURSOR_CLI` to e.g. `/Applications/Cursor.app/Contents/Resources/app/bin/cursor`.
- **Cursor “Authentication required”**: Run `jira cursor-login` (recommended; no PATH setup) or install Cursor’s shell command / set `CURSOR_API_KEY`; `jira log` still falls back to your raw note.
- **`cursor: command not found`**: Cursor.app may be installed without the shell shim — use **`jira cursor-login`** or `export CURSOR_CLI=/Applications/Cursor.app/Contents/Resources/app/bin/cursor`.
- **Workspace Trust Required** (running from an untrusted folder): add **`--trust`**, **`-f`**, or **`--yolo`** to `jira log` (passed through to `cursor agent`).
- **Bad Request / “Specify the value … in an array”**: Many Cloud selects need **`[{"value":"…"}]`** — leave **`jiraDisableCustomfieldOptionArrayWrap`** **false** so single-option dicts are wrapped automatically, or set arrays explicitly in config.
- **Bad Request / “Specify a valid 'id' or 'name'”**: Jira often wants **`[{"name":"…"}]`** or **`[{"id":"…"}]`**, not **`value`**. Leave **`jiraRewriteOptionValueOnlyToName`** **true** (default) so **`value`**-only entries are rewritten, or set **`name`** explicitly to match **`jira fields`**.
- **Bad Request / field required** (`components`, `customfield_*`): Run **`jira fields`** (or **`jira fields --json`**) to print allowed values and a starter **`jiraAdditionalFields`** blob; merge into `~/.config/jira-cli/config.json`, or use `JIRA_COMPONENT` / `JIRA_ADDITIONAL_FIELDS_JSON`.
- **Cannot be set / not on the appropriate screen**: Keys must be **REST field ids** (`customfield_10578`, `priority`), not UI titles (`Fin_Business Cost Center`). **`Priority`** is auto-renamed to **`priority`**; map other labels via **`jiraAdditionalFieldsKeyAliases`**. If the field is valid but missing from the **create** screen, add its id to **`jiraLogExcludeAdditionalFields`** or export **`JIRA_LOG_EXCLUDE_ADDITIONAL_FIELDS=id1,id2`**.

## File layout

```
jira-cli/
  jira.py              # entire CLI (single file)
  requirements.txt
  config.example.json
  install.sh
  README.md
```

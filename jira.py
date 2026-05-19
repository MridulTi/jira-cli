#!/usr/bin/env python3
"""
Jira terminal CLI via Atlassian Rovo MCP (OAuth through mcp-remote).
No Jira API tokens in config — same OAuth flow as Cursor's Atlassian plugin.

Usage:
  jira.py auth
  jira.py cursor-login                     # same Cursor CLI resolution as `jira log` (no PATH needed)
  jira.py log "Rough note" --time 2h       # Cursor fills title + detailed description (default)
  jira.py log "Exact text only" --time 1h --plain
  jira.py status TMD-123 Done
  jira.py fields [--issue-type Task] [--json]   # required create fields + sample jiraAdditionalFields
  jira.py eod
  jira.py eod --done TMD-1,TMD-2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

MCP_URL_DEFAULT = "https://mcp.atlassian.com/v1/mcp"
SCRIPT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = SCRIPT_ROOT / ".venv" / "bin" / "python3"
# setup.sh may install to ~/.local/share/jira-cli
_FALLBACK_INSTALL = Path.home() / ".local" / "share" / "jira-cli"

DEFAULT_CONFIG = {
    "cloudId": "",
    "siteHost": "paytmpayments.atlassian.net",
    "defaultProjectKey": "TMD",
    "defaultIssueType": "Task",
    "stateDir": "~/.local/share/jira-cli",
    "mcpUrl": MCP_URL_DEFAULT,
    "useCursorAgent": False,
    # When true, `jira log` expands description via Cursor CLI unless `--plain`
    "expandLogWithCursor": True,
    # Passed to createJiraIssue additional_fields (components, custom fields, etc.)
    "jiraAdditionalFields": {},
    # When false (default), single-option customfields shaped like {"value": "..."}
    # are sent as [{"value": "..."}] — required by many Jira Cloud select fields.
    "jiraDisableCustomfieldOptionArrayWrap": False,
    # Rename mistaken keys in jiraAdditionalFields (e.g. UI labels → REST ids).
    "jiraAdditionalFieldsKeyAliases": {},
    # Strip keys only for `jira log` creates (fields not on create screen).
    "jiraLogExcludeAdditionalFields": [],
    # [{"value":"x"}] on customfield_* → [{"name":"x"}] when Jira rejects `value` (default on).
    "jiraRewriteOptionValueOnlyToName": True,
    # Match config labels to Jira allowedValues and send [{"id": <int>}] on create (default on).
    "jiraResolveCustomfieldOptions": True,
    # `jira eod` lists only issues whose status is not in this list.
    "jiraEodExcludeStatuses": ["DONE", "Invalid"],
    # Target statuses offered at interactive `jira eod` (must match Jira workflow names).
    "jiraEodTargetStatuses": [
        "DONE",
        "IN REVIEW",
        "IN PROGRESS",
        "OPEN",
        "BLOCKED",
        "DEPLOYMENT IN PROGRESS",
        "PENDING FOR RCA",
        "QUEUE",
        "REOPENED (MIGRATED)",
    ],
    "jiraEodDefaultStatus": "DONE",
}


_OPTION_KEYS_CUSTOMFIELD = frozenset({"value", "id", "name"})

_BUILTIN_ADDITIONAL_FIELDS_KEY_ALIASES: dict[str, str] = {
    "Priority": "priority",
}


def _apply_additional_fields_key_aliases(cfg: dict[str, Any], merged: dict[str, Any]) -> None:
    aliases = dict(_BUILTIN_ADDITIONAL_FIELDS_KEY_ALIASES)
    user_aliases = cfg.get("jiraAdditionalFieldsKeyAliases")
    if isinstance(user_aliases, dict):
        for old_k, new_k in user_aliases.items():
            if (
                isinstance(old_k, str)
                and isinstance(new_k, str)
                and old_k.strip()
                and new_k.strip()
            ):
                aliases[old_k.strip()] = new_k.strip()
    for old_k, new_k in aliases.items():
        if old_k not in merged:
            continue
        val = merged.pop(old_k)
        if new_k not in merged:
            merged[new_k] = val


def _warn_display_name_additional_field_keys(merged: dict[str, Any]) -> None:
    """REST fields use ids (customfield_…) not labels with spaces."""
    for k in merged:
        sk = str(k)
        if " " not in sk:
            continue
        print(
            f'Warning: jiraAdditionalFields key {sk!r} looks like a field label, not an API id. '
            "Use the left column from `jira fields` (e.g. customfield_10578), or set "
            '"jiraAdditionalFieldsKeyAliases" to map labels to ids.',
            file=sys.stderr,
        )


def apply_jira_log_additional_field_exclusions(
    cfg: dict[str, Any], merged: dict[str, Any]
) -> dict[str, Any]:
    """Drop keys only for `jira log` creates (fields absent from create screen)."""
    skip: list[str] = []
    raw = cfg.get("jiraLogExcludeAdditionalFields")
    if isinstance(raw, list):
        skip.extend(str(x).strip() for x in raw if str(x).strip())
    env_skip = os.environ.get("JIRA_LOG_EXCLUDE_ADDITIONAL_FIELDS", "").strip()
    if env_skip:
        skip.extend(part.strip() for part in env_skip.split(",") if part.strip())
    if not skip:
        return merged
    out = dict(merged)
    for k in skip:
        out.pop(k, None)
    return out


def _wrap_option_like_custom_fields_for_create(
    merged: dict[str, Any], cfg: dict[str, Any]
) -> None:
    """Jira Cloud often expects select/list CF payloads as an array of options."""
    if cfg.get("jiraDisableCustomfieldOptionArrayWrap"):
        return
    for key in list(merged.keys()):
        if not key.startswith("customfield_"):
            continue
        v = merged[key]
        if isinstance(v, list):
            continue
        if not isinstance(v, dict):
            continue
        ks = set(v.keys())
        if not ks or not ks <= _OPTION_KEYS_CUSTOMFIELD:
            continue
        if any(val is None for val in v.values()):
            continue
        merged[key] = [v]


def _rewrite_customfield_value_only_to_name(merged: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Some selects reject `value` on create; error asks for valid `id` or `name`."""
    if not cfg.get("jiraRewriteOptionValueOnlyToName", True):
        return
    for key in list(merged.keys()):
        if not key.startswith("customfield_"):
            continue
        v = merged[key]
        if not isinstance(v, list) or len(v) != 1:
            continue
        item = v[0]
        if not isinstance(item, dict):
            continue
        if set(item.keys()) != {"value"}:
            continue
        val = item.get("value")
        if val is None:
            continue
        merged[key] = [{"name": str(val)}]


def _coerce_jira_option_id(oid: Any) -> dict[str, Any]:
    """Jira Cloud / migrated fields often require option id as a string."""
    return {"id": str(oid).strip()}


def _normalize_customfield_option_payloads(
    merged: dict[str, Any], fields_map: dict[str, Any] | None = None
) -> None:
    """Ensure customfield option ids are strings; unwrap [{id}] when field is not array."""
    fields_map = fields_map or {}
    for key in list(merged.keys()):
        if not key.startswith("customfield_"):
            continue
        raw = merged[key]
        if isinstance(raw, dict):
            items: list[Any] = [raw]
            was_list = False
        elif isinstance(raw, list):
            items = raw
            was_list = True
        else:
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id") is not None:
                item["id"] = str(item["id"]).strip()
        spec = fields_map.get(key)
        if (
            was_list
            and len(items) == 1
            and isinstance(spec, dict)
            and not _field_schema_is_array(spec)
        ):
            merged[key] = items[0]
        elif was_list:
            merged[key] = items
        else:
            merged[key] = items[0]


def _option_search_strings(opt: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for k in ("value", "name", "id"):
        v = opt.get(k)
        if v is not None:
            out.add(str(v).strip().lower())
    return out


def _resolve_option_item(item: dict[str, Any], allowed: list[Any]) -> dict[str, Any]:
    """Map user name/value/id strings to the option id Jira accepts on create."""
    keys = set(item.keys())
    if keys <= {"id"} and "id" in item:
        return _coerce_jira_option_id(item["id"])

    needles: set[str] = set()
    for k in ("name", "value"):
        if k in item and item[k] is not None:
            needles.add(str(item[k]).strip().lower())
    if not needles:
        return item

    for opt in allowed:
        if not isinstance(opt, dict):
            continue
        if needles & _option_search_strings(opt):
            oid = opt.get("id")
            if oid is not None:
                return _coerce_jira_option_id(oid)
    return item


def _resolve_customfield_options_in_merged(
    merged: dict[str, Any], fields_map: dict[str, Any]
) -> list[str]:
    """Rewrite customfield_* option payloads using allowedValues; return unresolved hints."""
    unresolved: list[str] = []
    for key in list(merged.keys()):
        if not key.startswith("customfield_"):
            continue
        spec = fields_map.get(key)
        if not isinstance(spec, dict):
            continue
        allowed = spec.get("allowedValues")
        if not isinstance(allowed, list):
            continue
        raw = merged[key]
        if isinstance(raw, dict):
            items: list[Any] = [raw]
        elif isinstance(raw, list):
            items = raw
        else:
            continue
        resolved: list[Any] = []
        for item in items:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                resolved.append(item)
                continue
            out = _resolve_option_item(item, allowed)
            resolved.append(out)
            if "id" not in out and any(k in item for k in ("name", "value")):
                unresolved.append(f"{key}={item!r}")
        merged[key] = resolved
    return unresolved


async def fetch_issue_type_fields_map(
    session: Any,
    cloud_id: str,
    project_key: str,
    issue_type_name: str,
) -> dict[str, Any]:
    """All field specs for an issue type (paginated getJiraIssueTypeMetaWithFields)."""
    pk = project_key
    want_type = issue_type_name.strip()
    itypes_raw = await call_tool(
        session,
        "getJiraProjectIssueTypesMetadata",
        {"cloudId": cloud_id, "projectIdOrKey": pk, "maxResults": 50},
    )
    itypes = _extract_issue_types_list(itypes_raw)
    issue_type_id: str | None = None
    for it in itypes:
        if str(it.get("name", "")).strip().lower() == want_type.lower():
            issue_type_id = str(it.get("id", "")).strip()
            break
    if not issue_type_id:
        avail = ", ".join(str(x.get("name", "")) for x in itypes if x.get("name")) or "none"
        raise RuntimeError(
            f"Issue type {want_type!r} not found for project {pk}. Available: {avail}"
        )

    merged: dict[str, Any] = {}
    start_at = 0
    for _ in range(30):
        chunk = await call_tool(
            session,
            "getJiraIssueTypeMetaWithFields",
            {
                "cloudId": cloud_id,
                "projectIdOrKey": pk,
                "issueTypeId": issue_type_id,
                "startAt": start_at,
                "maxResults": 100,
            },
        )
        field_map = _extract_fields_map(chunk)
        if not field_map:
            break
        merged.update(field_map)
        page_len = len(field_map)
        total = dig(chunk, "total")
        if total is None:
            total = dig(chunk, "data", "total")
        seen = start_at + page_len
        if isinstance(total, int) and seen >= total:
            break
        if page_len < 100:
            break
        start_at = seen
    return merged


def merge_additional_fields(cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge config + env for Jira fields required by project workflow."""
    merged = dict(cfg.get("jiraAdditionalFields") or {})
    comp = os.environ.get("JIRA_COMPONENT", "").strip()
    if comp:
        merged.setdefault("components", [{"name": comp}])
    raw = os.environ.get("JIRA_ADDITIONAL_FIELDS_JSON", "").strip()
    if raw:
        try:
            blob = json.loads(raw)
            if isinstance(blob, dict):
                merged.update(blob)
        except json.JSONDecodeError:
            print(
                "Warning: JIRA_ADDITIONAL_FIELDS_JSON is not valid JSON; ignoring.",
                file=sys.stderr,
            )
    _apply_additional_fields_key_aliases(cfg, merged)
    _wrap_option_like_custom_fields_for_create(merged, cfg)
    _rewrite_customfield_value_only_to_name(merged, cfg)
    _normalize_customfield_option_payloads(merged)
    _warn_display_name_additional_field_keys(merged)
    return merged


def _print_jira_create_error_hints(message: str) -> None:
    """Extra stderr hints for common Jira createIssue validation errors."""
    m = message.lower()
    if "required" in m or "customfield_" in m:
        print(
            '  Hint: set "jiraAdditionalFields" in ~/.config/jira-cli/config.json '
            "(components + required custom fields). Run `jira fields` or "
            "`jira fields --json` for allowed values. Optional env: JIRA_COMPONENT, "
            "JIRA_ADDITIONAL_FIELDS_JSON (JSON object merged on top).",
            file=sys.stderr,
        )
    if "in an array" in m:
        print(
            "  Hint: that field expects an array of options, e.g. "
            '`[{"value": "Advertisement"}]`. By default jira-cli wraps '
            'option-shaped customfield_* objects (`{"value": "..."}`) automatically; '
            'set \"jiraDisableCustomfieldOptionArrayWrap\": true to disable.',
            file=sys.stderr,
        )
    if "specify a valid" in m and "id" in m:
        print(
            "  Hint: use option **id** as a **string** from `jira fields`, e.g. "
            '[{"id": "11195"}] or {"id": "11195"} for single-select fields.',
            file=sys.stderr,
        )
    if "must be a string" in m:
        print(
            "  Hint: option ids must be JSON strings, not numbers — e.g. "
            '"customfield_10578": [{"id": "11217"}].',
            file=sys.stderr,
        )
    if "cannot be set" in m or "not on the appropriate screen" in m:
        print(
            "  Hint: keys must be REST ids (e.g. customfield_10578, priority), not display "
            'names like "Fin_Business Cost Center". Run `jira fields`. If a field is valid '
            "but blocked on **create**, list its id under \"jiraLogExcludeAdditionalFields\" "
            "or set JIRA_LOG_EXCLUDE_ADDITIONAL_FIELDS (comma-separated).",
            file=sys.stderr,
        )


def extract_runtime_from_group(exc: BaseException) -> RuntimeError | None:
    """MCP stdio teardown wraps failures in ExceptionGroup; surface RuntimeError."""
    if isinstance(exc, RuntimeError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = extract_runtime_from_group(sub)
            if found is not None:
                return found
    return None


def expand_home(p: str) -> str:
    if p.startswith("~/"):
        return str(Path.home() / p[2:])
    if p == "~":
        return str(Path.home())
    return p


def config_dir() -> Path:
    return Path.home() / ".config" / "jira-cli"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
    cfg["stateDir"] = expand_home(str(cfg["stateDir"]))
    return cfg


def save_config(partial: dict[str, Any]) -> dict[str, Any]:
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_path()
    current = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            current = json.load(f)
    current.update(partial)
    with path.open("w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
        f.write("\n")
    return load_config()


def state_week_path(cfg: dict[str, Any]) -> Path:
    """Local log entries for the current Mon–Sat work week."""
    return Path(cfg["stateDir"]) / "week.json"


def state_day_path(cfg: dict[str, Any]) -> Path:
    """Legacy daily file (migrated into week.json on read)."""
    return Path(cfg["stateDir"]) / "today.json"


def monday_of_week(d: date) -> date:
    """ISO-style Monday start (Python weekday: Mon=0 … Sun=6)."""
    return d - timedelta(days=d.weekday())


def saturday_of_work_week(d: date) -> date:
    """Inclusive Saturday end of Mon–Sat work week."""
    return monday_of_week(d) + timedelta(days=5)


def issue_url(cfg: dict[str, Any], key: str) -> str:
    host = str(cfg["siteHost"]).replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{host}/browse/{key}"


def open_issue_in_browser(cfg: dict[str, Any], key: str) -> None:
    """Open issue in the default system browser."""
    url = issue_url(cfg, key.upper())
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", url], check=False)
        else:
            import webbrowser

            webbrowser.open(url)
        print(f"Opened {url}")
    except OSError as err:
        print(f"Could not open browser: {err}", file=sys.stderr)
        print(url)


def prompt_open_in_browser(cfg: dict[str, Any], items: list[tuple[str, str]]) -> None:
    """Let user open selected issues in the browser (# from list, keys, all, or skip)."""
    if not items:
        return
    print("\nOpen in browser? Enter # from list, issue keys, `all`, or Enter to skip:")
    raw = input("> ").strip()
    if not raw:
        return
    keys_to_open: list[str] = []
    if raw.lower() == "all":
        keys_to_open = [k for k, _ in items]
    else:
        key_set = {k.upper() for k, _ in items}
        for part in re.split(r"[\s,]+", raw):
            if not part:
                continue
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(items):
                    keys_to_open.append(items[idx][0])
            elif part.upper() in key_set:
                keys_to_open.append(part.upper())
    seen: set[str] = set()
    for k in keys_to_open:
        ku = k.upper()
        if ku not in seen:
            seen.add(ku)
            open_issue_in_browser(cfg, ku)


def _empty_week_state(mon: date) -> dict[str, Any]:
    sat = mon + timedelta(days=5)
    return {
        "week_start": mon.isoformat(),
        "week_end_sat": sat.isoformat(),
        "entries": [],
    }


def _migrate_legacy_today_json(cfg: dict[str, Any], mon: date) -> list[dict[str, str]]:
    """Pull entries from legacy today.json into current week if applicable."""
    legacy = state_day_path(cfg)
    if not legacy.exists():
        return []
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = data.get("entries") or []
    # Old format used calendar day; keep entries when created this work week
    out: list[dict[str, str]] = []
    for e in entries:
        created = e.get("createdAt", "")
        try:
            day = date.fromisoformat(created[:10]) if len(created) >= 10 else None
        except ValueError:
            day = None
        if day is not None and mon <= day <= mon + timedelta(days=5):
            out.append(e)
    return out


def load_week_log(cfg: dict[str, Any]) -> dict[str, Any]:
    """Load Mon–Sat work week bucket for `jira log` / `jira eod`."""
    mon = monday_of_week(date.today())
    path = state_week_path(cfg)
    Path(cfg["stateDir"]).mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("week_start") == mon.isoformat():
                data.setdefault("entries", [])
                return data
        except (json.JSONDecodeError, OSError):
            pass

    merged = _empty_week_state(mon)
    merged["entries"].extend(_migrate_legacy_today_json(cfg, mon))
    return merged


def save_week_log(cfg: dict[str, Any], state: dict[str, Any]) -> None:
    path = state_week_path(cfg)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_week_log(cfg: dict[str, Any], entry: dict[str, str]) -> None:
    state = load_week_log(cfg)
    mon = monday_of_week(date.today())
    if state.get("week_start") != mon.isoformat():
        state = _empty_week_state(mon)
    state.setdefault("entries", []).append(entry)
    save_week_log(cfg, state)


def remove_week_log_entry(cfg: dict[str, Any], key: str) -> None:
    state = load_week_log(cfg)
    state["entries"] = [e for e in state.get("entries", []) if e.get("key") != key]
    save_week_log(cfg, state)


def _eod_excluded_statuses(cfg: dict[str, Any]) -> tuple[str, ...]:
    raw = cfg.get("jiraEodExcludeStatuses")
    if isinstance(raw, list) and raw:
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return ("DONE", "Invalid")


def _eod_target_statuses(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("jiraEodTargetStatuses")
    if isinstance(raw, list) and raw:
        return [str(x).strip() for x in raw if str(x).strip()]
    return [
        "DONE",
        "IN REVIEW",
        "IN PROGRESS",
        "OPEN",
        "BLOCKED",
        "DEPLOYMENT IN PROGRESS",
        "PENDING FOR RCA",
        "QUEUE",
        "REOPENED (MIGRATED)",
    ]


def _eod_default_status(cfg: dict[str, Any]) -> str:
    explicit = str(cfg.get("jiraEodDefaultStatus") or "").strip()
    if explicit:
        return explicit
    options = _eod_target_statuses(cfg)
    return options[0] if options else "DONE"


def _is_eod_excluded_status(status: str, excluded: tuple[str, ...]) -> bool:
    needle = status.strip().lower()
    return needle in {s.lower() for s in excluded}


def work_week_jql(cfg: dict[str, Any] | None = None) -> str:
    """Assignee issues updated Mon–Sat of current calendar week (locale-independent dates)."""
    mon = monday_of_week(date.today())
    sat = saturday_of_work_week(date.today())
    m = mon.strftime("%Y/%m/%d")
    s = sat.strftime("%Y/%m/%d")
    base = (
        f'assignee = currentUser() AND updated >= "{m}" AND updated <= endOfDay("{s}")'
    )
    excluded = _eod_excluded_statuses(cfg or {})
    if excluded:
        names = ", ".join(excluded)
        return f"{base} AND status NOT IN ({names})"
    return base


def _issue_status_from_search_hit(issue: dict[str, Any]) -> str:
    return str(dig(issue, "fields", "status", "name") or dig(issue, "status", "name") or "")


def parse_tool_result(result: Any) -> Any:
    if getattr(result, "isError", False):
        parts = []
        for block in getattr(result, "content", []) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", ""))
        raise RuntimeError("MCP tool error: " + ("\n".join(parts) or "unknown"))

    texts: list[str] = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            texts.append(getattr(block, "text", ""))
    raw = "\n".join(texts).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def dig(obj: Any, *keys: str, default: Any = None) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _deep_find(obj: Any, key: str) -> Any:
    """Find first value for key in nested dict/list structures."""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for v in obj.values():
            found = _deep_find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find(item, key)
            if found is not None:
                return found
    return None


def extract_user_info(payload: Any) -> dict[str, str]:
    """Normalize atlassianUserInfo tool output."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {
                "displayName": "",
                "emailAddress": "",
                "accountId": "",
            }

    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]

    display = (
        dig(payload, "displayName")
        or dig(payload, "name")
        or _deep_find(payload, "displayName")
        or _deep_find(payload, "name")
        or ""
    )
    email = (
        dig(payload, "emailAddress")
        or dig(payload, "email")
        or _deep_find(payload, "emailAddress")
        or _deep_find(payload, "email")
        or ""
    )
    account_id = (
        dig(payload, "accountId")
        or dig(payload, "account_id")
        or _deep_find(payload, "accountId")
        or _deep_find(payload, "account_id")
        or ""
    )
    return {
        "displayName": str(display) if display else "",
        "emailAddress": str(email) if email else "",
        "accountId": str(account_id) if account_id else "",
    }


def find_transition(transitions_payload: Any, status_name: str) -> dict[str, Any] | None:
    transitions = dig(transitions_payload, "transitions") or dig(
        transitions_payload, "data", "transitions"
    ) or []
    needle = status_name.strip().lower()
    for t in transitions:
        if str(t.get("name", "")).lower() == needle:
            return t
        if str(dig(t, "to", "name", default="")).lower() == needle:
            return t
    return None


def format_transitions(transitions_payload: Any) -> list[dict[str, str]]:
    transitions = dig(transitions_payload, "transitions") or dig(
        transitions_payload, "data", "transitions"
    ) or []
    out = []
    for t in transitions:
        out.append(
            {
                "id": str(t.get("id", "")),
                "name": str(t.get("name", "")),
                "to": str(dig(t, "to", "name", default="")),
            }
        )
    return out


def _extract_issue_types_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize getJiraProjectIssueTypesMetadata response."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    inner = payload.get("data")
    candidates: list[Any] = []
    if isinstance(inner, list):
        candidates.append(inner)
    elif isinstance(inner, dict):
        for key in ("issueTypes", "issue_types", "values"):
            v = inner.get(key)
            if isinstance(v, list):
                candidates.append(v)
    for key in ("issueTypes", "issue_types", "values"):
        v = payload.get(key)
        if isinstance(v, list):
            candidates.append(v)
    for c in candidates:
        out = [x for x in c if isinstance(x, dict)]
        if out:
            return out
    return []


def _extract_fields_map(payload: Any) -> dict[str, Any]:
    """Normalize getJiraIssueTypeMetaWithFields → field id → spec dict."""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data", payload)
    if not isinstance(inner, dict):
        return {}
    fields = inner.get("fields")
    if isinstance(fields, dict):
        return dict(fields)
    if isinstance(fields, list):
        out: dict[str, Any] = {}
        for item in fields:
            if not isinstance(item, dict):
                continue
            fid = item.get("fieldId") or item.get("key") or item.get("id")
            if fid:
                out[str(fid)] = item
        return out
    return {}


def _allowed_preview_lines(spec: dict[str, Any], limit: int = 35) -> list[str]:
    av = spec.get("allowedValues")
    if not isinstance(av, list):
        return []
    lines: list[str] = []
    for opt in av[:limit]:
        if not isinstance(opt, dict):
            continue
        label = opt.get("value") if opt.get("value") is not None else opt.get("name")
        oid = opt.get("id")
        if label is not None and oid is not None:
            lines.append(f"    • {label}  (id={oid})")
        elif label is not None:
            lines.append(f"    • {label}")
    if len(av) > limit:
        lines.append(f"    … +{len(av) - limit} more")
    return lines


def _field_schema_is_array(spec: dict[str, Any]) -> bool:
    schema = spec.get("schema")
    if not isinstance(schema, dict):
        return False
    return str(schema.get("type", "")).lower() == "array"


def _sample_for_additional_field(field_id: str, spec: dict[str, Any]) -> Any:
    av = spec.get("allowedValues") if isinstance(spec.get("allowedValues"), list) else []
    want_arr = _field_schema_is_array(spec)
    if field_id == "components":
        if av and isinstance(av[0], dict):
            name = av[0].get("name")
            if name:
                return [{"name": str(name)}]
        return [{"name": "<replace-with-component-name>"}]
    if av and isinstance(av[0], dict):
        first = av[0]
        inner: dict[str, Any] | None = None
        if first.get("id") is not None:
            inner = _coerce_jira_option_id(first["id"])
        elif first.get("value") is not None:
            inner = {"name": str(first["value"])}
        elif first.get("name") is not None:
            inner = {"name": str(first["name"])}
        if inner is not None:
            if want_arr or field_id.startswith("customfield_"):
                return [inner]
            return inner
    return "<replace-me>"


def normalize_jira_time_spent(raw: str) -> str:
    """
  Convert compact durations to Jira worklog format (space-separated units).
  e.g. 1h30m -> 1h 30m, 2d4h -> 2d 4h. Already-spaced input is preserved.
  """
    s = raw.strip()
    if not s:
        raise ValueError("time cannot be empty")
    spaced = re.match(r"^(\d+[wdhm])(\s+\d+[wdhm])+$", s, re.IGNORECASE)
    if spaced:
        return re.sub(r"\s+", " ", s)
    parts = re.findall(r"(\d+)\s*([wdhm])", s, re.IGNORECASE)
    if parts:
        return " ".join(f"{n}{u.lower()}" for n, u in parts)
    return s


def summary_from_text(text: str, max_len: int = 120) -> str:
    line = (text.strip().splitlines() or ["Work log"])[0]
    return line if len(line) <= max_len else line[: max_len - 3] + "..."


def cursor_mcp_url_from_config() -> str | None:
    """Read Atlassian MCP URL from Cursor user config if present."""
    candidates = [
        Path.home() / ".cursor" / "mcp.json",
        Path.home() / ".cursor" / "mcp_settings.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or data.get("servers") or {}
            for name, spec in servers.items():
                if "atlassian" in name.lower() or "atlassian" in json.dumps(spec).lower():
                    url = spec.get("url") if isinstance(spec, dict) else None
                    if url:
                        return url
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _venv_python() -> Path | None:
    if VENV_PYTHON.is_file():
        return VENV_PYTHON
    alt = _FALLBACK_INSTALL / ".venv" / "bin" / "python3"
    if alt.is_file():
        return alt
    return None


def bootstrap_venv() -> None:
    """Re-run with project venv Python if mcp is missing and .venv exists."""
    venv_py = _venv_python()
    if venv_py and Path(sys.executable).resolve() != venv_py.resolve():
        try:
            import mcp  # noqa: F401
        except ImportError:
            os.execv(str(venv_py), [str(venv_py), *sys.argv])


def _augment_path_for_node() -> None:
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".fnm/current/bin")]
    path = os.environ.get("PATH", "")
    for prefix in extra:
        if prefix not in path.split(os.pathsep):
            path = f"{prefix}{os.pathsep}{path}"
    os.environ["PATH"] = path


def resolve_npx() -> tuple[str, list[str]]:
    """Return (command, args_prefix) to launch mcp-remote via npx."""
    _augment_path_for_node()
    for npx in ("npx", "/opt/homebrew/bin/npx", "/usr/local/bin/npx"):
        if shutil.which(npx) or (npx.startswith("/") and Path(npx).is_file()):
            return npx, ["-y", "mcp-remote@latest"]
    raise RuntimeError(
        "npx not found. Install Node.js 18+ (e.g. brew install node) for mcp-remote OAuth."
    )


def require_mcp_package() -> None:
    try:
        from mcp import ClientSession, StdioServerParameters  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
    except ImportError as e:
        venv_hint = (
            f'  {VENV_PYTHON} -m pip install mcp\n  # or: cd "{SCRIPT_ROOT}" && ./install.sh'
            if VENV_PYTHON.parent.is_dir()
            else f'  pip install mcp\n  # or: cd "{SCRIPT_ROOT}" && ./install.sh'
        )
        print(
            "Missing Python package: mcp\n\n"
            f"Install in the project venv:\n{venv_hint}\n\n"
            "Use the venv when running:\n"
            f'  "{VENV_PYTHON}" "{SCRIPT_ROOT / "jira.py"}" auth',
            file=sys.stderr,
        )
        raise SystemExit(1) from e


@asynccontextmanager
async def mcp_session(mcp_url: str, *, quiet: bool = True) -> AsyncIterator[Any]:
    require_mcp_package()
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    npx_cmd, npx_args = resolve_npx()
    params = StdioServerParameters(
        command=npx_cmd,
        args=[*npx_args, mcp_url],
    )
    errlog = open(os.devnull, "w") if quiet else sys.stderr
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        if errlog is not sys.stderr:
            errlog.close()


async def call_tool(session: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    result = await session.call_tool(name, args or {})
    return parse_tool_result(result)


async def resolve_cloud_id(session: Any, cloud_id: str) -> str:
    if cloud_id:
        return cloud_id
    resources = await call_tool(session, "getAccessibleAtlassianResources", {})
    if not isinstance(resources, list) or not resources:
        raise RuntimeError("No accessible Atlassian sites. Run: jira auth")
    return str(resources[0]["id"])


async def cmd_auth(cfg: dict[str, Any]) -> None:
    mcp_url = cfg.get("mcpUrl") or cursor_mcp_url_from_config() or MCP_URL_DEFAULT
    print("Connecting to Atlassian MCP (browser OAuth may open)...\n")
    async with mcp_session(mcp_url) as session:
        user = await call_tool(session, "atlassianUserInfo", {})
        info = extract_user_info(user)
        display = info["displayName"] or "unknown"
        email = info["emailAddress"]
        cloud_id = await resolve_cloud_id(session, str(cfg.get("cloudId") or ""))
        resources = await call_tool(session, "getAccessibleAtlassianResources", {})
        site_host = cfg.get("siteHost")
        if isinstance(resources, list):
            site = next((r for r in resources if r.get("id") == cloud_id), resources[0] if resources else None)
            if site and site.get("url"):
                site_host = site["url"].replace("https://", "").replace("http://", "").rstrip("/")
        save_config({"cloudId": cloud_id, "siteHost": site_host, "mcpUrl": mcp_url})
        print("Authenticated successfully.\n")
        print(f"  User:    {display}" + (f" <{email}>" if email else ""))
        print(f"  Cloud:   {cloud_id}")
        print(f"  Config:  {config_path()}")
        print(f"  Project: {cfg.get('defaultProjectKey')} (default)")


async def cmd_fields(
    cfg: dict[str, Any],
    project: str | None,
    issue_type_name: str | None,
    json_only: bool,
) -> None:
    """Print required fields for issue create + sample ``jiraAdditionalFields`` from Jira metadata."""
    mcp_url = cfg.get("mcpUrl") or MCP_URL_DEFAULT
    async with mcp_session(mcp_url) as session:
        cloud_id = await resolve_cloud_id(session, str(cfg.get("cloudId") or ""))
        pk = project or str(cfg.get("defaultProjectKey") or "TMD")
        want_type = (issue_type_name or str(cfg.get("defaultIssueType") or "Task")).strip()

        itypes_raw = await call_tool(
            session,
            "getJiraProjectIssueTypesMetadata",
            {"cloudId": cloud_id, "projectIdOrKey": pk, "maxResults": 50},
        )
        itypes = _extract_issue_types_list(itypes_raw)
        issue_type_id: str | None = None
        matched = want_type
        for it in itypes:
            if str(it.get("name", "")).strip().lower() == want_type.lower():
                issue_type_id = str(it.get("id", "")).strip()
                matched = str(it.get("name", want_type))
                break
        if not issue_type_id:
            avail = ", ".join(str(x.get("name", "")) for x in itypes if x.get("name")) or "none"
            raise RuntimeError(
                f"Issue type {want_type!r} not found for project {pk}. Available: {avail}"
            )

        merged = await fetch_issue_type_fields_map(session, cloud_id, pk, matched)
        if not merged:
            raise RuntimeError(
                "No field metadata returned from Jira (empty response). "
                "Check project key, issue type, and MCP permissions."
            )

        required_pairs = sorted(
            (
                (fid, spec)
                for fid, spec in merged.items()
                if isinstance(spec, dict) and spec.get("required")
            ),
            key=lambda x: x[0],
        )
        sample: dict[str, Any] = {}
        for fid, spec in required_pairs:
            if not isinstance(spec, dict):
                continue
            if fid in ("summary", "description", "issuetype", "project", "reporter", "assignee"):
                continue
            sample[fid] = _sample_for_additional_field(fid, spec)

        if json_only:
            print(json.dumps(sample, indent=2))
            return

        print(f"Project {pk} / {matched} (issueTypeId={issue_type_id})\n")
        print("Required fields for create (beyond summary/description/project/type):\n")
        if not required_pairs:
            print("  (none reported — try creating in UI or check permissions)\n")
        else:
            for fid, spec in required_pairs:
                if not isinstance(spec, dict):
                    continue
                if fid in ("summary", "description", "issuetype", "project", "reporter", "assignee"):
                    continue
                fname = str(spec.get("name") or fid)
                print(f"  {fid} — {fname}")
                prev = _allowed_preview_lines(spec)
                if prev:
                    print("    Allowed values:")
                    print("\n".join(prev))
                elif fid == "components":
                    print(
                        "    (options often come from project Components — "
                        "names must match API expectations)"
                    )
                print()
        print(
            'Paste under "jiraAdditionalFields" in ~/.config/jira-cli/config.json '
            "(use **field ids** from the left column above, e.g. customfield_10578 — "
            "not labels such as \"Fin_Business Cost Center\"):\n"
        )
        print(json.dumps(sample, indent=2))


async def cmd_log(
    cfg: dict[str, Any],
    description: str,
    time_spent: str,
    project: str | None,
    issue_type: str | None,
    started: str | None,
    use_cursor: bool,
    cursor_agent_extra: list[str],
) -> None:
    brief = description.strip()
    summary_text = summary_from_text(brief)
    description_body = brief

    if use_cursor:
        print("Cursor: deriving title + detailed description…\n")
        try:
            summary_text, description_body = expand_issue_via_cursor(
                brief, agent_extra=cursor_agent_extra
            )
        except (RuntimeError, ValueError, json.JSONDecodeError) as err:
            msg = str(err)
            print(f"  (Cursor unavailable or parse failed: {msg})", file=sys.stderr)
            if "Authentication required" in msg or "CURSOR_API_KEY" in msg:
                print(
                    "  Hint: run `jira cursor-login` (works without cursor on PATH), "
                    "or `cursor agent login` / CURSOR_API_KEY.",
                    file=sys.stderr,
                )
            if "Workspace Trust" in msg or "trust this directory" in msg.lower():
                print(
                    "  Hint: pass `--trust`, `-f`, or `--yolo` to `jira log` "
                    "(forwards to `cursor agent`).",
                    file=sys.stderr,
                )
            print("  Falling back to your raw note for summary and description.\n", file=sys.stderr)
            summary_text = summary_from_text(brief)
            description_body = brief
        else:
            print(f"  Summary: {summary_text}")
            print(f"  Description:\n  {description_body.replace(chr(10), chr(10) + '  ')}\n")

    mcp_url = cfg.get("mcpUrl") or MCP_URL_DEFAULT
    async with mcp_session(mcp_url) as session:
        cloud_id = await resolve_cloud_id(session, str(cfg.get("cloudId") or ""))
        user = await call_tool(session, "atlassianUserInfo", {})
        account_id = extract_user_info(user)["accountId"]
        if not account_id:
            raise RuntimeError("Could not resolve account ID. Run: jira auth")

        project_key = project or cfg["defaultProjectKey"]
        type_name = issue_type or cfg["defaultIssueType"]

        create_payload: dict[str, Any] = {
            "cloudId": cloud_id,
            "projectKey": project_key,
            "issueTypeName": type_name,
            "summary": summary_text,
            "description": description_body,
            "assignee_account_id": account_id,
        }
        extra_fields = apply_jira_log_additional_field_exclusions(cfg, merge_additional_fields(cfg))
        if cfg.get("jiraResolveCustomfieldOptions", True):
            fields_map = await fetch_issue_type_fields_map(
                session, cloud_id, project_key, type_name
            )
            for hint in _resolve_customfield_options_in_merged(extra_fields, fields_map):
                print(
                    f"Warning: could not resolve {hint} — check spelling or use "
                    '`[{"id": "<id>"}]` from `jira fields`.',
                    file=sys.stderr,
                )
            _normalize_customfield_option_payloads(extra_fields, fields_map)
        if extra_fields:
            create_payload["additional_fields"] = extra_fields

        created = await call_tool(session, "createJiraIssue", create_payload)
        key = dig(created, "key") or dig(created, "issueKey") or dig(created, "data", "key")
        if not key:
            raise RuntimeError(f"Issue created but key missing: {str(created)[:200]}")

        jira_time = normalize_jira_time_spent(time_spent)
        wl_args: dict[str, Any] = {
            "cloudId": cloud_id,
            "issueIdOrKey": key,
            "timeSpent": jira_time,
            "commentBody": description_body,
        }
        if started:
            wl_args["started"] = started
        try:
            await call_tool(session, "addWorklogToJiraIssue", wl_args)
        except RuntimeError as err:
            msg = str(err)
            if "worklog" in msg.lower() or "timelogged" in msg.lower():
                raise RuntimeError(
                    f"Issue {key} was created, but worklog failed: {msg}\n"
                    f"  timeSpent sent: {jira_time!r} (from --time {time_spent.strip()!r}). "
                    "Use spaced units, e.g. 1h 30m or 90m."
                ) from err
            raise

        append_week_log(
            cfg,
            {
                "key": key,
                "summary": summary_text,
                "timeSpent": jira_time,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(f"Created {key} and logged {jira_time}")
        print(issue_url(cfg, key))


async def cmd_status(cfg: dict[str, Any], key: str, status_name: str) -> None:
    mcp_url = cfg.get("mcpUrl") or MCP_URL_DEFAULT
    async with mcp_session(mcp_url) as session:
        cloud_id = await resolve_cloud_id(session, str(cfg.get("cloudId") or ""))
        transitions = await call_tool(
            session,
            "getTransitionsForJiraIssue",
            {"cloudId": cloud_id, "issueIdOrKey": key},
        )
        match = find_transition(transitions, status_name)
        if not match:
            print(f'No transition matching "{status_name}" for {key}.', file=sys.stderr)
            for t in format_transitions(transitions):
                print(f"  - {t['name']} → {t['to']} (id: {t['id']})", file=sys.stderr)
            raise SystemExit(1)
        await call_tool(
            session,
            "transitionJiraIssue",
            {
                "cloudId": cloud_id,
                "issueIdOrKey": key,
                "transition": {"id": str(match["id"])},
            },
        )
        to_name = dig(match, "to", "name") or status_name
        print(f"{key} → {to_name}")
        print(issue_url(cfg, key))


async def transition_by_name(
    session: Any, cloud_id: str, key: str, status_name: str
) -> str:
    transitions = await call_tool(
        session,
        "getTransitionsForJiraIssue",
        {"cloudId": cloud_id, "issueIdOrKey": key},
    )
    match = find_transition(transitions, status_name)
    if not match:
        names = ", ".join(t["name"] for t in format_transitions(transitions))
        raise RuntimeError(f'No transition "{status_name}" for {key}. Available: {names}')
    await call_tool(
        session,
        "transitionJiraIssue",
        {
            "cloudId": cloud_id,
            "issueIdOrKey": key,
            "transition": {"id": str(match["id"])},
        },
    )
    return str(dig(match, "to", "name") or status_name)


def prompt_checkbox(items: list[tuple[str, str]]) -> list[str]:
    if not items:
        return []
    print("Select issues (comma-separated numbers, or 'all'):")
    for i, (key, summary) in enumerate(items, 1):
        print(f"  {i}. {key} — {summary}")
    raw = input("> ").strip().lower()
    if raw == "all":
        return [k for k, _ in items]
    picks: list[str] = []
    for part in re.split(r"[\s,]+", raw):
        if not part:
            continue
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(items):
                picks.append(items[idx][0])
        elif part.upper() in {k.upper() for k, _ in items}:
            picks.append(part.upper())
    return picks


def prompt_status(cfg: dict[str, Any]) -> str:
    options = _eod_target_statuses(cfg)
    default = _eod_default_status(cfg)
    print(f"Target status (default: {default}):")
    for i, name in enumerate(options, 1):
        print(f"  {i}. {name}")
    print(f"  {len(options) + 1}. Custom")
    raw = input("> ").strip()
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(options):
            return options[n - 1]
        if n == len(options) + 1:
            custom = input("Status name: ").strip()
            if custom:
                return custom
    return raw or default


async def _filter_eod_issues_by_status(
    session: Any,
    cloud_id: str,
    cfg: dict[str, Any],
    issues: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Keep only issues whose status is not in jiraEodExcludeStatuses (default Done, Invalid)."""
    if not issues:
        return issues
    excluded = _eod_excluded_statuses(cfg)
    keys = sorted(issues.keys())
    key_list = ", ".join(keys)
    jql = f"key in ({key_list}) AND status NOT IN ({', '.join(excluded)})"
    result = await call_tool(
        session,
        "searchJiraIssuesUsingJql",
        {
            "cloudId": cloud_id,
            "jql": jql,
            "maxResults": max(len(keys), 100),
            "fields": ["summary", "status", "key"],
        },
    )
    open: dict[str, dict[str, str]] = {}
    for i in dig(result, "issues") or []:
        k = i.get("key")
        if not k or k not in issues:
            continue
        open[k] = {
            "key": k,
            "summary": dig(i, "fields", "summary") or issues[k].get("summary", k),
            "status": _issue_status_from_search_hit(i),
        }
    return open


async def cmd_eod(
    cfg: dict[str, Any],
    done_keys: list[str] | None,
    status: str,
    merge_jql: bool,
    list_only: bool,
    open_browser: bool = False,
) -> None:
    mcp_url = cfg.get("mcpUrl") or MCP_URL_DEFAULT
    async with mcp_session(mcp_url) as session:
        cloud_id = await resolve_cloud_id(session, str(cfg.get("cloudId") or ""))
        issues: dict[str, dict[str, str]] = {}
        for e in load_week_log(cfg).get("entries", []):
            issues[e["key"]] = {"key": e["key"], "summary": e.get("summary", e["key"])}

        if merge_jql:
            result = await call_tool(
                session,
                "searchJiraIssuesUsingJql",
                {
                    "cloudId": cloud_id,
                    "jql": work_week_jql(cfg),
                    "maxResults": 100,
                    "fields": ["summary", "status", "key"],
                },
            )
            excluded = _eod_excluded_statuses(cfg)
            for i in dig(result, "issues") or []:
                k = i.get("key")
                if not k:
                    continue
                status_name = _issue_status_from_search_hit(i)
                if _is_eod_excluded_status(status_name, excluded):
                    continue
                if k not in issues:
                    issues[k] = {
                        "key": k,
                        "summary": dig(i, "fields", "summary") or k,
                        "status": status_name,
                    }

        issues = await _filter_eod_issues_by_status(session, cloud_id, cfg, issues)

        if not issues:
            print("No open issues for this work week (excluding Done / Invalid).")
            return

        if list_only:
            print("Open issues this week (not Done / Invalid):")
            numbered = sorted(issues.values(), key=lambda x: x["key"])
            for i, row in enumerate(numbered, 1):
                st = row.get("status")
                suffix = f"  [{st}]" if st else ""
                print(f"  {i}. {row['key']}{suffix}  {row['summary']}")
            items = [(row["key"], row["summary"]) for row in numbered]
            if open_browser:
                for k, _ in items:
                    open_issue_in_browser(cfg, k)
            else:
                prompt_open_in_browser(cfg, items)
            print("\nUse: jira eod --done KEY1,KEY2 [--status DONE]  |  jira eod --open")
            return

        if done_keys:
            target = status or _eod_default_status(cfg)
            for key in done_keys:
                k = key.strip().upper()
                if open_browser:
                    open_issue_in_browser(cfg, k)
                to = await transition_by_name(session, cloud_id, k, target)
                print(f"{k} → {to}")
                print(issue_url(cfg, k))
                remove_week_log_entry(cfg, k)
            return

        checkbox_items = [
            (
                i["key"],
                f"{i['summary']}" + (f" [{i['status']}]" if i.get("status") else ""),
            )
            for i in issues.values()
        ]
        picked = prompt_checkbox(checkbox_items)
        if not picked:
            print("No issues selected.")
            return
        if open_browser:
            for k in picked:
                open_issue_in_browser(cfg, k)
        else:
            picked_items = [(k, label) for k, label in checkbox_items if k in picked]
            prompt_open_in_browser(cfg, picked_items)
        status_name = prompt_status(cfg)
        for key in picked:
            try:
                to = await transition_by_name(session, cloud_id, key, status_name)
                print(f"{key} → {to}")
                print(issue_url(cfg, key))
                remove_week_log_entry(cfg, key)
            except RuntimeError as err:
                print(f"{key}: {err}", file=sys.stderr)


def run_via_cursor_agent(prompt: str) -> int:
    """Optional fallback: Cursor CLI agent with Atlassian MCP (must be enabled in Cursor)."""
    cursor = resolve_cursor_cli()
    if not cursor:
        print("cursor CLI not found.", file=sys.stderr)
        return 1
    cmd = [
        cursor,
        "agent",
        "--print",
        "--output-format",
        "text",
        prompt,
    ]
    return subprocess.call(cmd)


def resolve_cursor_cli() -> str | None:
    env = os.environ.get("CURSOR_CLI", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return str(p)
    w = shutil.which("cursor")
    if w:
        return w
    for candidate in (
        Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor"),
        Path.home() / "Applications/Cursor.app/Contents/Resources/app/bin/cursor",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def parse_cursor_issue_payload(raw: str) -> tuple[str, str]:
    """Expect JSON {summary, description}; return trimmed title + full description body."""
    text = strip_fence(raw.strip())
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("No JSON object in Cursor output") from None
        obj = json.loads(text[start : end + 1])

    summary = str(obj.get("summary", "")).strip().replace("\n", " ")
    desc = str(obj.get("description", "")).strip()
    summary = summary[:120]
    desc = desc[:8000]
    if not summary:
        raise ValueError("Cursor JSON missing summary")
    if not desc:
        desc = summary
    return summary, desc


def expand_issue_via_cursor(
    brief: str,
    timeout_sec: int = 240,
    agent_extra: list[str] | None = None,
) -> tuple[str, str]:
    """
    Ask Cursor agent for a concise Jira summary line + detailed description.
    Requires Cursor CLI (shell command or CURSOR_CLI).

    agent_extra: extra argv tokens after `cursor agent`, e.g. ["--trust"].
    """
    exe = resolve_cursor_cli()
    if not exe:
        raise RuntimeError(
            "Cursor CLI not found. Install Cursor shell command or set CURSOR_CLI "
            "(e.g. /Applications/Cursor.app/Contents/Resources/app/bin/cursor)."
        )

    prompt = f"""You fill Jira fields from rough engineer notes.

Notes:
---
{brief.strip()}
---

Reply with ONLY one JSON object (valid JSON, double-quoted keys and strings, no markdown fences):
{{"summary": "<concise issue title, <= 90 characters>", "description": "<detailed work log, plain text, multiple paragraphs allowed>"}}

Rules:
- summary: clear title / outcome for the issue list (infer from fragments if needed).
- description: expand the notes into a detailed Jira description — what was done, context, and outcome. Use complete sentences; multiple short paragraphs or bullet lines are fine. Stay factual; do not invent teams, tickets, or systems not hinted in the notes.
- No keys other than summary and description."""

    extras = list(agent_extra or ())
    agent_cwd = str(SCRIPT_ROOT)
    try:
        proc = subprocess.run(
            [
                exe,
                "agent",
                *extras,
                "--print",
                "--output-format",
                "text",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=os.environ.copy(),
            cwd=agent_cwd,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"Cursor agent timed out after {timeout_sec}s") from err

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"exit {proc.returncode}: {err[:400]}")

    out = proc.stdout or ""
    if not out.strip():
        raise RuntimeError("Cursor agent returned empty output")

    return parse_cursor_issue_payload(out)


def cmd_cursor_login() -> int:
    """Interactive `cursor agent login` using the same binary resolution as `jira log`."""
    exe = resolve_cursor_cli()
    if not exe:
        print(
            "Cursor CLI not found. Install Cursor.app, then either:\n"
            "  • Cursor → Command Palette → install shell command (adds `cursor` to PATH)\n"
            "  • export CURSOR_CLI=/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
            file=sys.stderr,
        )
        return 1
    print(f"Logging in via: {exe}", file=sys.stderr)
    return subprocess.call([exe, "agent", "login"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Jira CLI via Atlassian Rovo MCP (OAuth, no API tokens in config)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="jira-cli 1.0.0")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="Verify OAuth and save cloud/site config")

    sub.add_parser(
        "cursor-login",
        help="Run Cursor agent login (finds Cursor.app even when `cursor` is not on PATH)",
    )

    fields_p = sub.add_parser(
        "fields",
        help="Required fields for create + sample jiraAdditionalFields (from Jira metadata)",
    )
    fields_p.add_argument("-p", "--project", help="Project key (default from config)")
    fields_p.add_argument(
        "--issue-type",
        help="Issue type name (default from config, usually Task)",
    )
    fields_p.add_argument(
        "--json",
        action="store_true",
        help="Print only suggested jiraAdditionalFields JSON",
    )

    log_p = sub.add_parser(
        "log",
        help="Create issue, assign to you, log time",
        epilog=(
            "By default, Cursor CLI derives a concise issue title + detailed description from your note.\n"
            "Disable with --plain or \"expandLogWithCursor\": false in config.\n"
            "\n"
            "Examples:\n"
            "  jira log \"promo jenkins java21\" --time 2h\n"
            "  jira log \"storm staging smoke ok\" --time 30m --plain\n"
            "  jira log \"note\" --time 1h --cursor --trust   # workspace trust from CLI\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    log_p.add_argument("description", help="What you did (short summary or notes)")
    log_p.add_argument(
        "-t",
        "--time",
        required=True,
        help="Worklog duration: 2h, 30m, 1h 30m (1h30m is normalized to 1h 30m)",
    )
    log_p.add_argument("-p", "--project", help="Project key (default TMD)")
    log_p.add_argument("--type", help="Issue type (default Task)")
    log_p.add_argument("--started", help="Worklog start ISO 8601")
    cursor_group = log_p.add_mutually_exclusive_group()
    cursor_group.add_argument(
        "--cursor",
        action="store_true",
        help="Use Cursor for title/description even if expandLogWithCursor is false in config",
    )
    cursor_group.add_argument(
        "--plain",
        action="store_true",
        help="Skip Cursor; use your raw note as summary + description",
    )
    log_p.add_argument(
        "--trust",
        action="store_true",
        help="Forward --trust to cursor agent (when Workspace Trust blocks non-interactive runs)",
    )
    log_p.add_argument(
        "--yolo",
        action="store_true",
        help="Forward --yolo to cursor agent (trust workspace)",
    )
    log_p.add_argument(
        "-f",
        action="store_true",
        dest="cursor_agent_f",
        help="Forward -f to cursor agent (trust directory)",
    )

    st_p = sub.add_parser("status", help="Transition issue by status name")
    st_p.add_argument("key", help="Issue key")
    st_p.add_argument("status_name", help="e.g. Done")

    eod_p = sub.add_parser("eod", help="End of day: transition issues")
    eod_p.add_argument("--done", help="Comma-separated keys (non-interactive)")
    eod_p.add_argument(
        "--status",
        default=None,
        help="Target status with --done (default from jiraEodDefaultStatus in config, usually DONE)",
    )
    eod_p.add_argument("--no-merge-jql", action="store_true", help="Only week.json (no Jira search)")
    eod_p.add_argument("--list", action="store_true", help="List only, no prompts")
    eod_p.add_argument(
        "--open",
        action="store_true",
        help="Open issue(s) in browser (--list: all listed; interactive: selected; --done: each key)",
    )

    return p


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "cursor-login":
        return cmd_cursor_login()

    cfg = load_config()

    if args.command == "auth":
        await cmd_auth(cfg)
        return 0
    if args.command == "fields":
        await cmd_fields(
            cfg,
            getattr(args, "project", None),
            getattr(args, "issue_type", None),
            bool(getattr(args, "json", False)),
        )
        return 0
    if args.command == "log":
        expand_cfg = bool(cfg.get("expandLogWithCursor"))
        use_cursor = (args.cursor or expand_cfg) and not getattr(args, "plain", False)
        cursor_extras: list[str] = []
        if getattr(args, "trust", False):
            cursor_extras.append("--trust")
        if getattr(args, "yolo", False):
            cursor_extras.append("--yolo")
        if getattr(args, "cursor_agent_f", False):
            cursor_extras.append("-f")
        await cmd_log(
            cfg,
            args.description,
            args.time,
            args.project,
            args.type,
            args.started,
            use_cursor=use_cursor,
            cursor_agent_extra=cursor_extras,
        )
        return 0
    if args.command == "status":
        await cmd_status(cfg, args.key.upper(), args.status_name)
        return 0
    if args.command == "eod":
        done = [k.strip() for k in args.done.split(",")] if args.done else None
        eod_status = args.status or _eod_default_status(cfg)
        await cmd_eod(cfg, done, eod_status, not args.no_merge_jql, args.list, args.open)
        return 0
    return 1


def main() -> None:
    bootstrap_venv()
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130) from None
    except RuntimeError as err:
        print(err, file=sys.stderr)
        _print_jira_create_error_hints(str(err))
        raise SystemExit(1) from err
    except BaseExceptionGroup as group:
        nested_rt = extract_runtime_from_group(group)
        if nested_rt is not None:
            print(nested_rt, file=sys.stderr)
            _print_jira_create_error_hints(str(nested_rt))
            raise SystemExit(1) from None
        if all(
            type(e).__name__ in ("AbortError", "CancelledError", "BrokenResourceError")
            for e in group.exceptions
        ):
            raise SystemExit(0) from None
        raise


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# jira-cli one-shot installer — safe to pipe: curl -fsSL <url>/setup.sh | bash
set -euo pipefail

JIRA_CLI_VERSION="1.0.0"
INSTALL_DIR="${JIRA_CLI_INSTALL_DIR:-${HOME}/.local/share/jira-cli}"
CONFIG_DIR="${HOME}/.config/jira-cli"
CONFIG_FILE="${CONFIG_DIR}/config.json"
STATE_DIR="${HOME}/.local/share/jira-cli-state"
MARKER_START="# >>> jira-cli >>>"
MARKER_END="# <<< jira-cli <<<"

# Git repo containing jira-cli/ (set before curl | bash for remote install)
JIRA_CLI_REPO="${JIRA_CLI_REPO:-}"
# Subdirectory inside repo (default jira-cli)
JIRA_CLI_SUBDIR="${JIRA_CLI_SUBDIR:-jira-cli}"
# Raw GitHub/GitLab base URL to fetch files (alternative to git clone)
# e.g. https://raw.githubusercontent.com/org/dotfiles/main/jira-cli
JIRA_CLI_RAW="${JIRA_CLI_RAW:-}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Where this script lives (when run as a file, not only from pipe)
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "${SCRIPT_PATH}" && "${SCRIPT_PATH}" != "bash" && -f "${SCRIPT_PATH}" ]]; then
  SOURCE_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
else
  SOURCE_DIR=""
fi

fetch_via_git() {
  local repo="$1" dest="$2"
  command -v git >/dev/null 2>&1 || die "git is required. Install git or set JIRA_CLI_RAW to file URLs."
  log "Cloning ${repo} (sparse: ${JIRA_CLI_SUBDIR})..."
  rm -rf "${dest}"
  mkdir -p "${dest}"
  git clone --depth 1 --filter=blob:none --sparse "${repo}" "${dest}/repo"
  (
    cd "${dest}/repo"
    git sparse-checkout set "${JIRA_CLI_SUBDIR}"
  )
  echo "${dest}/repo/${JIRA_CLI_SUBDIR}"
}

fetch_via_raw() {
  local base="$1" dest="$2"
  command -v curl >/dev/null 2>&1 || die "curl is required for JIRA_CLI_RAW install."
  mkdir -p "${dest}/bin"
  log "Downloading jira-cli files from ${base}..."
  for file in jira.py requirements.txt config.example.json; do
    curl -fsSL "${base%/}/${file}" -o "${dest}/${file}"
  done
  curl -fsSL "${base%/}/bin/jira" -o "${dest}/bin/jira"
  echo "${dest}"
}

resolve_source_dir() {
  if [[ -n "${SOURCE_DIR}" && -f "${SOURCE_DIR}/jira.py" ]]; then
    echo "${SOURCE_DIR}"
    return 0
  fi
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "${tmp}"' EXIT

  if [[ -n "${JIRA_CLI_RAW}" ]]; then
    fetch_via_raw "${JIRA_CLI_RAW}" "${tmp}/files"
    return
  fi
  if [[ -n "${JIRA_CLI_REPO}" ]]; then
    fetch_via_git "${JIRA_CLI_REPO}" "${tmp}"
    return
  fi
  die "Cannot find jira.py.
Run from a checkout, or set one of:
  JIRA_CLI_REPO=git@host:org/dotfiles.git bash setup.sh
  JIRA_CLI_RAW=https://raw.../jira-cli bash setup.sh"
}

install_files() {
  local src="$1"
  log "Installing to ${INSTALL_DIR}..."
  mkdir -p "${INSTALL_DIR}/bin"
  cp "${src}/jira.py" "${INSTALL_DIR}/"
  cp "${src}/requirements.txt" "${INSTALL_DIR}/"
  cp "${src}/config.example.json" "${INSTALL_DIR}/"
  cp "${src}/bin/jira" "${INSTALL_DIR}/bin/"
  chmod +x "${INSTALL_DIR}/jira.py" "${INSTALL_DIR}/bin/jira"
}

ensure_python() {
  command -v python3 >/dev/null 2>&1 || die "python3 is required (macOS: xcode-select --install or brew install python)."
  local major
  major="$(python3 -c 'import sys; print(sys.version_info.major)')"
  [[ "${major}" -ge 3 ]] || die "Python 3.10+ required."
}

ensure_node() {
  export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
  if command -v npx >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
    log "Node $(node -v), npx OK"
    return 0
  fi
  warn "npx not found — required for Atlassian OAuth (mcp-remote)."
  if command -v brew >/dev/null 2>&1; then
    log "Installing Node.js via Homebrew..."
    brew install node
    export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
  fi
  command -v npx >/dev/null 2>&1 || die "Install Node.js 18+: brew install node"
}

setup_venv() {
  log "Creating Python venv and installing mcp..."
  python3 -m venv "${INSTALL_DIR}/.venv"
  # shellcheck disable=SC1091
  source "${INSTALL_DIR}/.venv/bin/activate"
  pip install -q --upgrade pip
  pip install -q -r "${INSTALL_DIR}/requirements.txt"
}

setup_config() {
  mkdir -p "${CONFIG_DIR}" "${STATE_DIR}"
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    cp "${INSTALL_DIR}/config.example.json" "${CONFIG_FILE}"
    # Point state dir at dedicated folder (avoid clobbering install dir)
    if command -v python3 >/dev/null 2>&1; then
      python3 <<PY
import json, pathlib
p = pathlib.Path("${CONFIG_FILE}")
cfg = json.loads(p.read_text())
cfg["stateDir"] = "${STATE_DIR}"
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY
    fi
    log "Created ${CONFIG_FILE}"
  else
    log "Config exists: ${CONFIG_FILE}"
  fi
}

run_cursor_agent_login() {
  local jira_bin="${INSTALL_DIR}/bin/jira"
  if [[ "${JIRA_CLI_SKIP_CURSOR_LOGIN:-}" == "1" ]]; then
    log "Skipping Cursor agent login (JIRA_CLI_SKIP_CURSOR_LOGIN=1). Run later: jira cursor-login"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "Non-interactive shell — skip Cursor agent login. After reload run: jira cursor-login"
    return 0
  fi
  if [[ ! -x "${jira_bin}" ]]; then
    warn "jira wrapper missing at ${jira_bin} — skip Cursor agent login."
    return 0
  fi
  log "Cursor agent login (browser; enables jira log title/description expansion)..."
  "${jira_bin}" cursor-login || warn "cursor-login failed — run manually: jira cursor-login"
}

patch_shell_rc() {
  local rc="${HOME}/.zshrc"
  [[ -f "${rc}" ]] || touch "${rc}"

  local block
  block="${MARKER_START}
# jira-cli ${JIRA_CLI_VERSION} — Atlassian MCP CLI (no API tokens)
export JIRA_CLI_HOME=\"${INSTALL_DIR}\"
export PATH=\"\${JIRA_CLI_HOME}/bin:\${PATH}\"
alias jlog='jira log'
alias jeod='jira eod'
alias jstatus='jira status'
${MARKER_END}"

  if grep -q "${MARKER_START}" "${rc}" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    awk -v start="${MARKER_START}" -v end="${MARKER_END}" '
      $0 == start { skip=1; next }
      $0 == end { skip=0; next }
      !skip { print }
    ' "${rc}" > "${tmp}"
    printf '\n%s\n' "${block}" >> "${tmp}"
    mv "${tmp}" "${rc}"
    log "Updated ${rc}"
  else
    printf '\n%s\n' "${block}" >> "${rc}"
    log "Appended to ${rc}"
  fi
}

main() {
  echo ""
  log "jira-cli installer v${JIRA_CLI_VERSION}"
  echo ""

  local src
  src="$(resolve_source_dir)"
  install_files "${src}"
  ensure_python
  ensure_node
  setup_venv
  setup_config
  patch_shell_rc
  run_cursor_agent_login

  echo ""
  log "Done! Reload your shell:"
  echo "  source ~/.zshrc"
  echo ""
  log "Then authenticate (browser OAuth):"
  echo "  jira auth"
  echo ""
  log "If Cursor agent login was skipped, run once for AI summaries on jira log:"
  echo "  jira cursor-login"
  echo ""
  log "Log work:"
  echo "  jira log \"What you did\" --time 2h"
  echo ""
  log "End of day:"
  echo "  jira eod"
  echo ""
}

main "$@"

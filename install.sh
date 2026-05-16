#!/usr/bin/env bash
# Backward-compatible entry — runs full setup.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${DIR}/setup.sh" "$@"

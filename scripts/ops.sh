#!/usr/bin/env bash
# Weekly ops wrapper: every live gate the runbook needs, then the CLI.
# The flags are not secrets (they do nothing without the keys the CLI loads
# from .env); this exists because the Claude Desktop shell cannot read
# .claude/settings.local.json, and the 2026-11-24 run missed three gates.
# Usage (from the repo root): scripts/ops.sh sync --live
set -euo pipefail
if [ "${1:-}" = "serve" ]; then
  echo "scripts/ops.sh refuses serve: the UI's publish endpoints would write live. Use uv run shopsteward serve." >&2
  exit 2
fi
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8
export SHOPSTEWARD_LIVE_ETSY_READ=1
export SHOPSTEWARD_LIVE_ETSY_WRITE=1
export SHOPSTEWARD_LIVE_AUTONOMY=1
export SHOPSTEWARD_LIVE_PLANNER=1
export SHOPSTEWARD_LIVE_COPY=1
export SHOPSTEWARD_LIVE_VISION=1
export SHOPSTEWARD_LIVE_BUFFER=1
export SHOPSTEWARD_LIVE_BUFFER_WRITE=1
exec uv run shopsteward "$@"

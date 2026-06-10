#!/bin/zsh
# Herder worker launchd wrapper.
# Sources the login shell so provider CLIs see the user's PATH + credentials,
# then execs the worker. Install: see launchd/README.md

# Capture the project root FIRST — user rc files (plugins/hooks) may mutate
# $0 or cwd, which previously sent `cd` to the wrong directory under launchd.
HERDER_DIR="$(cd "$(dirname "$0")/.." && pwd)"

set -euo pipefail

# load login env (PATH, API keys exported in ~/.zshrc / ~/.zprofile)
source ~/.zprofile 2>/dev/null || true
source ~/.zshrc 2>/dev/null || true

cd "$HERDER_DIR"                # Herder project root (captured pre-source)
export HERDER_HOME="${HERDER_HOME:-$PWD/.herder}"
[ -f config.yaml ] || { echo "FATAL: config.yaml missing in $PWD" >&2; exit 78; }
# prefer the venv entrypoint directly (saves the uv-wrapper ~26MB RSS);
# fall back to uv run on a fresh checkout without a synced venv
if [ -x .venv/bin/herder ]; then
  exec .venv/bin/herder --config config.yaml worker --interval 10 --worker-id herder-daemon
fi
exec uv run herder --config config.yaml worker --interval 10 --worker-id herder-daemon

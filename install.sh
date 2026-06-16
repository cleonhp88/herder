#!/bin/sh
# Herder bootstrap — installs the CLI via uv tool install.
#
# Usage (from repo clone):
#   sh install.sh
#
# Usage (one-liner, after repo is on GitHub):
#   curl -LsSf https://raw.githubusercontent.com/<USER>/<REPO>/main/install.sh | sh
#
# What this script does:
#   1. Checks for / installs uv (the package manager used to install Herder).
#   2. Installs the herder CLI via uv tool install.
#   3. Prints next steps.
#
# Nothing is deleted outside a temporary directory this script creates.

set -eu

# ---------------------------------------------------------------------------
# PLACEHOLDER — replace with the real GitHub URL once you push the repo.
# Example: https://github.com/yourname/herder
HERDER_REPO="${HERDER_REPO:-PLACEHOLDER_GIT_URL}"
# ---------------------------------------------------------------------------

HERDER_BANNER="Herder bootstrap — installs the 'herder' CLI via uv."
echo ""
echo "$HERDER_BANNER"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Ensure uv is available
# ---------------------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found — installing it now."
    echo "  Will run: curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  (Downloads and runs an install script from astral.sh — a trusted publisher of uv.)"
    echo ""
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo ""

    # uv installs into ~/.local/bin or ~/.cargo/bin; refresh PATH for this session.
    UV_HOME="${UV_HOME:-$HOME/.local/bin}"
    case ":${PATH}:" in
        *":${UV_HOME}:"*) ;;
        *) export PATH="${UV_HOME}:${PATH}" ;;
    esac

    if ! command -v uv >/dev/null 2>&1; then
        echo "error: uv was installed but is not on PATH." >&2
        echo "Add it to PATH (e.g. ~/.local/bin) and re-run this script." >&2
        exit 1
    fi
fi

echo "uv found: $(uv --version)"

# ---------------------------------------------------------------------------
# Step 2: Install herder
# ---------------------------------------------------------------------------

if [ -f "pyproject.toml" ]; then
    # Running from inside a repo clone — install directly.
    echo "Installing from local repo (pyproject.toml found in current directory)..."
    uv tool install .
else
    # Running via curl | sh or from outside the repo — clone into a temp dir.
    if [ "$HERDER_REPO" = "PLACEHOLDER_GIT_URL" ]; then
        echo "error: HERDER_REPO is not set and pyproject.toml was not found." >&2
        echo "Either:" >&2
        echo "  1. Run this script from inside the cloned repo directory, OR" >&2
        echo "  2. Set HERDER_REPO=https://github.com/<user>/herder before running." >&2
        exit 1
    fi

    TMPDIR_CLONE="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_CLONE"' EXIT
    echo "Cloning $HERDER_REPO into temporary directory..."
    git clone --depth 1 "$HERDER_REPO" "$TMPDIR_CLONE/herder"
    echo "Installing from cloned repo..."
    uv tool install "$TMPDIR_CLONE/herder"
fi

# ---------------------------------------------------------------------------
# Step 3: Confirm and print next steps
# ---------------------------------------------------------------------------

echo ""
echo "Herder installed successfully."
echo ""
echo "Next steps:"
echo "  1.  herder init          # first-run setup: config + brain wiring + doctor"
echo "  2.  herder add           # connect your first AI-agent hand"
echo "  3.  Open Claude Code or Codex — it reads the cheat-sheet and calls the hands."
echo ""
echo "Need help? See README.md or run: herder --help"

#!/bin/bash
# Double-click this file in Finder to launch the docsearch web UI.
# On first run it creates the Python venv and installs the package.

set -e

# Finder launches scripts with cwd=$HOME — re-anchor to this file's directory
# so relative paths (.venv, pyproject.toml) resolve correctly.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed. Install Python 3.9+ and try again."
  echo "Press any key to close…"
  read -n 1
  exit 1
fi

# --- update check (silent if offline or git unavailable) -------------------
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if git fetch --quiet origin 2>/dev/null; then
    LOCAL=$(git rev-parse HEAD 2>/dev/null)
    REMOTE=$(git rev-parse origin/main 2>/dev/null)
    if [ -n "$LOCAL" ] && [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
      echo "-----------------------------------------------------------"
      echo "  Update available ($(git rev-parse --short HEAD)  ->  $(git rev-parse --short origin/main))"
      echo "  To update, run in Terminal:"
      echo "    cd \"$(pwd)\" && git pull && pip install -e ."
      echo "-----------------------------------------------------------"
      echo
    fi
  fi
fi
# ---------------------------------------------------------------------------

if [ ! -d ".venv" ]; then
  echo "First-time setup: creating .venv and installing docsearch…"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -e .
  echo "Setup complete."
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo
exec docsearch-web

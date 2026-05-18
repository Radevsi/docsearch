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
      echo "Update available — installing $(git rev-parse --short HEAD) → $(git rev-parse --short origin/main)…"
      if git pull --quiet origin main 2>/dev/null; then
        source .venv/bin/activate 2>/dev/null || true
        pip install --quiet -e . && echo "Updated. Launching…" || echo "pip install failed — launching existing version."
      else
        echo "Update failed (local changes?) — launching existing version."
      fi
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

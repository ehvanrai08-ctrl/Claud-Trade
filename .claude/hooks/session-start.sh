#!/bin/bash
set -euo pipefail

# Only run in Claude Code on the web (remote) environments.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Install Python dependencies so the bots, status.py, py_compile and pyflakes
# all work immediately in a fresh web session. Idempotent; benefits from the
# container's post-hook cache.
pip install -r requirements.txt

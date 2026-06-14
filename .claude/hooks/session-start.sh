#!/bin/bash
# SessionStart hook: ensure the GitNexus index is built with full-text search (FTS).
#
# The LadybugDB FTS extension is not pre-installed in fresh containers, and the
# default install policy ("load-only") never reaches the network — so FTS search
# indexes get skipped. Setting GITNEXUS_LBUG_EXTENSION_INSTALL=auto makes analyze
# load-then-install the extension once with network access, then build the
# full-text/BM25 search indexes.
set -euo pipefail

# Web/remote sessions only — local sessions manage their own index.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Persist the auto install policy for every GitNexus invocation this session,
# so manual `analyze`/`serve` calls also build/use FTS.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export GITNEXUS_LBUG_EXTENSION_INSTALL=auto' >> "$CLAUDE_ENV_FILE"
fi

export GITNEXUS_LBUG_EXTENSION_INSTALL=auto

cd "${CLAUDE_PROJECT_DIR:-.}"

# Build/refresh the index with FTS. Idempotent: analyze is incremental, and the
# extension install is a no-op once cached. Prefer the project-local runner if
# present, else fall back to npx.
if [ -f .gitnexus/run.cjs ]; then
  node .gitnexus/run.cjs analyze || echo "GitNexus analyze failed; continuing session without fresh FTS index" >&2
else
  npx --yes gitnexus analyze || echo "GitNexus analyze failed; continuing session without fresh FTS index" >&2
fi

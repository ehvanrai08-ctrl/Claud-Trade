# GitNexus — Local Web UI & Full-Text Search

GitNexus indexes this repo for code intelligence (impact analysis, flow tracing,
full-text/BM25 search). The index lives in `.gitnexus/` and is **gitignored** —
it's rebuilt locally in seconds, not committed.

## Open the web UI (run on your own machine)

The web UI server binds to `localhost`, so it must run on the machine whose
browser you'll use. It is **not** reachable from Claude Code on the web sessions,
which run in isolated containers with no inbound networking.

```bash
# 1. Build the index with full-text search enabled.
#    GITNEXUS_LBUG_EXTENSION_INSTALL=auto installs the LadybugDB FTS extension
#    once (needs network); without it, FTS/BM25 search is skipped.
GITNEXUS_LBUG_EXTENSION_INSTALL=auto npx gitnexus analyze

# 2. Start the local server.
npx gitnexus serve            # add --port <n> or --host 0.0.0.0 if needed
```

Then open <http://localhost:4747> in your browser.

> npm 11 `npx` crash (`node.target is null`)? Install once with
> `npm i -g gitnexus` and use `gitnexus analyze` / `gitnexus serve`. See
> [GitNexus #1939](https://github.com/abhigyanpatwari/GitNexus/issues/1939).

## Full-text search in remote (web) sessions

FTS for the in-session MCP tools is handled automatically by the SessionStart
hook (`.claude/hooks/session-start.sh`), which sets
`GITNEXUS_LBUG_EXTENSION_INSTALL=auto` and runs `analyze` on startup. No web UI
is involved there — only the MCP tools the agent uses.

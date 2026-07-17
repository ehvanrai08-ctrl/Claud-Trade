"""
Project Optimizer Agent — the always-on "make this project better" loop.
========================================================================
A continuously-running agent that surveys the WHOLE codebase and improves it in
any way it can: dead-code cleanup, bug fixes, refactors, doc gaps, missing error
handling, new strategy ideas, performance, safety hardening. It is the meta-bot
that works on the *project*, where post_market_analysis.py works on the *trades*.

Runs on a schedule (a few times a week) via .github/workflows/project_optimizer.yml.

Each run:
  1. survey_codebase()  — read every .py file, gather metrics, collect pyflakes
     warnings and TODO/FIXME markers. Deterministic, no API needed.
  2. brainstorm()       — send the survey + selected source to Claude, which
     returns (a) a prioritized improvement backlog and (b) concrete FILE/OLD/NEW
     code patches it is confident are safe.
  3. apply_patches()    — apply only the safe patches, behind the SAME two gates
     the post-market bot uses (py_compile + pyflakes "undefined name"), rolling
     back anything that fails. The rest become backlog items, never silent edits.
  4. update_backlog()   — merge new ideas into IMPROVEMENT_BACKLOG.md (deduped,
     priority-sorted, capped), and write a run summary.

Self-protection (so the loop can never break its own recoverability):
  - It NEVER patches itself (project_optimizer.py) or the analysis engine
    (post_market_analysis.py) — same principle as the post-market loop.
  - It NEVER touches the live trading logic of a bot that currently holds a
    position without flagging it to the backlog instead (no surprise edits to
    code that is mid-trade).
  - Every change is a git commit, so any bad edit is one `git revert` away.
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TODAY = datetime.utcnow().strftime("%Y-%m-%d")

BACKLOG_FILE = f"{BASE_DIR}/IMPROVEMENT_BACKLOG.md"
MAX_BACKLOG  = 40   # keep the file (and the prompt context) bounded

# Files the optimizer must NEVER edit — its own engine + the other meta-engine.
# Editing these could break the recoverability guarantee or the safety gates.
PROTECTED = {"project_optimizer.py", "post_market_analysis.py", "bug_hunter.py"}

# Files that should never be auto-patched even if suggested — the workflow YAMLs
# and requirements are infra; a bad edit takes the whole fleet down.
NEVER_AUTOPATCH = PROTECTED | {"requirements.txt"}

import requests


# ── 1. Survey the codebase (deterministic, no API) ────────────────────────────

def list_py_files():
    """All .py files in the repo root (the bots + helpers), sorted."""
    return sorted(
        f for f in os.listdir(BASE_DIR)
        if f.endswith(".py") and os.path.isfile(f"{BASE_DIR}/{f}")
    )


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def pyflakes_warnings():
    """All pyflakes warnings across the repo (unused imports, undefined names,
    redefinitions, f-strings without placeholders, etc.) — the cheap, real signal
    for cleanup. Grouped by file."""
    files = [f"{BASE_DIR}/{f}" for f in list_py_files()]
    out = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                         capture_output=True, text=True)
    warnings = {}
    for line in (out.stdout + out.stderr).splitlines():
        # format: /path/file.py:LINE: message
        m = re.match(r".*/([^/]+\.py):(\d+):?\d*:?\s*(.*)", line)
        if m:
            fname, lineno, msg = m.group(1), m.group(2), m.group(3)
            warnings.setdefault(fname, []).append(f"L{lineno}: {msg}")
    return warnings


def grep_markers():
    """TODO / FIXME / XXX / HACK markers — explicit debt the authors left behind."""
    markers = {}
    for fname in list_py_files():
        if fname in PROTECTED:
            continue   # skip our own marker-detection source (self-reference noise)
        for i, line in enumerate(read_file(f"{BASE_DIR}/{fname}").splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line):
                markers.setdefault(fname, []).append(f"L{i}: {line.strip()[:100]}")
    return markers


def survey_codebase():
    """Build a compact, deterministic survey of the project's health."""
    files = list_py_files()
    sizes = {f: len(read_file(f"{BASE_DIR}/{f}").splitlines()) for f in files}
    flakes = pyflakes_warnings()
    markers = grep_markers()

    lines = [f"# Codebase survey — {TODAY}", ""]
    lines.append(f"{len(files)} Python files, {sum(sizes.values())} total lines.")
    lines.append("")
    lines.append("## File sizes (lines)")
    for f in sorted(sizes, key=lambda k: sizes[k], reverse=True):
        flag = "  ← large, candidate for refactor" if sizes[f] > 500 else ""
        lines.append(f"  {f:30} {sizes[f]:5}{flag}")

    lines.append("")
    lines.append(f"## pyflakes warnings ({sum(len(v) for v in flakes.values())} total)")
    if flakes:
        for f, ws in sorted(flakes.items()):
            lines.append(f"  {f}:")
            for w in ws[:10]:
                lines.append(f"    {w}")
    else:
        lines.append("  (none — clean)")

    lines.append("")
    lines.append(f"## TODO/FIXME markers ({sum(len(v) for v in markers.values())} total)")
    if markers:
        for f, ms in sorted(markers.items()):
            lines.append(f"  {f}:")
            for m in ms[:8]:
                lines.append(f"    {m}")
    else:
        lines.append("  (none)")

    return "\n".join(lines), flakes, markers


# ── 2. Brainstorm via Claude ──────────────────────────────────────────────────

def read_backlog_items():
    """Existing backlog entries (the '- [P?] ...' bullet lines) for dedup."""
    if not os.path.exists(BACKLOG_FILE):
        return []
    items = []
    for ln in read_file(BACKLOG_FILE).splitlines():
        s = ln.strip()
        if s.startswith("- ["):
            items.append(s)
    return items


def brainstorm(survey):
    """Ask Claude for a prioritized backlog + safe code patches.

    We send the survey plus a rotating subset of source files (the prompt can't
    hold everything). Claude returns two clearly delimited sections so we can
    apply patches automatically and file the rest as backlog ideas.
    """
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    # Include source for the smaller, non-protected files so Claude can write
    # exact patches. Skip the protected engines and the huge backtests.
    src_blocks = []
    budget = 0
    for f in list_py_files():
        if f in PROTECTED or f.startswith("backtest_"):
            continue
        code = read_file(f"{BASE_DIR}/{f}")
        if budget + len(code) > 60_000:   # keep the prompt bounded
            continue
        budget += len(code)
        src_blocks.append(f"<file name=\"{f}\">\n{code}\n</file>")

    existing = read_backlog_items()
    existing_block = "\n".join(existing) if existing else "(empty)"

    prompt = f"""You are a senior engineer doing continuous improvement on an automated paper-trading project. Your job is to make this project better in ANY way — cleanup, bug fixes, refactors, missing error handling, doc gaps, performance, safety hardening, or genuinely new strategy/feature ideas.

Here is a deterministic survey of the codebase:
<survey>
{survey}
</survey>

Here is the source of the smaller, editable files:
{chr(10).join(src_blocks)}

Here is the EXISTING improvement backlog (do NOT repeat these — only add genuinely new ideas):
<backlog>
{existing_block}
</backlog>

IMPORTANT CONSTRAINTS:
- NEVER suggest editing project_optimizer.py, post_market_analysis.py, or bug_hunter.py (protected engines).
- NEVER suggest editing .yml workflow files or requirements.txt via a patch.
- Auto-patches must be SAFE, mechanical, and obviously correct (e.g. remove an unused import flagged by pyflakes, fix a typo in a comment, tighten an except clause, add a missing .get() default). Anything touching live trading LOGIC goes in the backlog, NOT a patch.

Produce TWO sections, exactly in this format:

=== PATCHES ===
(Zero or more safe, mechanical patches. For each:)
FILE: <filename>
OLD:
```
<exact existing code>
```
NEW:
```
<exact replacement>
```
(Output ONLY your final version of each patch — never a draft you then revise. Each location at most once. If you are not 100% sure the OLD matches verbatim, put it in the backlog instead.)

=== BACKLOG ===
(A prioritized list of improvement ideas — anything from refactors to new strategies to test ideas. One per line, format:)
- [P1] <high-impact idea, concise>
- [P2] <medium idea>
- [P3] <nice-to-have>
(P1=high impact/low risk, P2=medium, P3=nice-to-have. 5–15 items. Be specific and actionable.)"""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-opus-4-8",
            "max_tokens": 4096,
            "messages":   [{"role": "user", "content": prompt}],
        },
    )
    if r.ok:
        return r.json()["content"][0]["text"], None
    return None, f"Claude API error: {r.status_code} {r.text[:300]}"


# ── 3. Apply safe patches (same two gates as the post-market bot) ─────────────

def apply_patches(text):
    """Parse the === PATCHES === section and apply each behind two gates:
      Gate 1 — py_compile: rejects syntax errors.
      Gate 2 — pyflakes 'undefined name': rejects missing-import/typo crashes.
    Protected and infra files are skipped outright. Returns (applied, rejected).
    """
    import py_compile

    # Only look inside the PATCHES section so a backlog mention of FILE: can't
    # be mistaken for a patch.
    patch_section = text
    if "=== PATCHES ===" in text:
        patch_section = text.split("=== PATCHES ===", 1)[1]
        if "=== BACKLOG ===" in patch_section:
            patch_section = patch_section.split("=== BACKLOG ===", 1)[0]

    pattern = r"FILE:\s*(\S+)\nOLD:\n```[^\n]*\n(.*?)```\nNEW:\n```[^\n]*\n(.*?)```"
    patches = re.findall(pattern, patch_section, re.DOTALL)
    # Most-specific (longest OLD) first, so a full revision wins over a draft.
    patches.sort(key=lambda p: len(p[1]), reverse=True)

    applied, rejected = [], []
    for filename, old_code, new_code in patches:
        fname = filename.strip()
        if fname in NEVER_AUTOPATCH or fname.endswith((".yml", ".yaml")):
            rejected.append((fname, "protected/infra — filed to backlog instead"))
            continue
        filepath = f"{BASE_DIR}/{fname}"
        if not os.path.exists(filepath):
            rejected.append((fname, "file not found"))
            continue
        original = read_file(filepath)
        if old_code.strip() not in original:
            rejected.append((fname, "OLD snippet did not match current code"))
            continue
        patched = original.replace(old_code.strip(), new_code.strip(), 1)
        with open(filepath, "w") as f:
            f.write(patched)
        # Gate 1: must still compile.
        try:
            py_compile.compile(filepath, doraise=True)
        except py_compile.PyCompileError as e:
            with open(filepath, "w") as f:
                f.write(original)
            rejected.append((fname, f"syntax error, reverted: {str(e)[:100]}"))
            continue
        # Gate 2: no new undefined names (would crash at runtime).
        flakes = subprocess.run([sys.executable, "-m", "pyflakes", filepath],
                                capture_output=True, text=True)
        if "undefined name" in (flakes.stdout + flakes.stderr):
            with open(filepath, "w") as f:
                f.write(original)
            reason = next((ln for ln in flakes.stdout.splitlines()
                           if "undefined name" in ln), "undefined name")
            rejected.append((fname, f"undefined name, reverted: {reason[-80:]}"))
            continue
        applied.append(fname)
    return applied, rejected


# ── 4. Maintain the backlog ───────────────────────────────────────────────────

def parse_backlog(text):
    """Pull '- [P?] ...' lines out of the === BACKLOG === section."""
    section = text
    if "=== BACKLOG ===" in text:
        section = text.split("=== BACKLOG ===", 1)[1]
    items = []
    for ln in section.splitlines():
        s = ln.strip()
        if re.match(r"- \[P[123]\]", s):
            items.append(s)
    return items


def _priority(item):
    m = re.search(r"\[P([123])\]", item)
    return int(m.group(1)) if m else 9


def _dedup_key(item):
    """Normalize a backlog line to a comparison key: drop the leading '- ', the
    '[P?]' priority tag, and case/whitespace, so the same idea at any priority
    dedups against itself."""
    s = item.strip().lstrip("-").strip()
    s = re.sub(r"\[P[123]\]\s*", "", s)
    return s.lower().strip()


_STOPWORDS = {"the", "a", "an", "in", "to", "so", "and", "or", "of", "for",
              "with", "on", "is", "it", "that", "add", "should", "can", "be"}


def _tokens(item):
    # Split on dots too, so `broker.market_is_open` and `market_is_open` in
    # `broker.py` count as the same concept tokens.
    return {w for w in re.findall(r"[a-z_][a-z0-9_]+", _dedup_key(item))
            if w not in _STOPWORDS}


def _is_duplicate(item, kept):
    """Fuzzy dedup: the model rephrases the same idea each run, so exact-text
    matching lets near-duplicates pile up until they fill the cap. Two items
    are the same idea when their content-token overlap (Jaccard) is high."""
    t = _tokens(item)
    if not t:
        return True
    for other in kept:
        o = _tokens(other)
        if not o:
            continue
        # Overlap coefficient (shared / smaller set) beats Jaccard here: a
        # rephrasing adds filler words that dilute the union but not the core.
        overlap = len(t & o) / min(len(t), len(o))
        if overlap >= 0.6:
            return True
    return False


def update_backlog(new_items, applied, rejected):
    """Merge new ideas into the backlog (fuzzy dedup, priority-sort, cap),
    prepend a run summary. Returns the count of genuinely-new items added."""
    existing_raw = read_backlog_items()
    # One-time self-clean: collapse near-duplicates already in the file.
    existing = []
    for i in existing_raw:
        if not _is_duplicate(i, existing):
            existing.append(i)
    fresh = []
    for i in new_items:
        if _dedup_key(i) and not _is_duplicate(i, existing + fresh):
            fresh.append(i)

    merged = sorted(existing + fresh, key=_priority)[:MAX_BACKLOG]

    header = (
        "# Improvement Backlog\n\n"
        "Continuously maintained by `project_optimizer.py`. Priority: "
        "P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. "
        f"Capped at {MAX_BACKLOG} items.\n\n"
        f"_Last run: {TODAY} — {len(applied)} patch(es) auto-applied, "
        f"{len(fresh)} new idea(s) filed._\n\n"
    )
    body = "\n".join(merged) if merged else "_(empty — nothing queued)_"

    run_log = ["\n\n---\n", f"## Run log {TODAY}"]
    if applied:
        run_log.append(f"- ✅ Auto-applied: {', '.join(applied)}")
    if rejected:
        for fname, reason in rejected:
            run_log.append(f"- ⏭️  Skipped {fname}: {reason}")
    if not applied and not rejected:
        run_log.append("- No patches this run (ideas filed to backlog only).")

    with open(BACKLOG_FILE, "w") as f:
        f.write(header + body + "\n".join(run_log) + "\n")

    return len(fresh)


# ── Orchestration ─────────────────────────────────────────────────────────────

def run():
    print("[OPTIMIZER] Surveying codebase…", flush=True)
    survey, flakes, markers = survey_codebase()
    print(survey, flush=True)

    if not ANTHROPIC_KEY:
        # Still useful without the API: write the deterministic survey + any
        # pyflakes/TODO debt straight into the backlog as P2 items.
        debt_items = []
        for f, ws in flakes.items():
            debt_items.append(f"- [P2] Clean {len(ws)} pyflakes warning(s) in {f}")
        for f, ms in markers.items():
            debt_items.append(f"- [P3] Resolve {len(ms)} TODO/FIXME in {f}")
        added = update_backlog(debt_items, [], [])
        print(f"[OPTIMIZER] No API key — filed {added} debt item(s) from survey.", flush=True)
        return

    print("[OPTIMIZER] Brainstorming improvements via Claude…", flush=True)
    text, err = brainstorm(survey)
    if err:
        print(f"[OPTIMIZER] Brainstorm failed: {err}", flush=True)
        return

    applied, rejected = apply_patches(text)
    print(f"[OPTIMIZER] Applied {len(applied)} patch(es), rejected {len(rejected)}.", flush=True)
    for fname in applied:
        print(f"  ✅ {fname}", flush=True)
    for fname, reason in rejected:
        print(f"  ⏭️  {fname}: {reason}", flush=True)

    new_items = parse_backlog(text)
    added = update_backlog(new_items, applied, rejected)
    print(f"[OPTIMIZER] Filed {added} new backlog idea(s). See {BACKLOG_FILE}", flush=True)


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
"""SessionStart hook: auto-recall.

Claude Code runs this when a session starts and injects whatever we print (as
`additionalContext`) into the model's context. So every session begins already
knowing your standing preferences and this project's key memories — without
Claude having to remember to search.

Fast on purpose: it only LISTS memories (no embeddings, no model load), so it
adds no startup latency. Never throws — on any error it stays silent so it can't
break your session.

Installed by `python3 setup.py hooks`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_PREFS = 10
MAX_PROJECT = 10


def build_context(cwd: str) -> str:
    # Read directly (DB query + file frontmatter) — NO embeddings, so this stays
    # instant even when the project already has lots of memories.
    from graph_memory import GraphMemory, default_db_path
    from project import find_repo_root, project_key, parse

    g = GraphMemory(default_db_path())  # opening doesn't embed
    grows = [dict(r) for r in g.db.execute(
        "SELECT id,label,summary,type,importance FROM nodes")]
    g.close()

    project_name, proj_rows = None, []
    repo = find_repo_root(Path(cwd)) if cwd else None
    if repo:
        project_name = project_key(repo)
        for f in sorted((repo / ".cc-mem" / "nodes").glob("*.md")):
            try:
                meta, _ = parse(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            meta["id"] = f"p:{meta.get('id', f.stem)}"
            proj_rows.append(meta)

    prefs = [{"id": f"g:{r['id']}", **r} for r in grows if r.get("type") == "preference"]
    prefs += [r for r in proj_rows if r.get("type") == "preference"]
    prefs = prefs[:MAX_PREFS]
    proj = sorted([r for r in proj_rows if r.get("type") != "preference"],
                  key=lambda r: r.get("importance", 1.0), reverse=True)[:MAX_PROJECT]
    ctx = {"project_key": project_name}
    pending = _pending_count()

    lines = ["# cc-mem — recalled memory for this session"]
    lines.append(f"Project: {ctx['project_key'] or '(no git repo — global memory only)'}")
    if prefs:
        lines.append("\n## Standing preferences — APPLY these (don't just recall):")
        for r in prefs:
            lines.append(f"- [{r['id']}] {r.get('summary') or r.get('label')}")
    if proj:
        lines.append("\n## This project's key memories:")
        for r in proj:
            lines.append(f"- [{r['id']}] {r.get('label','')} — {r.get('summary','')}")
    if not prefs and not proj:
        lines.append("\n(No memories yet for this scope — save verified learnings as you go.)")
    if pending:
        lines.append(f"\n⚠ {pending} captured memory proposal(s) await review in the cc-mem UI.")
    lines.append("\nUse `memory_search`/`memory_recall` for more, `memory_get <id>` for full content.")
    return "\n".join(lines)


def _pending_count() -> int:
    try:
        from graph_memory import default_db_path
        pend = default_db_path().parent / "pending"
        return len(list(pend.glob("*.json"))) if pend.exists() else 0
    except Exception:
        return 0


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    cwd = payload.get("cwd") or payload.get("workspace") or ""
    try:
        context = build_context(cwd)
    except Exception:
        return  # stay silent; never break the session
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()

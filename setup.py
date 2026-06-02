#!/usr/bin/env python3
"""cc-mem installer — set everything up from inside the repo.

Pull the repo, then:

    python3 setup.py all       # deps + register MCP + install prompt (everything)
    python3 setup.py prompt    # just sync the memory-loop prompt into CLAUDE.md
    python3 setup.py mcp        # just (re)register the MCP server
    python3 setup.py deps       # just create .venv + install the embedder model dep

The prompt lives in this repo at prompt/memory-loop.md (the single source of
truth). `setup.py prompt` injects it into your Claude config(s) between marker
comments, so re-running UPDATES the block in place instead of duplicating — edit
the file, re-run, done. No hunting through ~/.claude by hand.

Targets are auto-detected: ~/.claude, $CLAUDE_CONFIG_DIR, and ~/.claude-work if
present. Override with --target /path/to/CLAUDE.md (repeatable).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
PROMPT_FILE = REPO / "prompt" / "memory-loop.md"
START = "<!-- cc-mem:start (managed by setup.py — edit prompt/memory-loop.md, re-run) -->"
END = "<!-- cc-mem:end -->"

VENV_PY = REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DEFAULT_DB = Path.home() / ".claude-cc-mem" / "memory.db"


# ── config-dir discovery ──────────────────────────────────────────────────────

def config_dirs() -> list[Path]:
    dirs = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path.home() / ".claude")
    work = Path.home() / ".claude-work"
    if work.exists():
        dirs.append(work)
    # de-dupe, preserve order
    seen, out = set(), []
    for d in dirs:
        r = d.resolve()
        if r not in seen:
            seen.add(r)
            out.append(d)
    return out


def prompt_targets() -> list[Path]:
    """Where to write the prompt. ALWAYS ~/.claude/CLAUDE.md (the user memory file
    Claude Code reads, including under a custom CLAUDE_CONFIG_DIR). We add other
    config dirs' CLAUDE.md ONLY if they already exist — creating a new one there
    could shadow ~/.claude/CLAUDE.md and silently drop your other global rules."""
    out = [Path.home() / ".claude" / "CLAUDE.md"]
    for d in config_dirs():
        f = d / "CLAUDE.md"
        if f.exists() and f.resolve() not in {t.resolve() for t in out}:
            out.append(f)
    return out


# ── prompt sync (idempotent, marker-based) ────────────────────────────────────

def sync_prompt(targets: list[Path]) -> None:
    body = PROMPT_FILE.read_text(encoding="utf-8").strip()
    block = f"{START}\n{body}\n{END}"
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if START in existing and END in existing:
            pre = existing[: existing.index(START)]
            post = existing[existing.index(END) + len(END):]
            updated = f"{pre}{block}{post}"
            action = "updated"
        elif existing.strip():
            updated = f"{existing.rstrip()}\n\n{block}\n"
            action = "appended to"
        else:
            updated = f"{block}\n"
            action = "created"
        path.write_text(updated, encoding="utf-8")
        print(f"  prompt {action}: {path}")


# ── MCP registration ──────────────────────────────────────────────────────────

def register_mcp(dirs: list[Path]) -> None:
    if not _has_claude():
        print("  ! 'claude' CLI not found — skipping MCP registration.")
        print(f"    Add manually (per config dir): a stdio server 'cc-mem' running")
        print(f"    {VENV_PY} {REPO/'mcp_server.py'}  with CC_MEM_EMBEDDER=local")
        return
    for d in dirs:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(d)}
        # remove first so re-running updates cleanly (ignore failure)
        subprocess.run(["claude", "mcp", "remove", "cc-mem", "-s", "user"],
                       env=env, capture_output=True)
        r = subprocess.run(
            ["claude", "mcp", "add", "cc-mem", "--scope", "user",
             "-e", "CC_MEM_EMBEDDER=local", "-e", f"CC_MEM_DB={DEFAULT_DB}",
             "--", str(VENV_PY), str(REPO / "mcp_server.py")],
            env=env, capture_output=True, text=True)
        ok = "Added" in (r.stdout + r.stderr)
        print(f"  mcp {'registered' if ok else 'FAILED'} in {d}"
              + ("" if ok else f"  ({r.stderr.strip().splitlines()[-1:]})"))


def _has_claude() -> bool:
    return subprocess.run(["which", "claude"] if os.name != "nt" else ["where", "claude"],
                          capture_output=True).returncode == 0


# ── hooks (auto-recall on SessionStart; opt-in deterministic recall on prompt) ─

def _hook_cmd(script: str, embedder: bool) -> str:
    env = f"CC_MEM_DB={DEFAULT_DB} "
    if embedder:
        env += "CC_MEM_EMBEDDER=local "
    return f"{env}{VENV_PY} {REPO / 'hooks' / script}"


_HOOKS_MARKER = str(REPO / "hooks")  # our hook commands all reference this dir


def _scrub(groups: list) -> list:
    """Drop any hook group that points at THIS repo's hooks/ scripts (so re-runs
    update cleanly instead of stacking duplicates) — leaves unrelated hooks be."""
    return [g for g in groups
            if not any(_HOOKS_MARKER in h.get("command", "")
                       for h in g.get("hooks", []))]


def _set(hooks: dict, event: str, command: str | None) -> None:
    groups = _scrub(hooks.get(event, []))
    if command:
        groups.append({"hooks": [{"type": "command", "command": command}]})
    if groups:
        hooks[event] = groups
    elif event in hooks:
        del hooks[event]


def install_hooks(dirs: list[Path], recall: bool) -> None:
    for d in dirs:
        settings = d / "settings.json"
        try:
            data = json.loads(settings.read_text()) if settings.exists() else {}
        except Exception:
            print(f"  ! {settings} is not valid JSON — skipping"); continue
        hooks = data.setdefault("hooks", {})
        _set(hooks, "SessionStart", _hook_cmd("session_start.py", False))   # always: auto-recall
        # SessionEnd is intentionally NOT installed: it used to spawn a headless
        # `claude -p` per session, which burned Claude usage. Pass None so re-runs
        # also SCRUB any previously-installed capture hook from settings.json.
        _set(hooks, "SessionEnd", None)
        _set(hooks, "UserPromptSubmit", _hook_cmd("prompt_recall.py", True) if recall else None)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
        extras = "+".join(["recall-on-start"] + (["prompt-recall"] if recall else []))
        print(f"  hooks ({extras}) -> {settings}")
    if not recall:
        print("  note: --recall adds UserPromptSubmit deterministic auto-recall "
              "(runs a warm background daemon; semantic search on every prompt — "
              "a local Python process, never a Claude agent).")


# ── deps ──────────────────────────────────────────────────────────────────────

def install_deps() -> None:
    if not VENV_PY.exists():
        print("  creating .venv …")
        subprocess.run([sys.executable, "-m", "venv", str(REPO / ".venv")], check=True)
    print("  installing sentence-transformers (semantic embedder) …")
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "-q",
                    "sentence-transformers"], check=True)
    print("  deps installed.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="cc-mem installer")
    ap.add_argument("command", choices=["all", "prompt", "mcp", "deps", "hooks"])
    ap.add_argument("--target", action="append", type=Path,
                    help="explicit CLAUDE.md path for `prompt` (repeatable)")
    ap.add_argument("--recall", action="store_true",
                    help="for `hooks`/`all`: also install the UserPromptSubmit auto-recall hook")
    args = ap.parse_args()

    if args.command in ("deps", "all"):
        print("• deps"); install_deps()
    if args.command in ("mcp", "all"):
        print("• mcp"); register_mcp(config_dirs())
    if args.command in ("prompt", "all"):
        print("• prompt")
        targets = args.target or prompt_targets()
        sync_prompt(targets)
    if args.command in ("hooks", "all"):
        print("• hooks"); install_hooks(config_dirs(), recall=args.recall)
    print("done.")


if __name__ == "__main__":
    main()

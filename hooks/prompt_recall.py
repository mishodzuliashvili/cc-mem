#!/usr/bin/env python3
"""UserPromptSubmit hook: deterministic auto-recall.

Fires on every prompt. Runs a semantic search for the prompt against the warm
recall daemon and injects the top hits as context BEFORE Claude responds — so
relevant memories are present whether or not Claude remembers to search.

Non-blocking: if the daemon isn't running yet it spawns it (detached) and skips
this one turn — so a prompt is never delayed more than a socket round-trip. Once
warm, recall is near-instant. Never throws (a hook must not break the prompt).

Installed (opt-in) by `python3 setup.py hooks --recall`.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MIN_SCORE = 0.3   # don't inject weak/irrelevant hits
MAX_HITS = 5


def sock_path() -> Path:
    from graph_memory import default_db_path
    return default_db_path().parent / "recall.sock"


def query_daemon(sock: Path, prompt: str, cwd: str) -> list | None:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(4.0)
        s.connect(str(sock))
        s.sendall((json.dumps({"query": prompt, "cwd": cwd, "k": MAX_HITS}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.decode("utf-8")).get("hits", [])
    except (FileNotFoundError, ConnectionRefusedError):
        return None  # daemon not running
    except Exception:
        return []


def spawn_daemon():
    try:
        subprocess.Popen(
            [sys.executable, str(REPO / "recall_daemon.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy(),
        )
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    prompt = (payload.get("prompt") or "").strip()
    cwd = payload.get("cwd") or ""
    if len(prompt) < 6 or prompt.startswith("/"):
        return  # skip trivial prompts and slash commands

    hits = query_daemon(sock_path(), prompt, cwd)
    if hits is None:           # cold: warm it for next time, skip this turn
        spawn_daemon()
        return
    hits = [h for h in hits if h.get("score", 0) >= MIN_SCORE][:MAX_HITS]
    if not hits:
        return

    lines = ["Relevant memories (cc-mem auto-recall) — verify with memory_get if you use them:"]
    for h in hits:
        tier = h.get("tier", "")
        lines.append(f"- [{h['id']}] ({tier}) {h.get('label','')}: {h.get('summary','')}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    }))


if __name__ == "__main__":
    main()

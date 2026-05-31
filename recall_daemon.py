#!/usr/bin/env python3
"""Warm recall daemon for the UserPromptSubmit hook.

The hook fires on every prompt and needs a semantic search — but loading the
embedding model per prompt (~2s) would be unusable. So this long-lived process
holds the model in memory and answers searches over a Unix socket in
milliseconds. It caches one Brain per repo (model is shared module-global), and
exits after an idle period so it doesn't linger forever.

Protocol: newline-delimited JSON. Request {"query","cwd","k"} -> {"hits":[...]}.
Started on demand (detached) by hooks/prompt_recall.py; you never run it by hand.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import Brain
from graph_memory import default_db_path

SOCK = default_db_path().parent / "recall.sock"
IDLE_TIMEOUT = 1800  # seconds with no request -> exit (frees the model)

_brains: dict[str, Brain] = {}


def brain_for(cwd: str) -> Brain:
    from project import find_repo_root
    repo = find_repo_root(Path(cwd)) if cwd else None
    key = str(repo) if repo else "__global__"
    if key not in _brains:
        _brains[key] = Brain(cwd=Path(cwd) if cwd else None)
    else:
        _brains[key].reload_project_if_changed()  # pick up file edits
    return _brains[key]


def handle(req: dict) -> dict:
    b = brain_for(req.get("cwd", ""))
    hits = b.search(req.get("query", ""), int(req.get("k", 5)), scope="auto")
    return {"hits": hits}


def main():
    SOCK.parent.mkdir(parents=True, exist_ok=True)
    # if a daemon is already listening, bail out
    if SOCK.exists():
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(str(SOCK)); probe.close()
            return  # someone's already serving
        except OSError:
            SOCK.unlink(missing_ok=True)  # stale socket

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    srv.listen(8)
    srv.settimeout(IDLE_TIMEOUT)
    # warm the model up front so the first real request is instant
    try:
        brain_for(os.getcwd())
    except Exception:
        pass
    print(f"[cc-mem recall] warm at {SOCK}", file=sys.stderr, flush=True)

    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            break  # idle -> exit
        with conn:
            try:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.decode("utf-8") or "{}")
                resp = handle(req)
            except Exception as exc:
                resp = {"hits": [], "error": str(exc)}
            try:
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except OSError:
                pass
    SOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

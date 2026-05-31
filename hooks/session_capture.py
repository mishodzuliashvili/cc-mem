#!/usr/bin/env python3
"""SessionEnd hook: auto-capture (proposals only).

When a session ends, a small headless Claude pass reads the transcript and
extracts durable, verified learnings worth remembering. These are written as
PROPOSALS to ~/.claude-cc-mem/pending/ — never auto-saved. You approve or
dismiss them in the cc-mem web UI (the SessionStart hook tells you how many are
waiting). So capture stops depending on the main agent remembering to save,
but a human still gates what enters long-term memory.

Optional + best-effort: needs the `claude` CLI; on any error it exits quietly.
Installed (opt-in) by `python3 setup.py hooks --capture`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPT = """You are reviewing a coding session transcript to extract memories worth keeping long-term.

Output ONLY a JSON array (possibly empty). Each item:
  {"label": short title, "summary": one line, "content": the durable fact/decision/gotcha,
   "type": one of fact|preference|decision|howto|reference,
   "scope": "project" if specific to this repo else "global"}

Extract at most 5. Include ONLY things that are durable and reusable: decisions + rationale,
gotchas, conventions, verified facts, or user preferences/corrections ("from now on…").
EXCLUDE transient chatter, one-off task steps, and anything already obvious from the code.
If nothing qualifies, output [].

TRANSCRIPT:
"""


def read_transcript(path: str, limit: int = 16000) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    out = []
    for ln in lines:
        try:
            msg = json.loads(ln)
        except Exception:
            continue
        role = msg.get("role") or msg.get("type") or ""
        content = msg.get("content") or msg.get("text") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if content:
            out.append(f"{role}: {content}")
    text = "\n".join(out)
    return text[-limit:]


def extract(transcript: str) -> list:
    try:
        r = subprocess.run(
            ["claude", "-p", PROMPT + transcript],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return []
    raw = r.stdout.strip()
    # be lenient: pull the first [...] block
    i, j = raw.find("["), raw.rfind("]")
    if i == -1 or j == -1:
        return []
    try:
        items = json.loads(raw[i:j + 1])
        return items if isinstance(items, list) else []
    except Exception:
        return []


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    transcript = read_transcript(payload.get("transcript_path", ""))
    if len(transcript) < 200:  # nothing substantial
        return
    proposals = extract(transcript)
    if not proposals:
        return
    try:
        from graph_memory import default_db_path
        pend = default_db_path().parent / "pending"
        pend.mkdir(parents=True, exist_ok=True)
        sid = payload.get("session_id", "session")
        (pend / f"{sid}.json").write_text(json.dumps({
            "session_id": sid,
            "cwd": payload.get("cwd", ""),
            "proposals": proposals,
        }, indent=2), encoding="utf-8")
    except Exception:
        return


if __name__ == "__main__":
    main()

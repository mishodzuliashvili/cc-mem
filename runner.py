"""Run a verification command for verification-gated memory writes.

A memory can carry the shell command that PROVES it (a test, a build, an API
probe). We run that command; exit 0 means the claim still holds. This is what
keeps the memory honest for externally-checkable facts — and lets a stored fact
be RE-verified later, so the docs don't rot into confident lies.

Trust boundary: the command is run on the user's machine, in the project dir,
with a timeout. It is only ever run when a caller explicitly supplies `verify=`
on insert or calls memory_verify — never automatically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(command: str, cwd: Path | str | None = None, timeout: int = 120) -> dict:
    """Run `command` in a shell. Returns {ok, exit_code, output} (output is the
    tail of combined stdout+stderr)."""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout + proc.stderr).strip()
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "output": out[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None,
                "output": f"timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "exit_code": None, "output": f"failed to run: {exc}"}

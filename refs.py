"""File references with content hashing — staleness detection for memories.

A memory derived from a file (a path mentioned in its content/sources) goes stale
when that file changes, but text can't know that. So a memory can carry structured
`refs`: the files it depends on. We snapshot each file's content hash when the
memory is saved; later `check()` recomputes and reports ok / changed / missing —
so a memory whose source moved under it gets flagged for re-verification instead
of being trusted forever.

Project-memory refs are repo-relative (portable across machines); global-memory
refs are absolute (machine-specific — same trade-off as global ids).
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def resolve(path: str, base: str | Path | None) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute() or not base:
        return p
    return Path(base) / p


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def file_hash(path: Path, lines: str | None = None) -> str | None:
    """Short content hash of a file, or of a 'start-end' line range. None if the
    file is missing."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    if lines:
        a, _, b = lines.partition("-")
        try:
            start = int(a); end = int(b) if b else start
            text = "\n".join(text.splitlines()[start - 1:end])
        except ValueError:
            pass
    return _hash(text)


def normalize(ref) -> dict:
    """Accept a bare path string or {path, lines}."""
    if isinstance(ref, str):
        return {"path": ref, "lines": None}
    return {"path": ref["path"], "lines": ref.get("lines")}


def snapshot(refs, base, now) -> list[dict]:
    """Compute stored refs (path + lines + current hash) at save time."""
    out = []
    for ref in refs or []:
        r = normalize(ref)
        out.append({"path": r["path"], "lines": r["lines"],
                    "hash": file_hash(resolve(r["path"], base), r["lines"]),
                    "checked_at": now})
    return out


def check(refs, base) -> list[dict]:
    """Recompute each ref's hash now and compare to the stored one."""
    res = []
    for r in refs or []:
        cur = file_hash(resolve(r["path"], base), r.get("lines"))
        status = "missing" if cur is None else ("ok" if cur == r.get("hash") else "changed")
        res.append({"path": r["path"], "lines": r.get("lines"), "status": status})
    return res

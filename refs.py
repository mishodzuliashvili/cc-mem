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


def file_meta(path: Path):
    """(mtime, size) or None if the file is gone. A cheap pre-filter: if both are
    unchanged the content is unchanged, so we can skip re-hashing."""
    try:
        st = path.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None


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
    """Compute stored refs (path + lines + content hash + mtime/size) at save time."""
    out = []
    for ref in refs or []:
        r = normalize(ref)
        p = resolve(r["path"], base)
        meta = file_meta(p)
        out.append({"path": r["path"], "lines": r["lines"],
                    "hash": file_hash(p, r["lines"]),
                    "mtime": meta[0] if meta else None,
                    "size": meta[1] if meta else None,
                    "checked_at": now})
    return out


def check(refs, base) -> list[dict]:
    """Re-check each ref. Fast path: if mtime+size are unchanged the content is
    unchanged (status ok, no read). Otherwise re-hash to decide ok vs changed —
    so a touch/checkout that didn't change content won't false-flag. Returns the
    file's current mtime too, for display."""
    res = []
    for r in refs or []:
        p = resolve(r["path"], base)
        meta = file_meta(p)
        if meta is None:
            res.append({"path": r["path"], "lines": r.get("lines"),
                        "status": "missing", "mtime": None})
            continue
        mtime, size = meta
        if r.get("mtime") == mtime and r.get("size") == size and r.get("mtime") is not None:
            status = "ok"  # unchanged metadata -> unchanged content, skip hashing
        else:
            cur = file_hash(p, r.get("lines"))
            status = "ok" if cur == r.get("hash") else "changed"
        res.append({"path": r["path"], "lines": r.get("lines"),
                    "status": status, "mtime": mtime})
    return res

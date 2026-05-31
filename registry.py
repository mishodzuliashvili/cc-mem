"""Registry of projects that use cc-mem.

There's no way to discover which repos have a `.cc-mem/` without scanning the
whole disk, so each project records itself here the first time it gets a project
memory. The web UI reads this to show ALL projects at once (a dashboard over your
whole knowledge base), while the MCP server stays scoped to one project.

Stored at ~/.claude-cc-mem/projects.json as {key: {"root": "<abs path>"}}.
"""

from __future__ import annotations

import json
from pathlib import Path

from graph_memory import default_db_path


def _path() -> Path:
    return default_db_path().parent / "projects.json"


def _read() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def register(key: str, root: str | Path) -> None:
    """Record a project. Idempotent; updates the path if it moved."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _read()
    if data.get(key, {}).get("root") == str(root):
        return
    data[key] = {"root": str(root)}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_projects() -> dict:
    """{key: {"root": path}} — only entries whose .cc-mem still exists on disk."""
    out = {}
    for key, info in _read().items():
        root = Path(info.get("root", ""))
        if (root / ".cc-mem" / "nodes").exists():
            out[key] = info
    return out

"""Project-scoped memory: one mergeable file per node under <repo>/.cc-mem/.

Why files instead of a DB for project memory:
  - committed to git, so the whole team shares it;
  - one file per memory, so two teammates adding memories produce two different
    files -> git merges them additively (synergy, not conflicts);
  - human-readable (JSON frontmatter + markdown body);
  - embeddings are DERIVED, never committed — recomputed into an in-memory index
    on load, so git only ever sees readable text.

Project identity is the git remote (stable across machines/paths), falling back
to the repo directory name. The retrieval engine is a throwaway in-memory
GraphMemory rebuilt from the files at startup — project memory is small, so
re-embedding on launch is cheap, and there's no binary cache to invalidate.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from graph_memory import GraphMemory


# ── Locating the project ──────────────────────────────────────────────────────

def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) to the nearest dir containing .git."""
    p = (start or Path.cwd()).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def project_key(root: Path) -> str:
    """Stable identity for a repo: normalized git origin URL, else dir name.
    Read from .git/config directly so we don't shell out to git."""
    cfg = root / ".git" / "config"
    if cfg.is_file():
        text = cfg.read_text(encoding="utf-8", errors="ignore")
        # crude but dependency-free: find the origin remote's url
        m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(.+)', text, re.S)
        if m:
            url = m.group(1).strip().splitlines()[0].strip()
            return _normalize_remote(url)
    return f"dir:{root.name}"


def _normalize_remote(url: str) -> str:
    """git@github.com:org/repo.git and https://github.com/org/repo(.git) ->
    github.com/org/repo, so the same repo matches across clone styles."""
    url = url.strip()
    url = re.sub(r"^[a-z]+://", "", url)        # strip scheme
    url = re.sub(r"^git@", "", url)             # strip ssh user
    url = url.replace(":", "/", 1) if "@" not in url and url.count(":") == 1 and "/" not in url.split(":")[0] else url.replace(":", "/", 1)
    url = re.sub(r"\.git$", "", url)
    url = url.rstrip("/")
    return url


# ── File (de)serialization: JSON frontmatter + markdown body ──────────────────

_FENCE = "---"


def serialize(meta: dict, content: str) -> str:
    return f"{_FENCE}\n{json.dumps(meta, indent=2, ensure_ascii=False)}\n{_FENCE}\n{content}"


def parse(text: str) -> tuple[dict, str]:
    """Inverse of serialize. Tolerant: missing frontmatter -> ({}, whole text)."""
    if text.startswith(_FENCE):
        end = text.find(f"\n{_FENCE}", len(_FENCE))
        if end != -1:
            head = text[len(_FENCE):end].strip()
            body = text[end + len(_FENCE) + 1:]
            if body.startswith("\n"):
                body = body[1:]
            try:
                return json.loads(head), body
            except json.JSONDecodeError:
                pass
    return {}, text


# ── The project store ─────────────────────────────────────────────────────────

class ProjectMemory:
    """Loads <root>/.cc-mem/nodes/*.md into an in-memory graph and keeps the
    files as the source of truth. Node ids are stable uuids (the file stem)."""

    def __init__(self, repo_root: Path, *, clock=time.time):
        self.repo_root = repo_root
        self.key = project_key(repo_root)
        self.dir = repo_root / ".cc-mem"
        self.nodes_dir = self.dir / "nodes"
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.store = GraphMemory(":memory:", clock=clock)  # throwaway engine
        self.uid2int: dict[str, int] = {}
        self.int2uid: dict[int, str] = {}
        self.meta: dict[str, dict] = {}  # uid -> frontmatter
        self._load()

    # -- loading --
    def _path(self, uid: str) -> Path:
        return self.nodes_dir / f"{uid}.md"

    def signature(self) -> str:
        """Cheap fingerprint of the node files on disk (count + newest mtime).
        Changes when a memory is added/edited/removed — including by a teammate's
        `git pull` or by the MCP server in a Claude session."""
        files = list(self.nodes_dir.glob("*.md"))
        mtime = max((f.stat().st_mtime for f in files), default=0.0)
        return f"{len(files)}-{mtime:.3f}"

    def _load(self) -> None:
        files = sorted(self.nodes_dir.glob("*.md"))
        parsed = []
        for f in files:
            meta, body = parse(f.read_text(encoding="utf-8"))
            uid = meta.get("id") or f.stem
            meta["id"] = uid
            self.meta[uid] = meta
            parsed.append((uid, meta, body))
        # pass 1: create nodes so every uid maps to an int before wiring links
        for uid, meta, body in parsed:
            iid = self.store.insert(
                content=body, summary=meta.get("summary", ""),
                label=meta.get("label", ""), importance=float(meta.get("importance", 1.0)),
                scope="project", project=self.key, type=meta.get("type", "fact"),
                sources=meta.get("sources", ""), confidence=float(meta.get("confidence", 1.0)),
                verified_by=meta.get("verified_by", ""),
                refs=__import__("json").dumps(meta.get("refs", [])) if meta.get("refs") else "")
            self.uid2int[uid] = iid
            self.int2uid[iid] = uid
        if parsed:  # this repo actually has project memory — make it discoverable
            try:
                from registry import register
                register(self.key, self.repo_root)
            except Exception:
                pass
        # pass 2: wire links once (canonical-dedupe across both endpoints' files)
        seen: dict[tuple, tuple] = {}
        for uid, meta, _ in parsed:
            for link in meta.get("links", []):
                other, kind, weight = _norm_link(link)
                if other not in self.uid2int:
                    continue
                a, b = sorted((uid, other))
                key = (a, b, kind)
                if key not in seen or weight > seen[key][2]:
                    seen[key] = (a, b, weight)
        for (a, b, kind), (_, _, w) in {(k[0], k[1], k[2]): v for k, v in seen.items()}.items():
            self.store.link(self.uid2int[a], self.uid2int[b], kind, w)

    # -- writes (file is source of truth; in-memory store mirrors it) --
    def insert(self, content, summary="", label="", importance=1.0,
               links=None, sources="", confidence=1.0, type="fact",
               verified_by="", refs=None) -> str:
        uid = uuid.uuid4().hex[:12]
        now = self._clock()
        norm_links = [list(_norm_link(l)) for l in (links or [])
                      if _norm_link(l)[0] in self.uid2int]
        meta = {"id": uid, "label": label, "summary": summary, "scope": "project",
                "type": type, "importance": float(importance),
                "confidence": float(confidence), "sources": sources,
                "verified_by": verified_by,
                "last_verified": now if verified_by else None,
                "refs": refs or [],
                "links": norm_links, "created_at": now}
        self.meta[uid] = meta
        self._path(uid).write_text(serialize(meta, content), encoding="utf-8")
        try:
            from registry import register
            register(self.key, self.repo_root)
        except Exception:
            pass
        iid = self.store.insert(content=content, summary=summary, label=label,
                                importance=importance, scope="project", type=type,
                                project=self.key, sources=sources, confidence=confidence,
                                verified_by=verified_by,
                                refs=__import__("json").dumps(refs) if refs else "")
        self.uid2int[uid] = iid
        self.int2uid[iid] = uid
        for other, kind, weight in norm_links:
            self.link(uid, other, kind, weight)
        return uid

    def update(self, uid: str, **fields) -> dict | None:
        if uid not in self.meta:
            return None
        meta = self.meta[uid]
        _, body = parse(self._path(uid).read_text(encoding="utf-8"))
        content = fields.get("content")
        if content is None:
            content = body
        for k in ("label", "summary", "importance", "confidence", "sources", "type",
                  "verified_by", "last_verified", "refs"):
            if fields.get(k) is not None:
                meta[k] = fields[k]
        self._path(uid).write_text(serialize(meta, content), encoding="utf-8")
        self.store.update_node(self.uid2int[uid], content=content,
                               summary=meta.get("summary"), label=meta.get("label"),
                               importance=meta.get("importance"), type=meta.get("type"),
                               confidence=meta.get("confidence"), sources=meta.get("sources"))
        return self.get(uid)

    def delete(self, uid: str) -> bool:
        if uid not in self.meta:
            return False
        self._path(uid).unlink(missing_ok=True)
        self.store.delete_node(self.uid2int[uid])
        # drop this uid from other files' link lists so they stay consistent
        for other_uid, m in list(self.meta.items()):
            if other_uid == uid:
                continue
            links = m.get("links", [])
            kept = [l for l in links if _norm_link(l)[0] != uid]
            if len(kept) != len(links):
                m["links"] = kept
                _, body = parse(self._path(other_uid).read_text(encoding="utf-8"))
                self._path(other_uid).write_text(serialize(m, body), encoding="utf-8")
        del self.meta[uid]
        iid = self.uid2int.pop(uid)
        self.int2uid.pop(iid, None)
        return True

    def link(self, a_uid: str, b_uid: str, kind="related", weight=1.0) -> dict:
        if a_uid not in self.uid2int or b_uid not in self.uid2int:
            return {"ok": False, "error": "unknown project node"}
        self.store.link(self.uid2int[a_uid], self.uid2int[b_uid], kind, weight)
        # record the edge in BOTH files so each is self-describing + merge-safe
        for src, dst in ((a_uid, b_uid), (b_uid, a_uid)):
            m = self.meta[src]
            links = m.setdefault("links", [])
            if not any(_norm_link(l)[0] == dst and _norm_link(l)[1] == kind for l in links):
                links.append([dst, kind, weight])
                _, body = parse(self._path(src).read_text(encoding="utf-8"))
                self._path(src).write_text(serialize(m, body), encoding="utf-8")
        return {"ok": True}

    def unlink(self, a_uid: str, b_uid: str, kind: str | None = None) -> int:
        if a_uid not in self.uid2int or b_uid not in self.uid2int:
            return 0
        removed = self.store.unlink(self.uid2int[a_uid], self.uid2int[b_uid], kind)
        for src, dst in ((a_uid, b_uid), (b_uid, a_uid)):
            m = self.meta[src]
            links = m.get("links", [])
            kept = [l for l in links
                    if not (_norm_link(l)[0] == dst and (kind is None or _norm_link(l)[1] == kind))]
            if len(kept) != len(links):
                m["links"] = kept
                _, body = parse(self._path(src).read_text(encoding="utf-8"))
                self._path(src).write_text(serialize(m, body), encoding="utf-8")
        return removed

    # -- reads --
    def get(self, uid: str) -> dict | None:
        if uid not in self.uid2int:
            return None
        node = self.store.get(self.uid2int[uid])
        if not node:
            return None
        node["id"] = uid
        # verified_by / last_verified live in the file (source of truth), not the
        # throwaway in-memory store.
        node["verified_by"] = self.meta[uid].get("verified_by", "")
        node["last_verified"] = self.meta[uid].get("last_verified")
        node["refs"] = self.meta[uid].get("refs", [])
        node["neighbors"] = self._neighbors(uid)
        return node

    def _neighbors(self, uid: str) -> list[dict]:
        out = []
        for nb in self.store.expand(self.uid2int[uid])["neighbors"]:
            ouid = self.int2uid.get(nb["id"])
            if ouid:
                out.append({**nb, "id": ouid})
        return out

    def search(self, query: str, k: int = 10) -> list[dict]:
        hits = self.store.search(query, k)
        for h in hits:
            h["id"] = self.int2uid.get(h["id"], h["id"])
        return hits

    def list_nodes(self) -> list[dict]:
        rows = []
        for uid, iid in self.uid2int.items():
            r = self.store.db.execute(
                "SELECT label,summary,scope,type,importance,access_count,confidence,created_at "
                "FROM nodes WHERE id=?", (iid,)).fetchone()
            rows.append({"id": uid, "project": self.key, **dict(r)})
        return rows

    def graph(self) -> dict:
        nodes = [{"id": uid, **{k: self.meta[uid].get(k) for k in ("label", "summary")},
                  "scope": "project", "type": self.meta[uid].get("type", "fact"),
                  "importance": self.meta[uid].get("importance", 1.0),
                  "access_count": 0} for uid in self.uid2int]
        seen = {}
        for r in self.store.db.execute("SELECT src,dst,kind,weight FROM edges"):
            su, du = self.int2uid.get(r["src"]), self.int2uid.get(r["dst"])
            if not (su and du):
                continue
            a, b = sorted((su, du))
            key = (a, b, r["kind"])
            if key not in seen or r["weight"] > seen[key]["weight"]:
                seen[key] = {"src": a, "dst": b, "kind": r["kind"], "weight": r["weight"]}
        return {"nodes": nodes, "edges": list(seen.values())}


def _norm_link(link) -> tuple[str, str, float]:
    if isinstance(link, (list, tuple)):
        other = str(link[0])
        kind = str(link[1]) if len(link) > 1 and link[1] else "related"
        weight = float(link[2]) if len(link) > 2 and link[2] is not None else 1.0
        return other, kind, weight
    return str(link), "related", 1.0

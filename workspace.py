"""Workspace: a dashboard view over the WHOLE knowledge base — the global brain
plus EVERY registered project's memory at once.

This is the web UI's backend (not the MCP server's — Claude stays scoped to its
own project for isolation; the human inspecting the UI sees everything). Node ids
stay namespaced `g:<int>` / `p:<uuid>`; project uuids are globally unique, so a
single uuid->project map routes get/update/delete to the right project.
"""

from __future__ import annotations

from pathlib import Path

from graph_memory import GraphMemory, default_db_path
from project import ProjectMemory
import registry


def _g(i):
    return f"g:{i}"


def _p(u):
    return f"p:{u}"


def _parse(node_id: str):
    s = str(node_id)
    if s.startswith("g:"):
        return "g", int(s[2:])
    if s.startswith("p:"):
        return "p", s[2:]
    return "g", int(s)


class Workspace:
    def __init__(self, global_db=None):
        self.global_store = GraphMemory(global_db or default_db_path())
        self.projects: dict[str, ProjectMemory] = {}
        self._uuid2proj: dict[str, ProjectMemory] = {}
        self._sig = None
        self._load_projects()

    def _load_projects(self):
        self.projects, self._uuid2proj = {}, {}
        for key, info in registry.list_projects().items():
            try:
                pm = ProjectMemory(Path(info["root"]))
            except Exception:
                continue
            self.projects[pm.key] = pm
            for uid in pm.uid2int:
                self._uuid2proj[uid] = pm
        self._sig = self._signature()

    def _signature(self) -> str:
        """Fingerprint of all projects on disk: which projects exist + each one's
        file count and newest mtime. Changes on add/edit/remove or a new project."""
        live = []
        for key, info in sorted(registry.list_projects().items()):
            d = Path(info["root"]) / ".cc-mem" / "nodes"
            files = list(d.glob("*.md")) if d.exists() else []
            mt = max((f.stat().st_mtime for f in files), default=0.0)
            live.append(f"{key}:{len(files)}:{mt:.3f}")
        return "|".join(live)

    def signature(self) -> str:
        return self._sig or ""

    def reload_project_if_changed(self) -> bool:
        """Re-scan the registry (new projects) and reload any whose files changed.
        Cheap when nothing changed."""
        if self._signature() != self._sig:
            self._load_projects()
            return True
        return False

    # ── reads ────────────────────────────────────────────────────────────────
    def context(self):
        return {
            "global_db": str(self.global_store.path),
            "projects": [{"key": k, "dir": str(pm.dir), "nodes": len(pm.uid2int)}
                         for k, pm in self.projects.items()],
        }

    def stats(self):
        g = self.global_store.stats()
        pnodes = sum(len(pm.uid2int) for pm in self.projects.values())
        return {"nodes": g["nodes"] + pnodes, "edges": g["edges"],
                "by_scope": {"global": g["nodes"], "project": pnodes},
                "projects": len(self.projects)}

    def list_nodes(self, q="", scope="", sort="created_at", order="desc"):
        rows = []
        if scope in ("", "global"):
            for r in self.global_store.db.execute(
                    "SELECT id,label,summary,scope,type,importance,access_count,"
                    "confidence,created_at FROM nodes"):
                rows.append({**dict(r), "id": _g(r["id"]), "tier": "global", "project": ""})
        if scope in ("", "project"):
            for key, pm in self.projects.items():
                for r in pm.list_nodes():
                    rows.append({**r, "id": _p(r["id"]), "tier": "project",
                                 "project": key, "last_accessed": r.get("created_at")})
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r.get("label", "") or "").lower()
                    or ql in (r.get("summary", "") or "").lower()]
        rows.sort(key=lambda r: (r.get(sort) if r.get(sort) is not None else r.get("created_at", 0)) or 0,
                  reverse=(order != "asc"))
        return {"nodes": rows, "shown": len(rows), "total": len(rows)}

    def get(self, node_id):
        tier, raw = _parse(node_id)
        if tier == "g":
            node = self.global_store.get(raw)
            if not node:
                return None
            node["id"] = _g(raw)
            node["tier"] = "global"
            node["neighbors"] = [{**nb, "id": _g(nb["id"])}
                                 for nb in self.global_store.expand(raw)["neighbors"]]
            return node
        pm = self._uuid2proj.get(raw)
        if not pm:
            return None
        node = pm.get(raw)
        if not node:
            return None
        node["id"] = _p(raw)
        node["project"] = pm.key
        node["tier"] = "project"
        node["neighbors"] = [{**nb, "id": _p(nb["id"])} for nb in node.get("neighbors", [])]
        return node

    def graph(self):
        nodes, edges, seen = [], [], {}
        for r in self.global_store.db.execute(
                "SELECT id,label,summary,scope,type,importance,access_count FROM nodes"):
            nodes.append({**dict(r), "id": _g(r["id"]), "tier": "global"})
        for r in self.global_store.db.execute("SELECT src,dst,kind,weight FROM edges"):
            a, b = sorted((r["src"], r["dst"]))
            k = (a, b, r["kind"])
            if k not in seen or r["weight"] > seen[k]["weight"]:
                seen[k] = {"src": _g(a), "dst": _g(b), "kind": r["kind"], "weight": r["weight"]}
        edges.extend(seen.values())
        for key, pm in self.projects.items():
            pg = pm.graph()
            for n in pg["nodes"]:
                nodes.append({**n, "id": _p(n["id"]), "tier": "project", "project": key})
            for e in pg["edges"]:
                edges.append({**e, "src": _p(e["src"]), "dst": _p(e["dst"])})
        return {"nodes": nodes, "edges": edges}

    def search(self, query, k=20, scope="auto"):
        hits = []
        if scope in ("auto", "all", "global", ""):
            for h in self.global_store.search(query, k):
                hits.append({**h, "id": _g(h["id"]), "tier": "global"})
        if scope in ("auto", "all", "project", ""):
            for key, pm in self.projects.items():
                for h in pm.search(query, k):
                    hits.append({**h, "id": _p(h["id"]), "tier": "project", "project": key})
        hits.sort(key=lambda h: h.get("score", 0), reverse=True)
        return hits[:k]

    # ── writes ──────────────────────────────────────────────────────────────
    def insert(self, content, summary="", label="", importance=1.0, type="fact",
               scope="global", project="", sources="", confidence=1.0):
        if scope == "project":
            pm = self.projects.get(project)
            if not pm and len(self.projects) == 1:
                pm = next(iter(self.projects.values()))
            if not pm:
                return {"ok": False, "error": "choose a project for project-scoped memory"}
            uid = pm.insert(content, summary, label, importance, None, sources,
                            confidence, type=type)
            self._uuid2proj[uid] = pm
            return {"ok": True, "id": _p(uid), "scope": "project", "project": pm.key}
        nid = self.global_store.insert(content, summary, label, importance,
                                       scope="global", type=type, sources=sources,
                                       confidence=confidence)
        return {"ok": True, "id": _g(nid), "scope": "global"}

    def update(self, node_id, **fields):
        tier, raw = _parse(node_id)
        if tier == "g":
            n = self.global_store.update_node(raw, **fields)
            if n:
                n["id"] = _g(raw)
            return n
        pm = self._uuid2proj.get(raw)
        if not pm:
            return None
        n = pm.update(raw, **fields)
        if n:
            n["id"] = _p(raw)
        return n

    def delete(self, node_id):
        tier, raw = _parse(node_id)
        if tier == "g":
            return self.global_store.delete_node(raw)
        pm = self._uuid2proj.get(raw)
        return bool(pm and pm.delete(raw))

    def link(self, a, b, kind="related", weight=1.0):
        ta, ra = _parse(a)
        tb, rb = _parse(b)
        if ta != tb:
            return {"ok": False, "error": "cannot link across tiers"}
        if ta == "g":
            return self.global_store.link(ra, rb, kind, weight)
        pa, pb = self._uuid2proj.get(ra), self._uuid2proj.get(rb)
        if not pa or pa is not pb:
            return {"ok": False, "error": "links must be within one project"}
        return pa.link(ra, rb, kind, weight)

    def unlink(self, a, b, kind=None):
        ta, ra = _parse(a)
        tb, rb = _parse(b)
        if ta != tb:
            return {"ok": False, "error": "cannot unlink across tiers"}
        if ta == "g":
            return {"ok": True, "removed": self.global_store.unlink(ra, rb, kind)}
        pa = self._uuid2proj.get(ra)
        return {"ok": True, "removed": pa.unlink(ra, rb, kind) if pa else 0}

"""The Brain: one facade over two tiers of memory.

  - GLOBAL  — your private, cross-project knowledge in ~/.claude-cc-mem (SQLite).
  - PROJECT — the current repo's shared, git-committed memory (files via
              ProjectMemory), isolated to that repo.

Node ids are namespaced strings so the tiers never collide:
    g:<int>     a global node
    p:<uuid>    a project node

Default recall = global + the current project (never other projects), which is
the isolation the hybrid model promises. Both the MCP server and the web API go
through this class so they behave identically.
"""

from __future__ import annotations

import os
from pathlib import Path

from graph_memory import GraphMemory, default_db_path
from project import ProjectMemory, find_repo_root


def _enc_g(i) -> str:
    return f"g:{i}"


def _enc_p(u) -> str:
    return f"p:{u}"


def parse_id(node_id: str):
    """'g:5' -> ('g', 5) ; 'p:ab12' -> ('p', 'ab12'). Bare ints assumed global."""
    s = str(node_id)
    if s.startswith("g:"):
        return "g", int(s[2:])
    if s.startswith("p:"):
        return "p", s[2:]
    return "g", int(s)  # tolerate a bare global int


class Brain:
    def __init__(self, cwd: Path | None = None, global_db: str | Path | None = None):
        self.global_store = GraphMemory(global_db or default_db_path())
        self._repo = find_repo_root(cwd)
        self.project = ProjectMemory(self._repo) if self._repo else None

    def close(self):
        self.global_store.close()

    def reload_project_if_changed(self) -> bool:
        """Rebuild the project store from files if they changed on disk (a
        teammate pulled, or a Claude session wrote a project memory). Cheap
        no-op when nothing changed. Returns True if it reloaded."""
        if not self._repo or not self.project:
            return False
        sig = self.project.signature()
        if sig != getattr(self, "_proj_sig", None):
            self.project = ProjectMemory(self._repo)
            self._proj_sig = self.project.signature()
            return True
        return False

    def project_signature(self) -> str:
        return self.project.signature() if self.project else ""

    # ── context ───────────────────────────────────────────────────────────────
    def context(self) -> dict:
        return {
            "global_db": str(self.global_store.path),
            "project_active": self.project is not None,
            "project_key": self.project.key if self.project else None,
            "project_dir": str(self.project.dir) if self.project else None,
        }

    # ── writes ──────────────────────────────────────────────────────────────
    # cosine; above this an insert likely duplicates an existing memory. Advisory:
    # the agent gets the candidates and can reconcile (update) or force a new insert.
    # Tuned for all-MiniLM-L6-v2 (near-dups ~0.83, distinct ~0.1). Override via env.
    DUP_THRESHOLD = float(os.environ.get("CC_MEM_DUP_THRESHOLD", "0.78"))

    def _duplicates(self, text, scope):
        """Near-duplicate candidates in the target tier (so insert can steer
        toward update instead of piling up copies)."""
        cands = self.search(text, k=3, scope="project" if scope == "project" else "global")
        return [c for c in cands
                if c.get("similarity", c.get("score", 0)) >= self.DUP_THRESHOLD]

    def insert(self, content, summary="", label="", importance=1.0, links=None,
               scope="global", sources="", confidence=1.0, type="fact",
               force=False, verify=None, refs=None) -> dict:
        if scope == "project" and not self.project:
            return {"ok": False, "error": "no git repo here — project scope "
                    "unavailable. Use scope='global', or run inside a repo."}
        if not force:
            probe = " ".join(p for p in (label, summary, content) if p)
            dups = self._duplicates(probe, scope)
            if dups:
                return {"ok": False, "reason": "possible_duplicate",
                        "duplicate_candidates": dups,
                        "hint": "A very similar memory already exists. Prefer "
                        "memory_update on one of these (reconcile in place); or "
                        "re-insert with force=true if this is genuinely new/distinct."}
        # Verification gate: if a check command is given, it must pass to persist.
        verified_by = ""
        if verify:
            from runner import run
            res = run(verify, cwd=self._repo or Path.cwd())
            if not res["ok"]:
                return {"ok": False, "reason": "verification_failed", "command": verify,
                        "exit_code": res["exit_code"], "output": res["output"],
                        "hint": "The check command failed, so nothing was saved. Fix "
                        "the claim or the command, then retry."}
            verified_by = verify
        import json as _json
        import refs as _refs
        now = self.global_store._clock()
        snap = _refs.snapshot(refs, self._repo if scope == "project" else None, now)
        if scope == "project":
            plinks = [self._strip(l) for l in (links or [])]
            uid = self.project.insert(content, summary, label, importance,
                                      plinks, sources, confidence, type=type,
                                      verified_by=verified_by, refs=snap)
            return {"ok": True, "id": _enc_p(uid), "scope": "project",
                    "project": self.project.key, "verified": bool(verified_by)}
        glinks = [self._strip_global(l) for l in (links or [])]
        nid = self.global_store.insert(content, summary, label, importance,
                                       glinks, scope="global", sources=sources,
                                       confidence=confidence, type=type,
                                       verified_by=verified_by,
                                       refs=_json.dumps(snap) if snap else "")
        return {"ok": True, "id": _enc_g(nid), "scope": "global",
                "verified": bool(verified_by)}

    def verify(self, node_id) -> dict:
        """Re-check a memory's freshness: re-run its verified_by command (if any)
        AND re-hash its file refs (if any). On all-good, refresh last_verified; on
        a failed command or a changed/missing source file, flag it stale and drop
        confidence. Keeps facts honest instead of letting them rot."""
        import refs as _refs
        node = self.get(node_id)
        if not node:
            return {"ok": False, "error": "not found"}
        tier, _ = parse_id(node_id)
        base = (self.project.repo_root if tier == "p" and self.project else None)

        cmd = node.get("verified_by") or ""
        cmd_res = None
        if cmd:
            from runner import run
            cmd_res = run(cmd, cwd=base or Path.cwd())
        ref_status = _refs.check(node.get("refs") or [], base)

        if not cmd and not ref_status:
            return {"ok": False, "error": "nothing to verify (no command or file refs)"}

        stale = (cmd_res is not None and not cmd_res["ok"]) \
            or any(r["status"] != "ok" for r in ref_status)
        now = self.global_store._clock()
        if not stale:
            self.update(node_id, last_verified=now)
        else:
            note = " [stale: " + ("cmd-failed " if cmd_res and not cmd_res["ok"] else "")
            note += ",".join(f"{r['path']}:{r['status']}" for r in ref_status if r["status"] != "ok")
            self.update(node_id, confidence=max(0.0, (node.get("confidence") or 1.0) * 0.5),
                        sources=(node.get("sources") or "") + note + "]")
        return {"ok": True, "stale": stale, "command": cmd or None,
                "command_ok": (cmd_res["ok"] if cmd_res else None),
                "refs": ref_status}

    @staticmethod
    def _strip(link):
        """For a project link, the target id may be 'p:uid' or bare uid."""
        if isinstance(link, (list, tuple)):
            t, *rest = link
            _, raw = parse_id(t)
            return [raw, *rest]
        _, raw = parse_id(link)
        return raw

    @staticmethod
    def _strip_global(link):
        if isinstance(link, (list, tuple)):
            t, *rest = link
            _, raw = parse_id(t)
            return [raw, *rest]
        _, raw = parse_id(link)
        return raw

    def update(self, node_id, **fields):
        tier, raw = parse_id(node_id)
        if tier == "p":
            if not self.project:
                return None
            node = self.project.update(raw, **fields)
            return self._wrap(node, "p") if node else None
        node = self.global_store.update_node(raw, **fields)
        return self._wrap(node, "g") if node else None

    def delete(self, node_id) -> bool:
        tier, raw = parse_id(node_id)
        if tier == "p":
            return bool(self.project and self.project.delete(raw))
        return self.global_store.delete_node(raw)

    def link(self, a, b, kind="related", weight=1.0):
        ta, ra = parse_id(a)
        tb, rb = parse_id(b)
        if ta != tb:
            return {"ok": False, "error": "cannot link across global/project tiers"}
        if ta == "p":
            return self.project.link(ra, rb, kind, weight) if self.project else {"ok": False}
        return self.global_store.link(ra, rb, kind, weight)

    def unlink(self, a, b, kind=None):
        ta, ra = parse_id(a)
        tb, rb = parse_id(b)
        if ta != tb:
            return {"ok": False, "error": "cannot unlink across tiers"}
        if ta == "p":
            removed = self.project.unlink(ra, rb, kind) if self.project else 0
        else:
            removed = self.global_store.unlink(ra, rb, kind)
        return {"ok": True, "removed": removed}

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, node_id):
        import refs as _refs
        tier, raw = parse_id(node_id)
        if tier == "p":
            node = self.project.get(raw) if self.project else None
            if not node:
                return None
            node = self._wrap(node, "p")
            node["refs"] = _refs.enrich(node.get("refs") or [], self.project.repo_root)
            node["neighbors"] = [{**nb, "id": _enc_p(nb["id"])}
                                 for nb in node.get("neighbors", [])]
            return node
        node = self.global_store.get(raw)
        if not node:
            return None
        node = self._wrap(node, "g")
        node["refs"] = _refs.enrich(node.get("refs") or [], None)
        node["neighbors"] = [
            {**nb, "id": _enc_g(nb["id"])}
            for nb in self.global_store.expand(raw)["neighbors"]
        ]
        return node

    def get_many(self, ids):
        """Fetch several full nodes in one call. Returns them in request order,
        skipping any that don't exist. Lets a caller (or a retrieval subagent)
        pull a whole relevant cluster at once instead of one round-trip each."""
        out = []
        for nid in ids:
            node = self.get(nid)
            if node:
                out.append(node)
        return out

    def suggest_links(self, node_id, k=5):
        """Find nodes most similar to this one that AREN'T linked to it yet —
        candidates worth connecting, so the graph gets denser/more useful over
        time. Same tier only (links don't cross tiers). Returns compact briefs."""
        node = self.get(node_id)
        if not node:
            return []
        text = " ".join(p for p in (node.get("label"), node.get("summary"),
                                     node.get("content")) if p)
        tier, _ = parse_id(node_id)
        scope = "project" if tier == "p" else "global"
        existing = {nb["id"] for nb in node.get("neighbors", [])} | {node_id}
        hits = self.search(text, k + len(existing) + 3, scope=scope)
        return [h for h in hits
                if h["id"] not in existing and h.get("similarity", 0) >= 0.25][:k]

    def _ref_base(self, node_id, ref=None):
        tier, _ = parse_id(node_id)
        if tier == "p" and self.project:
            return self.project.repo_root
        return None

    def _set_refs(self, node_id, new_refs):
        import json as _json
        tier, raw = parse_id(node_id)
        if tier == "p" and self.project:
            self.project.update(raw, refs=new_refs)
        else:
            self.global_store.update_node(raw, refs=_json.dumps(new_refs))

    def relocate(self, node_id, apply=True) -> dict:
        """For any MISSING ref (file renamed/moved), hunt the repo for a file whose
        content hash matches — that's the same file at a new path. With apply=True,
        re-link unambiguous matches in place (and re-snapshot). Returns what it found
        / did so a 'missing' source can be recovered instead of rotting."""
        import refs as _refs
        node = self.get(node_id)
        if not node:
            return {"ok": False, "error": "not found"}
        base = self._ref_base(node_id)
        now = self.global_store._clock()
        result, new_refs, relinked = [], [], False
        for r in node.get("refs") or []:
            if r.get("exists"):
                new_refs.append(_refs.strip(r)); continue
            root = base or Path(r.get("abspath", r["path"])).parent
            matches = _refs.find_by_hash(r.get("hash"), r.get("size"), root, r.get("lines"))
            rel = [os.path.relpath(m, base) if base else m for m in matches]
            if apply and len(rel) == 1:
                snap = _refs.snapshot([{"path": rel[0], "lines": r.get("lines")}], base, now)[0]
                new_refs.append(snap); relinked = True
                result.append({"path": r["path"], "status": "relinked", "new_path": rel[0]})
            else:
                new_refs.append(_refs.strip(r))
                result.append({"path": r["path"],
                               "status": "candidates" if rel else "not_found",
                               "candidates": rel})
        if apply and relinked:
            self._set_refs(node_id, new_refs)
        return {"ok": True, "relinked": relinked, "refs": result}

    def recall(self, query, k=6, full=3, scope="auto"):
        """One-shot 'gather the relevant neighborhood': run the cold-start search,
        then return compact briefs for the top `k` AND the full content of the top
        `full` — so the caller gets multiple related nodes together, budget-bounded,
        without a brief->get round-trip per node."""
        hits = self.search(query, k, scope=scope)
        full_nodes = self.get_many([h["id"] for h in hits[:full]])
        return {"briefs": hits, "full": full_nodes}

    def search(self, query, k=10, scope="auto"):
        """scope: 'auto' = global + current project (default), 'global', 'project',
        or 'all' (same as auto here — there's only one project loaded)."""
        hits = []
        if scope in ("auto", "all", "global"):
            for h in self.global_store.search(query, k):
                hits.append({**h, "id": _enc_g(h["id"]), "tier": "global"})
        if scope in ("auto", "all", "project") and self.project:
            for h in self.project.search(query, k):
                hits.append({**h, "id": _enc_p(h["id"]), "tier": "project"})
        hits.sort(key=lambda h: h.get("score", 0), reverse=True)
        return hits[:k]

    def search_neighbors(self, anchor_id, query, k=5, hops=3):
        tier, raw = parse_id(anchor_id)
        if tier == "p":
            if not self.project:
                return []
            hits = self.project.store.search_neighbors(
                self.project.uid2int.get(raw, -1), query, k, hops)
            return [{**h, "id": _enc_p(self.project.int2uid.get(h["id"], h["id"])),
                     "tier": "project"} for h in hits]
        return [{**h, "id": _enc_g(h["id"]), "tier": "global"}
                for h in self.global_store.search_neighbors(raw, query, k, hops)]

    def expand(self, node_id):
        tier, raw = parse_id(node_id)
        if tier == "p":
            if not self.project:
                return {"id": node_id, "exists": False, "neighbors": []}
            nbs = (self.project._neighbors(raw)
                   if raw in self.project.uid2int else [])
            return {"id": node_id, "exists": raw in self.project.uid2int,
                    "neighbors": [{**nb, "id": _enc_p(nb["id"])} for nb in nbs]}
        exp = self.global_store.expand(raw)
        exp["id"] = node_id
        exp["neighbors"] = [{**nb, "id": _enc_g(nb["id"])} for nb in exp["neighbors"]]
        return exp

    # ── for the web app ───────────────────────────────────────────────────────
    def list_nodes(self, q="", scope="", sort="id", order="desc"):
        rows = []
        if scope in ("", "global"):
            for r in self.global_store.db.execute(
                    "SELECT id,label,summary,scope,type,project,importance,access_count,"
                    "confidence,created_at,last_accessed FROM nodes"):
                rows.append({**dict(r), "id": _enc_g(r["id"]), "tier": "global"})
        if scope in ("", "project") and self.project:
            for r in self.project.list_nodes():
                rows.append({**r, "id": _enc_p(r["id"]), "tier": "project",
                             "last_accessed": r.get("created_at")})
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r.get("label", "") or "").lower()
                    or ql in (r.get("summary", "") or "").lower()]
        rev = order != "asc"
        rows.sort(key=lambda r: (r.get(sort) if sort in r else r.get("created_at", 0)) or 0,
                  reverse=rev)
        return {"nodes": rows, "shown": len(rows), "total": len(rows)}

    def graph(self):
        nodes, edges = [], []
        for r in self.global_store.db.execute(
                "SELECT id,label,summary,scope,type,importance,access_count FROM nodes"):
            nodes.append({**dict(r), "id": _enc_g(r["id"]), "tier": "global"})
        seen = {}
        for r in self.global_store.db.execute("SELECT src,dst,kind,weight FROM edges"):
            a, b = sorted((r["src"], r["dst"]))
            key = (a, b, r["kind"])
            if key not in seen or r["weight"] > seen[key]["weight"]:
                seen[key] = {"src": _enc_g(a), "dst": _enc_g(b),
                             "kind": r["kind"], "weight": r["weight"]}
        edges.extend(seen.values())
        if self.project:
            pg = self.project.graph()
            for n in pg["nodes"]:
                nodes.append({**n, "id": _enc_p(n["id"]), "tier": "project"})
            for e in pg["edges"]:
                edges.append({**e, "src": _enc_p(e["src"]), "dst": _enc_p(e["dst"])})
        return {"nodes": nodes, "edges": edges}

    def stats(self):
        g = self.global_store.stats()
        out = {"global_nodes": g["nodes"], "global_edges": g["edges"],
               "project_active": self.project is not None,
               "project_key": self.project.key if self.project else None,
               "project_nodes": len(self.project.uid2int) if self.project else 0}
        out["nodes"] = out["global_nodes"] + out["project_nodes"]
        out["edges"] = g["edges"]
        out["by_scope"] = {"global": out["global_nodes"], "project": out["project_nodes"]}
        return out

    @staticmethod
    def _wrap(node, tier):
        if node and "id" in node:
            node = {**node, "id": (_enc_p if tier == "p" else _enc_g)(node["id"])}
        return node

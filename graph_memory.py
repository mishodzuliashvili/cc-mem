"""Self-learning graph memory: nodes + typed/weighted edges + vectors in one
SQLite store.

Design notes that matter:

* One file holds everything (nodes, edges, embeddings). A single `insert`
  writes the node, its vector, and its links in one transaction, so the graph
  and the vector index can never drift out of sync.
* WAL mode + a busy timeout make the file safe to share across many concurrent
  Claude Code sessions — that shared file IS the cross-session memory.
* Retrieval is two-mode: a global cold-start scan to find an entry node, and a
  cheap neighbor-scoped walk (spreading activation) that only ever looks at a
  small local subgraph. The expensive filtering happens here, under the hood;
  callers get back compact briefs, never the whole graph.

See index.md for the full vision; this module implements the "built today"
slice plus spreading activation and the §3.5 scoring blend.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import time
from pathlib import Path

from embedder import cosine, embed

# ── Scoring weights (§3.5). Tunable; documented in index.md §3.5. ────────────
ALPHA = 1.0   # similarity to query
BETA = 0.6    # edge strength / activation from the anchor
GAMMA = 0.15  # node importance
DELTA = 0.10  # recency
EPSILON = 0.08  # hop-distance penalty

_HALF_LIFE_DAYS = 30.0  # recency decay half-life


def default_db_path() -> Path:
    """Where the shared brain lives by default. Override with $CC_MEM_DB.

    Default is the user home dir (not any project), because the whole point is
    one memory shared by every session on the machine."""
    env = os.environ.get("CC_MEM_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude-cc-mem" / "memory.db"


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _recency(last_accessed: float, now: float) -> float:
    """1.0 for just-now, decaying with a 30-day half-life."""
    age_days = max(0.0, (now - last_accessed) / 86400.0)
    return 0.5 ** (age_days / _HALF_LIFE_DAYS)


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    importance    REAL NOT NULL DEFAULT 1.0,
    type          TEXT NOT NULL DEFAULT 'fact',
    project       TEXT NOT NULL DEFAULT '',
    scope         TEXT NOT NULL DEFAULT 'global',
    created_at    REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count  INTEGER NOT NULL DEFAULT 0,
    sources       TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 1.0,
    last_verified REAL,
    verified_by   TEXT NOT NULL DEFAULT '',
    embedding     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    src        INTEGER NOT NULL,
    dst        INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'related',
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    PRIMARY KEY (src, dst, kind)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_nodes_scope ON nodes(scope);
"""


class GraphMemory:
    """The store. Thread/process-safe enough for concurrent sessions via WAL."""

    def __init__(self, db_path: str | Path | None = None, *, clock=time.time):
        self.path = Path(db_path) if db_path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.db = sqlite3.connect(str(self.path), timeout=30.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self._dim_checked = False

    def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(nodes)")}
        if "type" not in cols:
            self.db.execute("ALTER TABLE nodes ADD COLUMN type TEXT NOT NULL DEFAULT 'fact'")
        if "verified_by" not in cols:
            self.db.execute("ALTER TABLE nodes ADD COLUMN verified_by TEXT NOT NULL DEFAULT ''")

    def _ensure_dim(self) -> None:
        """Verify the DB's stored vectors match the active embedder's dimension,
        once, lazily — only when we're about to embed something. Catches the
        silent-corruption case where a DB built with the semantic model gets
        opened under the lexical fallback (or vice versa). Kept out of __init__
        so opening the store (and pure-read endpoints) never pays the model-load
        cost."""
        if self._dim_checked:
            return
        self._dim_checked = True
        row = self.db.execute(
            "SELECT length(embedding) AS n FROM nodes LIMIT 1"
        ).fetchone()
        if not row:
            return  # empty DB — first insert sets the dimension
        stored_dim = row["n"] // 4  # float32 = 4 bytes
        active_dim = len(embed("dimension probe"))
        if stored_dim != active_dim:
            raise RuntimeError(
                f"embedding dimension mismatch: {self.path} holds {stored_dim}-dim "
                f"vectors but the active embedder produces {active_dim}-dim. The DB "
                f"was built with a different embedder (check $CC_MEM_EMBEDDER / "
                f"$CC_MEM_MODEL). Use the same embedder, or start a fresh DB."
            )

    def close(self) -> None:
        self.db.close()

    # ── Writes ──────────────────────────────────────────────────────────────

    def insert(
        self,
        content: str,
        summary: str = "",
        label: str = "",
        importance: float = 1.0,
        links: list | None = None,
        project: str = "",
        scope: str = "global",
        sources: str = "",
        confidence: float = 1.0,
        type: str = "fact",
        verified_by: str = "",
    ) -> int:
        """Write a node + its vector + its links atomically. Returns node id.

        `links` is a list of [other_id, kind, weight] — each becomes a symmetric
        edge so association works both ways. Embedding is computed from
        label + summary + content so a node is findable by all three."""
        self._ensure_dim()
        now = self._clock()
        text = " ".join(p for p in (label, summary, content) if p)
        vec = embed(text)
        cur = self.db.execute(
            """INSERT INTO nodes
               (content, summary, label, importance, type, project, scope,
                created_at, last_accessed, access_count, sources, confidence,
                last_verified, verified_by, embedding)
               VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
            (content, summary, label, importance, type, project, scope,
             now, now, sources, confidence,
             now if verified_by else None, verified_by, _pack(vec)),
        )
        node_id = int(cur.lastrowid or 0)

        for link in links or []:
            other, kind, weight = self._normalize_link(link)
            if other == node_id or not self._node_exists(other):
                continue
            self._upsert_edge(node_id, other, kind, weight, now)
            self._upsert_edge(other, node_id, kind, weight, now)

        self.db.commit()
        return node_id

    def update_node(self, node_id: int, **fields) -> dict | None:
        """Edit a node's fields. If content/summary/label change, the embedding
        is recomputed so search stays correct. Returns the fresh node (no access
        bump) or None if it doesn't exist. Accepts: content, summary, label,
        importance, scope, project, confidence, sources."""
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            return None
        allowed = {"content", "summary", "label", "importance", "scope",
                   "project", "confidence", "sources", "type",
                   "verified_by", "last_verified"}
        merged = {k: row[k] for k in allowed}
        for k, v in fields.items():
            if k in allowed and v is not None:
                merged[k] = v

        sets = [f"{k}=?" for k in allowed]
        params = [merged[k] for k in allowed]

        # Re-embed only when the embedded text actually changed.
        text_changed = any(
            fields.get(k) is not None and fields[k] != row[k]
            for k in ("label", "summary", "content")
        )
        if text_changed:
            self._ensure_dim()
            text = " ".join(p for p in (merged["label"], merged["summary"],
                                        merged["content"]) if p)
            sets.append("embedding=?")
            params.append(_pack(embed(text)))

        params.append(node_id)
        self.db.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?", params)
        self.db.commit()
        fresh = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._full(fresh)

    def delete_node(self, node_id: int) -> bool:
        """Delete a node and every edge touching it. Returns False if missing."""
        if not self._node_exists(node_id):
            return False
        self.db.execute("DELETE FROM edges WHERE src=? OR dst=?", (node_id, node_id))
        self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        self.db.commit()
        return True

    def unlink(self, a: int, b: int, kind: str | None = None) -> int:
        """Remove the edge(s) between two nodes (both directions). If kind is
        given, only that kind. Returns rows removed."""
        if kind:
            cur = self.db.execute(
                "DELETE FROM edges WHERE ((src=? AND dst=?) OR (src=? AND dst=?)) AND kind=?",
                (a, b, b, a, kind))
        else:
            cur = self.db.execute(
                "DELETE FROM edges WHERE (src=? AND dst=?) OR (src=? AND dst=?)",
                (a, b, b, a))
        self.db.commit()
        return cur.rowcount

    def link(self, src: int, dst: int, kind: str = "related", weight: float = 1.0) -> dict:
        """Create/strengthen a symmetric edge. Alias of reinforce with a clearer
        name for UI 'connect' actions."""
        return self.reinforce(src, dst, kind, weight)

    @staticmethod
    def _normalize_link(link) -> tuple[int, str, float]:
        if isinstance(link, (list, tuple)):
            other = int(link[0])
            kind = str(link[1]) if len(link) > 1 and link[1] else "related"
            weight = float(link[2]) if len(link) > 2 and link[2] is not None else 1.0
            return other, kind, weight
        return int(link), "related", 1.0

    def _node_exists(self, node_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM nodes WHERE id=?", (node_id,)
        ).fetchone() is not None

    def _upsert_edge(self, src: int, dst: int, kind: str, weight: float, now: float) -> None:
        self.db.execute(
            """INSERT INTO edges (src, dst, kind, weight, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(src, dst, kind)
               DO UPDATE SET weight = weight + excluded.weight""",
            (src, dst, kind, weight, now),
        )

    def reinforce(self, src: int, dst: int, kind: str = "related", delta: float = 0.5) -> dict:
        """Strengthen (or create) the edge between two nodes that proved useful
        together. Symmetric. This is how the graph learns its own shape."""
        now = self._clock()
        if not (self._node_exists(src) and self._node_exists(dst)):
            return {"ok": False, "error": "src or dst does not exist"}
        self._upsert_edge(src, dst, kind, delta, now)
        self._upsert_edge(dst, src, kind, delta, now)
        self.db.commit()
        w = self.db.execute(
            "SELECT weight FROM edges WHERE src=? AND dst=? AND kind=?", (src, dst, kind)
        ).fetchone()
        return {"ok": True, "src": src, "dst": dst, "kind": kind, "weight": w["weight"] if w else delta}

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, node_id: int) -> dict | None:
        """Full content of one node. The only fat payload. Bumps access stats
        (recency + count) because actually reading a node is the real signal
        that it was useful."""
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            return None
        now = self._clock()
        self.db.execute(
            "UPDATE nodes SET last_accessed=?, access_count=access_count+1 WHERE id=?",
            (now, node_id),
        )
        self.db.commit()
        return self._full(row)

    def search(self, query: str, k: int = 5, scope: str | None = None,
               project: str | None = None) -> list[dict]:
        """COLD START / global. Linear cosine scan over the whole index to find
        an entry node. Returns compact briefs only.

        `scope='project'` + `project=<cwd>` narrows to one project's memories;
        omit both for the whole shared brain (the default)."""
        self._ensure_dim()
        qvec = embed(query)
        now = self._clock()
        sql = "SELECT id, label, summary, importance, last_accessed, embedding FROM nodes"
        clauses, params = [], []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if project:
            clauses.append("project=?")
            params.append(project)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        scored = []
        for row in self.db.execute(sql, params):
            sim = cosine(qvec, _unpack(row["embedding"]))
            score = (ALPHA * sim
                     + GAMMA * _norm_importance(row["importance"])
                     + DELTA * _recency(row["last_accessed"], now))
            scored.append((score, sim, row))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [self._brief(r, score=s, sim=sm) for s, sm, r in scored[:k]]

    def search_neighbors(self, anchor_id: int, query: str, k: int = 5,
                         hops: int = 3) -> list[dict]:
        """SCOPED / lean path. Spread activation out from the anchor over the
        local subgraph, score candidates with the §3.5 blend, return the top-k
        compact briefs. Never scans the whole index."""
        if not self._node_exists(anchor_id):
            return []
        self._ensure_dim()
        qvec = embed(query)
        now = self._clock()

        activation, hopdist = self._spread(anchor_id, hops)
        scored = []
        for nid, act in activation.items():
            if nid == anchor_id:
                continue
            row = self.db.execute(
                "SELECT id, label, summary, importance, last_accessed, embedding FROM nodes WHERE id=?",
                (nid,),
            ).fetchone()
            if not row:
                continue
            sim = cosine(qvec, _unpack(row["embedding"]))
            score = (ALPHA * sim
                     + BETA * act
                     + GAMMA * _norm_importance(row["importance"])
                     + DELTA * _recency(row["last_accessed"], now)
                     - EPSILON * hopdist.get(nid, hops))
            scored.append((score, sim, act, row))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [self._brief(r, score=s, sim=sm, activation=a) for s, sm, a, r in scored[:k]]

    def _spread(self, anchor_id: int, hops: int) -> tuple[dict, dict]:
        """Spreading activation (random-walk-with-restart flavored). Energy
        starts on the anchor and flows along edges, decaying with distance and
        scaled by edge weight, so a node reachable weakly from two directions
        can still light up. Returns (activation_by_node, min_hop_by_node)."""
        decay = 0.6
        activation = {anchor_id: 1.0}
        hopdist = {anchor_id: 0}
        frontier = {anchor_id: 1.0}
        for hop in range(1, hops + 1):
            nxt: dict[int, float] = {}
            for node, energy in frontier.items():
                rows = self.db.execute(
                    "SELECT dst, weight FROM edges WHERE src=?", (node,)
                ).fetchall()
                total_w = sum(r["weight"] for r in rows) or 1.0
                for r in rows:
                    dst = r["dst"]
                    passed = energy * decay * (r["weight"] / total_w)
                    if passed <= 0.001:
                        continue
                    nxt[dst] = nxt.get(dst, 0.0) + passed
                    activation[dst] = activation.get(dst, 0.0) + passed
                    if dst not in hopdist:
                        hopdist[dst] = hop
            if not nxt:
                break
            frontier = nxt
        return activation, hopdist

    def expand(self, node_id: int) -> dict:
        """Walk: direct neighbors of a node with edge kind + weight. Lets Claude
        pull in more neighborhood on demand instead of front-loading it."""
        if not self._node_exists(node_id):
            return {"id": node_id, "exists": False, "neighbors": []}
        rows = self.db.execute(
            """SELECT e.dst AS nid, e.kind, e.weight, n.label, n.summary
               FROM edges e JOIN nodes n ON n.id = e.dst
               WHERE e.src=? ORDER BY e.weight DESC""",
            (node_id,),
        ).fetchall()
        neighbors = [
            {"id": r["nid"], "kind": r["kind"], "weight": round(r["weight"], 3),
             "label": r["label"], "summary": r["summary"]}
            for r in rows
        ]
        return {"id": node_id, "exists": True, "neighbors": neighbors}

    def stats(self) -> dict:
        n = self.db.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
        e = self.db.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        scopes = {
            r["scope"]: r["c"]
            for r in self.db.execute("SELECT scope, COUNT(*) c FROM nodes GROUP BY scope")
        }
        projects = self.db.execute(
            "SELECT COUNT(DISTINCT project) c FROM nodes WHERE project<>''"
        ).fetchone()["c"]
        return {
            "nodes": n,
            "edges": e,  # directed rows; symmetric edges count twice
            "by_scope": scopes,
            "projects": projects,
            "db_path": str(self.path),
        }

    # ── Shaping helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _brief(row, *, score=None, sim=None, activation=None) -> dict:
        out = {
            "id": row["id"],
            "label": row["label"],
            "summary": row["summary"],
        }
        if score is not None:
            out["score"] = round(score, 4)
        if sim is not None:
            out["similarity"] = round(sim, 4)
        if activation is not None:
            out["activation"] = round(activation, 4)
        return out

    @staticmethod
    def _full(row) -> dict:
        return {
            "id": row["id"],
            "label": row["label"],
            "summary": row["summary"],
            "content": row["content"],
            "importance": row["importance"],
            "type": row["type"],
            "project": row["project"],
            "scope": row["scope"],
            "created_at": row["created_at"],
            "last_accessed": row["last_accessed"],
            "access_count": row["access_count"],
            "sources": row["sources"],
            "confidence": row["confidence"],
            "last_verified": row["last_verified"],
            "verified_by": row["verified_by"],
        }


def _norm_importance(importance: float) -> float:
    """Squash importance into ~[0,1] so it can't dominate the blend."""
    return importance / (1.0 + importance)

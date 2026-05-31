"""cc-mem web backend — a small stdlib JSON API over the graph memory.

Single-threaded on purpose: one SQLite connection, one user, no concurrency
headaches. Reuses GraphMemory for every write so edits/creates re-embed the
node and search stays correct.

Routes (all JSON):
    GET    /api/stats
    GET    /api/nodes?q=&scope=&sort=&order=&limit=
    GET    /api/nodes/{id}                 full node + neighbors
    POST   /api/nodes                      create  {content,summary,label,...,links}
    PUT    /api/nodes/{id}                 edit    (re-embeds if text changed)
    DELETE /api/nodes/{id}
    GET    /api/graph                      nodes + de-duped edges
    POST   /api/search                     {query, mode: text|semantic, k}
    POST   /api/edges                      {src,dst,kind,weight}   connect
    DELETE /api/edges?a=&b=&kind=          disconnect

If ui/dist exists (after `npm run build`) it is served at / for a one-command
production mode. In dev, run the Vite server and let it proxy /api here.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from brain import Brain          # used for pending-approval (routes to one project)
from workspace import Workspace  # the dashboard view: global + ALL projects

_REPO = Path(__file__).resolve().parent.parent
_DIST = _REPO / "ui" / "dist"

_ws = None


def ws():
    global _ws
    if _ws is None:
        _ws = Workspace()  # global brain + every registered project
    return _ws


# ── API handlers (return plain dicts/lists; routed below) ────────────────────

def api_context(_m, _q, _b):
    return ws().context()


def api_stats(_m, _q, _b):
    return ws().stats()


def api_version(_m, _q, _b):
    """Cheap change marker for live updates. Folds together: the global DB's
    data_version (bumps when another connection commits — e.g. the MCP server),
    its counts, and the project files' fingerprint (bumps on git pull or a
    Claude session writing a project memory). Also reloads the project store
    from disk when its files changed, so the UI stays live for team edits too."""
    b = ws()
    b.reload_project_if_changed()
    g = b.global_store
    dv = g.db.execute("PRAGMA data_version").fetchone()[0]
    n = g.db.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
    e = g.db.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    psig = b.signature()
    return {"version": f"{dv}-{n}-{e}-{psig}"}


def api_list_nodes(_m, q, _b):
    text = (q.get("q", [""])[0] or "").strip()
    scope = (q.get("scope", [""])[0] or "").strip()
    sort = (q.get("sort", ["created_at"])[0] or "created_at")
    order = (q.get("order", ["desc"])[0] or "desc")
    return ws().list_nodes(q=text, scope=scope, sort=sort, order=order)


def api_get_node(_m, _q, _b, node_id):
    node = ws().get(node_id)
    return node if node else ({"error": "not found"}, 404)


def api_create_node(_m, _q, body):
    if not (body.get("content") or "").strip():
        return {"error": "content is required"}, 400
    res = ws().insert(
        content=body["content"], summary=body.get("summary", ""),
        label=body.get("label", ""), importance=float(body.get("importance", 1.0)),
        scope=body.get("scope", "global"), project=body.get("project", ""),
        sources=body.get("sources", ""), confidence=float(body.get("confidence", 1.0)),
        type=body.get("type", "fact"))
    return res, (201 if res.get("ok") else 409)


def api_update_node(_m, _q, body, node_id):
    fields = {k: body.get(k) for k in
              ("content", "summary", "label", "importance", "confidence", "sources", "type")}
    node = ws().update(node_id, **fields)
    if node is None:
        return {"error": "not found"}, 404
    return {"ok": True, "node": node}


def api_delete_node(_m, _q, _b, node_id):
    ok = ws().delete(node_id)
    return ({"ok": True} if ok else ({"error": "not found"}, 404))


def api_graph(_m, _q, _b):
    return ws().graph()


def api_search(_m, _q, body):
    query = (body.get("query") or "").strip()
    if not query:
        return {"hits": []}
    k = int(body.get("k", 20))
    scope = body.get("scope", "auto")
    if body.get("mode") == "semantic":
        return {"mode": "semantic", "hits": ws().search(query, k, scope=scope)}
    # text mode: substring over labels/summaries across both tiers
    ql = query.lower()
    rows = ws().list_nodes(q=query)["nodes"]
    hits = [{"id": r["id"], "label": r["label"], "summary": r["summary"],
             "scope": r.get("scope"), "tier": r.get("tier")} for r in rows[:k]
            if ql in (r.get("label", "") or "").lower()
            or ql in (r.get("summary", "") or "").lower()]
    return {"mode": "text", "hits": hits}


def api_create_edge(_m, _q, body):
    return ws().link(body["src"], body["dst"],
                        body.get("kind", "related"), float(body.get("weight", 1.0)))


def api_delete_edge(_m, q, _b):
    return ws().unlink(q["a"][0], q["b"][0], (q.get("kind", [None])[0]))


# ── Pending capture proposals (from the SessionEnd hook) ──────────────────────

def _pending_dir() -> Path:
    from graph_memory import default_db_path
    return default_db_path().parent / "pending"


def api_pending_list(_m, _q, _b):
    pend = _pending_dir()
    files = []
    if pend.exists():
        for f in sorted(pend.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                files.append({"file": f.name, "cwd": data.get("cwd", ""),
                              "proposals": data.get("proposals", [])})
            except Exception:
                continue
    total = sum(len(f["proposals"]) for f in files)
    return {"files": files, "total": total}


def _load_pending(name: str):
    f = _pending_dir() / Path(name).name
    return f, (json.loads(f.read_text()) if f.exists() else None)


def api_pending_approve(_m, _q, body):
    f, data = _load_pending(body.get("file", ""))
    if not data:
        return {"error": "pending file not found"}, 404
    idx = int(body.get("index", -1))
    props = data.get("proposals", [])
    if not (0 <= idx < len(props)):
        return {"error": "bad index"}, 400
    p = {**props[idx], **(body.get("overrides") or {})}  # allow edits on approve
    # route to the proposal's own project so project-scope lands in the right repo
    target = Brain(cwd=Path(data["cwd"]) if data.get("cwd") else None)
    res = target.insert(content=p.get("content", ""), summary=p.get("summary", ""),
                        label=p.get("label", ""), type=p.get("type", "fact"),
                        scope=p.get("scope", "global"), force=True)
    target.close()
    if res.get("ok"):
        props.pop(idx)
        _save_or_delete(f, data, props)
    return res


def api_pending_dismiss(_m, _q, body):
    f, data = _load_pending(body.get("file", ""))
    if not data:
        return {"error": "not found"}, 404
    props = data.get("proposals", [])
    idx = int(body.get("index", -1))
    if 0 <= idx < len(props):
        props.pop(idx)
    _save_or_delete(f, data, props)
    return {"ok": True}


def _save_or_delete(f: Path, data: dict, props: list):
    if props:
        data["proposals"] = props
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        f.unlink(missing_ok=True)


# ── Routing ──────────────────────────────────────────────────────────────────

_ID = r"([\w:.\-]+)"
ROUTES = [
    ("GET", r"^/api/context$", api_context),
    ("GET", r"^/api/stats$", api_stats),
    ("GET", r"^/api/version$", api_version),
    ("GET", r"^/api/nodes$", api_list_nodes),
    ("GET", rf"^/api/nodes/{_ID}$", api_get_node),
    ("POST", r"^/api/nodes$", api_create_node),
    ("PUT", rf"^/api/nodes/{_ID}$", api_update_node),
    ("DELETE", rf"^/api/nodes/{_ID}$", api_delete_node),
    ("GET", r"^/api/graph$", api_graph),
    ("POST", r"^/api/search$", api_search),
    ("POST", r"^/api/edges$", api_create_edge),
    ("DELETE", r"^/api/edges$", api_delete_edge),
    ("GET", r"^/api/pending$", api_pending_list),
    ("POST", r"^/api/pending/approve$", api_pending_approve),
    ("POST", r"^/api/pending/dismiss$", api_pending_dismiss),
]
COMPILED = [(m, re.compile(p), fn) for m, p, fn in ROUTES]


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, code=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self):
        """Serve the built UI (production) or a hint if it isn't built yet."""
        path = urlparse(self.path).path
        rel = path.lstrip("/") or "index.html"
        target = (_DIST / rel).resolve()
        if not _DIST.exists():
            return self._json({"error": "UI not built. In dev run `npm run dev` in "
                                        "ui/ (it proxies /api here). For one-command "
                                        "mode run `npm run build` first."}, 503)
        if not str(target).startswith(str(_DIST)) or not target.is_file():
            target = _DIST / "index.html"  # SPA fallback
        ctype = {
            ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
            ".svg": "image/svg+xml", ".json": "application/json",
            ".png": "image/png", ".ico": "image/x-icon",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            if method == "GET":
                return self._static()
            return self._json({"error": "not found"}, 404)
        q = parse_qs(parsed.query)
        body = {}
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON body"}, 400)
        for m, rx, fn in COMPILED:
            if m != method:
                continue
            match = rx.match(parsed.path)
            if not match:
                continue
            try:
                args = match.groups()
                if method in ("POST", "PUT"):
                    result = fn(method, q, body, *args)
                else:
                    result = fn(method, q, body, *args)
            except Exception as exc:
                import traceback; traceback.print_exc()
                return self._json({"error": str(exc)}, 500)
            if isinstance(result, tuple):
                return self._json(result[0], result[1])
            return self._json(result)
        return self._json({"error": f"no route for {method} {parsed.path}"}, 404)

    def do_OPTIONS(self):
        self._json({}, 204)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, *a):
        pass


def serve(port: int = 8765):
    # Don't construct the Workspace here — it would embed every project's memory and
    # load the model before we bind. Bind instantly; build lazily on first request.
    from graph_memory import default_db_path
    import registry
    print(f"[cc-mem api] global: {default_db_path()}")
    print(f"[cc-mem api] projects: {len(registry.list_projects())} registered")
    served = "+ UI" if _DIST.exists() else "(API only — run Vite for the UI)"
    print(f"[cc-mem api] http://localhost:{port}  {served}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()

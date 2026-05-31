#!/usr/bin/env python3
"""Tests for cc-mem: core store behavior + a full MCP-over-stdio session.

Run:  python3 test_memory.py
Zero dependencies — just stdlib + the modules in this folder.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from embedder import cosine, embed
from graph_memory import GraphMemory

HERE = Path(__file__).parent
_passed = 0
_failed = 0


def check(cond, label):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}")


# ── Embedder ──────────────────────────────────────────────────────────────────

def test_embedder():
    print("embedder")
    v1 = embed("docker compose networking")
    v2 = embed("docker compose networking")
    check(v1 == v2, "deterministic: same text -> same vector")
    near = cosine(embed("docker container build"), embed("dockerfile container image"))
    far = cosine(embed("docker container build"), embed("french revolution napoleon"))
    check(near > far, "lexically related text scores higher than unrelated")
    check(abs(sum(x * x for x in v1) - 1.0) < 1e-6 or sum(x * x for x in v1) == 0,
          "vectors are L2-normalized")


# ── Store core ──────────────────────────────────────────────────────────────

def test_core():
    print("store core")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "m.db"
        m = GraphMemory(db)

        a = m.insert("Use WAL mode for concurrent SQLite access from many sessions.",
                     summary="SQLite WAL for concurrency", label="sqlite-wal",
                     importance=2.0)
        b = m.insert("Set busy_timeout so concurrent writers wait instead of erroring.",
                     summary="SQLite busy_timeout", label="sqlite-busy",
                     links=[[a, "related", 1.0]])
        c = m.insert("Napoleon crowned himself emperor in 1804.",
                     summary="Napoleon 1804", label="napoleon")
        check(a and b and c, "insert returns node ids")

        # Cold-start global search finds the relevant node, not the unrelated one.
        hits = m.search("sqlite concurrency settings", k=3)
        check(hits and hits[0]["id"] in (a, b), "global search surfaces sqlite nodes first")
        check(all("content" not in h for h in hits), "search returns briefs, not full content")

        # Briefs carry id/label/summary/score.
        check(all({"id", "label", "summary", "score"} <= set(h) for h in hits),
              "briefs have id/label/summary/score")

        # Full content only via get.
        full = m.get(a)
        check(full and "WAL" in full["content"], "memory_get returns full content")
        check(m.get(a)["access_count"] >= 1, "get bumps access_count")

        # Neighbor search walks the local subgraph from the anchor.
        nb = m.search_neighbors(a, "timeout for writers", k=3)
        check(any(h["id"] == b for h in nb), "neighbor search reaches linked node b")
        check(all(h["id"] != c for h in nb), "neighbor search does NOT reach unlinked node c")

        # Expand lists direct neighbors with edge metadata.
        exp = m.expand(a)
        check(exp["exists"] and any(n["id"] == b for n in exp["neighbors"]),
              "expand lists direct neighbor b with edge info")

        # Reinforcement strengthens the edge and persists.
        before = next(n["weight"] for n in m.expand(a)["neighbors"] if n["id"] == b)
        m.reinforce(a, b, "co-used", 0.7)
        after_co = m.expand(a)["neighbors"]
        check(any(n["id"] == b and n["kind"] == "co-used" for n in after_co),
              "reinforce creates the co-used edge")

        # Multi-hop: chain a-b-d, spreading activation should reach d from a.
        dd = m.insert("Enable synchronous=NORMAL with WAL for a good durability/speed trade.",
                      summary="synchronous NORMAL", label="sqlite-sync",
                      links=[[b, "related", 1.0]])
        nb2 = m.search_neighbors(a, "durability speed tradeoff", k=5, hops=3)
        check(any(h["id"] == dd for h in nb2), "spreading activation reaches 2-hop node d")

        stats = m.stats()
        check(stats["nodes"] == 4, "stats counts all nodes")
        m.close()


# ── Scope / project isolation ─────────────────────────────────────────────────

def test_scope():
    print("scope + project isolation")
    with tempfile.TemporaryDirectory() as d:
        m = GraphMemory(Path(d) / "m.db")
        g = m.insert("Always run the full test suite before pushing.",
                     summary="run tests before push", label="g", scope="global",
                     project="/proj/alpha")
        p = m.insert("Alpha service deploys via the deploy-alpha.sh script.",
                     summary="alpha deploy script", label="p", scope="project",
                     project="/proj/alpha")
        only_proj = m.search("deploy script", k=5, scope="project", project="/proj/alpha")
        check(any(h["id"] == p for h in only_proj), "project-scoped search finds project node")
        global_only = m.search("workflow practice", k=5, scope="global")
        check(all(h["id"] != p for h in global_only), "global-scoped search excludes project node")
        m.close()


# ── Cross-session persistence (two separate processes, one DB file) ───────────

def test_cross_session_persistence():
    print("cross-session persistence")
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "shared.db")

        # Session 1: learn + link.
        m1 = GraphMemory(db)
        x = m1.insert("Prefer ripgrep over grep for speed in large repos.",
                      summary="use ripgrep", label="rg", importance=1.5)
        y = m1.insert("ripgrep respects .gitignore by default.",
                      summary="rg gitignore", label="rg-ignore",
                      links=[[x, "related", 1.0]])
        m1.reinforce(x, y, "co-used", 1.0)
        m1.close()

        # Session 2: brand-new process/connection, same file. Must see it all.
        m2 = GraphMemory(db)
        hits = m2.search("fast search tool for repos", k=3)
        check(any(h["id"] == x for h in hits), "session 2 cold-starts and finds session 1's node")
        nb = m2.search_neighbors(x, "ignore files", k=3)
        check(any(h["id"] == y for h in nb), "session 2 sees the edge session 1 created")
        weight = next(n["weight"] for n in m2.expand(x)["neighbors"]
                      if n["id"] == y and n["kind"] == "co-used")
        check(weight >= 1.0, "session 2 sees the reinforced edge weight")
        m2.close()


# ── Full MCP-over-stdio session simulation ────────────────────────────────────

class MCPClient:
    """Minimal JSON-RPC-over-stdio client driving the real server subprocess."""

    def __init__(self, db_path: str):
        import os
        env = dict(os.environ)  # inherit so CC_MEM_EMBEDDER reaches the subprocess
        env["CC_MEM_DB"] = db_path
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "mcp_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, bufsize=1,
        )
        self._id = 0

    def _send(self, method, params=None, *, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line)

    def call_tool(self, name, arguments):
        resp = self._send("tools/call", {"name": name, "arguments": arguments})
        text = resp["result"]["content"][0]["text"]
        return json.loads(text)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def test_mcp_stdio():
    print("MCP stdio session")
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "mcp.db")
        c = MCPClient(db)
        try:
            init = c._send("initialize", {"protocolVersion": "2024-11-05",
                                          "capabilities": {}, "clientInfo": {"name": "test"}})
            check(init["result"]["serverInfo"]["name"] == "cc-mem", "initialize handshake")
            c._send("notifications/initialized", notify=True)

            listed = c._send("tools/list")
            names = {t["name"] for t in listed["result"]["tools"]}
            check({"memory_search", "memory_insert", "memory_get",
                   "memory_search_neighbors", "memory_expand", "memory_reinforce",
                   "memory_stats"} <= names, "tools/list exposes all 7 tools")

            r1 = c.call_tool("memory_insert", {
                "content": "The CI gate requires 80% coverage; below that the merge blocks.",
                "summary": "CI needs 80% coverage", "label": "ci-coverage",
                "scope": "global", "confidence": 0.9, "sources": "checked .github/workflows"})
            check(r1.get("ok") and isinstance(r1.get("id"), str) and r1["id"].startswith("g:"),
                  "insert via MCP returns namespaced id")
            nid = r1["id"]

            r2 = c.call_tool("memory_insert", {
                "content": "Coverage is measured by pytest-cov and uploaded to the CI artifact.",
                "summary": "pytest-cov measures coverage", "label": "ci-cov-tool",
                "links": [[nid, "related", 1.0]]})
            n2 = r2["id"]

            srch = c.call_tool("memory_search", {"query": "merge coverage requirement", "k": 3})
            check(srch["count"] >= 1 and any(h["id"] == nid for h in srch["hits"]),
                  "search via MCP finds the inserted node")

            got = c.call_tool("memory_get", {"node_id": nid})
            check("80%" in got["content"], "get via MCP returns full content")
            check(got["confidence"] == 0.9, "provenance/confidence persisted")

            nb = c.call_tool("memory_search_neighbors",
                             {"anchor_id": nid, "query": "how coverage is measured", "k": 3})
            check(any(h["id"] == n2 for h in nb["hits"]), "neighbor search via MCP reaches linked node")

            rein = c.call_tool("memory_reinforce", {"src": nid, "dst": n2, "kind": "co-used", "delta": 1.0})
            check(rein.get("ok"), "reinforce via MCP ok")

            stats = c.call_tool("memory_stats", {})
            check(stats["nodes"] == 2, "stats via MCP reflects inserts")
        finally:
            c.close()

        # Second MCP process, same DB file -> the durable shared brain.
        c2 = MCPClient(db)
        try:
            c2._send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
            c2._send("notifications/initialized", notify=True)
            srch = c2.call_tool("memory_search", {"query": "coverage gate for merging", "k": 3})
            check(srch["count"] >= 1, "a fresh MCP process recalls what the prior one learned")
        finally:
            c2.close()


def _make_repo(path: Path, remote: str):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {remote}\n')


def test_brain_hybrid():
    print("brain: hybrid global + project files")
    from brain import Brain
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        repo = d / "myapp"
        _make_repo(repo, "git@github.com:acme/myapp.git")
        gdb = str(d / "global.db")

        b = Brain(cwd=repo, global_db=gdb)
        check(b.context()["project_key"] == "github.com/acme/myapp", "git remote -> stable project key")

        g = b.insert("Use ripgrep.", summary="use ripgrep", label="rg", scope="global")
        p1 = b.insert("Deploy via deploy.sh.", summary="deploy script", label="deploy", scope="project")
        p2 = b.insert("Auth uses JWT.", summary="auth jwt", label="auth", scope="project",
                      links=[[p1["id"], "related", 1.0]])
        check(g["id"].startswith("g:") and p1["id"].startswith("p:"), "namespaced ids per tier")

        files = list((repo / ".cc-mem" / "nodes").glob("*.md"))
        check(len(files) == 2, "project memories written as one file each (git-committed)")

        hits = b.search("deployment", 5)
        check(any(h["tier"] == "project" for h in hits) and any(h["tier"] == "global" for h in hits),
              "default search blends global + project")
        b.close()

        # Reload (a teammate pulled the files / a fresh process) sees them.
        b2 = Brain(cwd=repo, global_db=gdb)
        got = b2.get(p2["id"])
        check(got and any(n["id"] == p1["id"] for n in got["neighbors"]),
              "project memory + links survive reload from files")

        # Isolation: a different repo sees only global, never myapp's project memory.
        other = d / "other"
        _make_repo(other, "git@github.com:acme/other.git")
        b3 = Brain(cwd=other, global_db=gdb)
        res = b3.search("deploy auth jwt", 10)
        check(all(h["tier"] == "global" for h in res),
              "other project does NOT see myapp's project memory (isolation)")
        b2.close(); b3.close()


def main():
    test_embedder()
    test_core()
    test_scope()
    test_cross_session_persistence()
    test_brain_hybrid()
    test_mcp_stdio()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()

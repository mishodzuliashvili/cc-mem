#!/usr/bin/env python3
"""cc-mem MCP server — hand-rolled, zero pip dependencies.

Speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0).
Runs on a bare system `python3` — no venv, no installs — which is exactly what
a memory meant to live globally in every Claude Code session needs.

The DB path comes from $CC_MEM_DB, else ~/.claude-cc-mem/memory.db. Because
every session points at the same file, anything one session learns, the next
one can recall. Logs go to stderr (stdout is reserved for the protocol).

Register globally (once):
    claude mcp add cc-mem --scope user -- python3 /ABS/PATH/cc-mem/mcp_server.py
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from brain import Brain  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "cc-mem", "version": "0.1.0"}

_brain: "Brain | None" = None


def brain() -> "Brain":
    global _brain
    if _brain is None:
        _brain = Brain()  # detects the current repo from cwd for project scope
        ctx = _brain.context()
        log(f"global={ctx['global_db']}  project={ctx['project_key'] or '(none)'}")
    return _brain


def log(msg: str) -> None:
    print(f"[cc-mem] {msg}", file=sys.stderr, flush=True)


# ── Tool definitions: (schema for tools/list) + (handler) ────────────────────

_ID_NOTE = ("Node ids are namespaced strings: 'g:<n>' = global (your private, "
            "cross-project brain) and 'p:<uuid>' = project (this repo's shared, "
            "git-committed memory). Pass them back verbatim.")

TOOLS = [
    {
        "name": "memory_context",
        "description": (
            "Call FIRST in a session. Tells you which global brain and which "
            "project (git repo) this session is bound to, so you know whether "
            "project-scoped memory is available and what it covers."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_search",
        "description": (
            "COLD START recall. Use at the start of a task to find an entry point. "
            "By default (scope='auto') it searches your GLOBAL brain + the CURRENT "
            "project's memory together, and never other projects — so results are "
            "always relevant to where you are. Returns compact briefs "
            "{id,label,summary,score,tier}, NOT full content; call memory_get for "
            "the one node you want. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you're trying to recall."},
                "k": {"type": "integer", "description": "Max hits (default 5).", "default": 5},
                "scope": {"type": "string", "default": "auto",
                          "description": "'auto' (global+project), 'global', or 'project'."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_search_neighbors",
        "description": (
            "SCOPED / lean recall. Once you have an anchor node, walk only its "
            "local neighborhood via spreading activation instead of rescanning. "
            "Multi-hop, edge-weight aware. Stays within the anchor's tier. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "anchor_id": {"type": "string", "description": "Node id to walk out from (e.g. 'g:3' or 'p:ab12')."},
                "query": {"type": "string", "description": "What you're looking for nearby."},
                "k": {"type": "integer", "default": 5},
                "hops": {"type": "integer", "description": "Spread radius (default 3).", "default": 3},
            },
            "required": ["anchor_id", "query"],
        },
    },
    {
        "name": "memory_expand",
        "description": "Walk: list a node's direct neighbors with edge kind + weight. " + _ID_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_get",
        "description": (
            "Drill in: return ONE node's FULL content (the only fat payload). Call "
            "deliberately for the single node you need. Bumps access stats. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_insert",
        "description": (
            "LEARN: persist a new memory. Save durable, reusable, ideally VERIFIED "
            "knowledge — not transient chatter. Write a tight one-line `summary` "
            "and short `label`; those drive recall.\n"
            "CHOOSE SCOPE DELIBERATELY:\n"
            "  scope='global'  — knowledge true for YOU across all projects "
            "(preferences, general verified facts). Private to you.\n"
            "  scope='project' — knowledge about THIS specific repo (architecture, "
            "deploy steps, gotchas, conventions). Saved as a committed file the "
            "whole team shares; isolated to this repo. Requires being in a git repo.\n"
            "When working on a specific app, prefer 'project' for app-specific facts "
            "so they don't pollute other projects. `links` = [[other_id, kind, "
            "weight], ...] (same tier only; kinds: related, causal, co-used, part-of).\n"
            "DEDUP: by default this REFUSES if a very similar memory already exists, "
            "returning the candidates so you can memory_update one instead of "
            "duplicating. Pass force=true only when it's genuinely new/distinct."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The full memory text (markdown ok)."},
                "summary": {"type": "string", "description": "One-line summary (drives recall)."},
                "label": {"type": "string", "description": "Short title."},
                "type": {"type": "string", "default": "fact",
                         "description": "One of: fact (verified knowledge), preference "
                         "(a rule/correction Claude must APPLY, not just recall), "
                         "decision (a choice + rationale), howto (a procedure that "
                         "worked), gotcha (a mistake/trap + its fix — a lesson learned), "
                         "reference (pointer to an external resource)."},
                "importance": {"type": "number", "default": 1.0},
                "links": {"type": "array", "description": "[[other_id, kind, weight], ...]",
                          "items": {"type": "array"}},
                "scope": {"type": "string", "default": "global", "description": "'global' or 'project'."},
                "sources": {"type": "string", "description": "Provenance: how you verified this."},
                "confidence": {"type": "number", "default": 1.0, "description": "0..1."},
                "force": {"type": "boolean", "default": False,
                          "description": "Bypass the duplicate check (only if truly new)."},
                "verify": {"type": "string", "description": "Optional shell command that "
                           "PROVES this memory (a test/build/probe). If given, it must "
                           "exit 0 or NOTHING is saved; the command is stored so the "
                           "fact can be re-verified later. Use for code/empirical claims."},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "Files this memory is DERIVED FROM (paths; "
                         "repo-relative for project scope). Their content is hashed now "
                         "so memory_verify can later detect when a source file changed "
                         "and flag this memory stale. Prefer this over writing paths in "
                         "prose — prose paths can't be checked."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_verify",
        "description": (
            "Re-check a memory's FRESHNESS: re-hash its file `refs` (detect when a "
            "source file changed/vanished) and re-run its `verify` command if it has "
            "one. On all-good, refreshes last-verified; if a source changed or the "
            "command fails, marks it stale and drops confidence — then you should "
            "re-read the file and memory_update the node. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_update",
        "description": (
            "REVISE: correct or refine an existing memory in place (don't insert a "
            "duplicate). Use this when the user corrects you, or you verify a better "
            "version of something already stored. Only pass the fields you're "
            "changing; the embedding is recomputed if text changes. Bump `confidence` "
            "and note what changed in `sources`. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "content": {"type": "string"}, "summary": {"type": "string"},
                "label": {"type": "string"}, "importance": {"type": "number"},
                "confidence": {"type": "number"}, "sources": {"type": "string"},
                "type": {"type": "string", "description": "fact|preference|decision|howto|gotcha|reference"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "Remove a memory that's wrong, obsolete, or superseded (also removes its "
            "edges). Prefer memory_update when the fact just needs correcting. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_get_many",
        "description": (
            "Fetch the FULL content of several nodes at once (request order). Use "
            "after a search/expand to pull a whole relevant cluster in one call. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["ids"],
        },
    },
    {
        "name": "memory_recall",
        "description": (
            "One-shot gather: search + return compact briefs for the top hits AND "
            "the full content of the top few, together. Convenient for pulling the "
            "relevant neighborhood in a single call. IDEAL to run inside a retrieval "
            "SUBAGENT: let the subagent recall broadly here, distill, and return only "
            "the facts the main session needs — keeping the main context lean."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 6, "description": "How many briefs."},
                "full": {"type": "integer", "default": 3, "description": "How many returned with full content."},
                "scope": {"type": "string", "default": "auto"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_relocate",
        "description": (
            "Recover a memory's MISSING file refs (renamed/moved). Hunts the repo "
            "for a file whose content hash matches the stored one — the same file at "
            "a new path — and re-links unambiguous matches in place. Use when "
            "memory_verify reports a ref 'missing'. If no hash match (the file also "
            "changed), the memory's content has the keywords to search for it "
            "yourself, then memory_update the path. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"},
                           "apply": {"type": "boolean", "default": True,
                                     "description": "Re-link unambiguous matches (else just report candidates)."}},
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_suggest_links",
        "description": (
            "Find existing nodes similar to this one that are NOT linked to it yet — "
            "connection candidates. Use it to densify the graph: when you notice (or "
            "want to check) that memories are related, get suggestions here and "
            "memory_reinforce the real ones. Connecting existing knowledge is as "
            "valuable as adding new. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"},
                           "k": {"type": "integer", "default": 5}},
            "required": ["node_id"],
        },
    },
    {
        "name": "memory_reinforce",
        "description": (
            "CONNECT or strengthen: create (or strengthen) the edge between two "
            "EXISTING nodes (same tier) — use it any time you see a relationship "
            "between memories, not only ones you just created. Symmetric. Kinds: "
            "related, causal, co-used, part-of. " + _ID_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "kind": {"type": "string", "default": "related"},
                "delta": {"type": "number", "default": 0.5},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "memory_stats",
        "description": "Introspect: global vs project node counts, project key, db path.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def handle_tool(name: str, args: dict) -> dict:
    b = brain()
    if name == "memory_context":
        return b.context()
    if name == "memory_search":
        hits = b.search(args["query"], int(args.get("k", 5)), scope=args.get("scope", "auto"))
        return {"count": len(hits), "hits": hits}
    if name == "memory_search_neighbors":
        hits = b.search_neighbors(args["anchor_id"], args["query"],
                                  int(args.get("k", 5)), int(args.get("hops", 3)))
        return {"count": len(hits), "hits": hits}
    if name == "memory_expand":
        return b.expand(args["node_id"])
    if name == "memory_get":
        node = b.get(args["node_id"])
        return node if node else {"error": "not found", "node_id": args.get("node_id")}
    if name == "memory_insert":
        return b.insert(
            content=args["content"], summary=args.get("summary", ""),
            label=args.get("label", ""), importance=float(args.get("importance", 1.0)),
            links=args.get("links"), scope=args.get("scope", "global"),
            sources=args.get("sources", ""), confidence=float(args.get("confidence", 1.0)),
            type=args.get("type", "fact"), force=bool(args.get("force", False)),
            verify=args.get("verify"), refs=args.get("refs"))
    if name == "memory_verify":
        return b.verify(args["node_id"])
    if name == "memory_update":
        fields = {k: args.get(k) for k in
                  ("content", "summary", "label", "importance", "confidence", "sources", "type")}
        node = b.update(args["node_id"], **fields)
        return {"ok": True, "node": node} if node else {"ok": False, "error": "not found"}
    if name == "memory_delete":
        return {"ok": b.delete(args["node_id"])}
    if name == "memory_get_many":
        nodes = b.get_many(args.get("ids", []))
        return {"count": len(nodes), "nodes": nodes}
    if name == "memory_recall":
        return b.recall(args["query"], int(args.get("k", 6)),
                        int(args.get("full", 3)), scope=args.get("scope", "auto"))
    if name == "memory_relocate":
        return b.relocate(args["node_id"], bool(args.get("apply", True)))
    if name == "memory_suggest_links":
        hits = b.suggest_links(args["node_id"], int(args.get("k", 5)))
        return {"count": len(hits), "candidates": hits}
    if name == "memory_reinforce":
        return b.link(args["src"], args["dst"],
                      args.get("kind", "related"), float(args.get("delta", 0.5)))
    if name == "memory_stats":
        return b.stats()
    raise ValueError(f"unknown tool: {name}")


# ── JSON-RPC / MCP transport ──────────────────────────────────────────────────

def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def dispatch(msg: dict):
    """Return a response dict, or None for notifications (no reply)."""
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _result(req_id, {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # notification, no response

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            payload = handle_tool(name, args)
            text = json.dumps(payload, ensure_ascii=False)
            return _result(req_id, {"content": [{"type": "text", "text": text}],
                                    "isError": False})
        except Exception as exc:  # surface tool errors as tool results, not RPC errors
            log("tool error:\n" + traceback.format_exc())
            text = json.dumps({"error": str(exc)})
            return _result(req_id, {"content": [{"type": "text", "text": text}],
                                    "isError": True})

    if req_id is not None:
        return _error(req_id, -32601, f"method not found: {method}")
    return None


def main() -> None:
    log("starting (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"skipping non-JSON line: {line[:120]!r}")
            continue
        try:
            response = dispatch(msg)
        except Exception:
            log("dispatch crashed:\n" + traceback.format_exc())
            response = _error(msg.get("id"), -32603, "internal error")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()

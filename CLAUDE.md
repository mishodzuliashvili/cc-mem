# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

cc-mem is a self-learning graph memory for Claude Code: a graph of nodes + typed/weighted
edges + embeddings, exposed to Claude as an MCP server and to humans as a web app. The
design/vision lives in `index.md` (its §3–5 are referenced from code comments).

## Commands

```bash
# Tests — a custom stdlib harness (NOT pytest), 36 checks incl. a real MCP-over-stdio
# subprocess. Run with the lexical embedder so it's fast and needs no model:
CC_MEM_EMBEDDER=lexical .venv/bin/python test_memory.py
# Run a single test: call its function, e.g.
CC_MEM_EMBEDDER=lexical .venv/bin/python -c "import test_memory as t; t.test_brain_hybrid(); print(t._passed,'passed',t._failed,'failed')"

# Web app — dev (Python API on :8765 + Vite hot-reload on :5173, proxied):
./dev.sh
# Web app — one-command prod (Python serves the built UI + API on :8765):
cd ui && npm run build && cd .. && CC_MEM_EMBEDDER=local .venv/bin/python manage.py
cd ui && npm run build        # build only
cd ui && npm run lint         # eslint

# Installer (idempotent). Pieces: deps | mcp | prompt | hooks | all
python3 setup.py all                 # venv+deps, register MCP, sync prompt, install recall hook
python3 setup.py hooks --capture     # also install the opt-in SessionEnd auto-capture hook

# MCP server (stdio JSON-RPC) — normally launched by Claude Code, not by hand:
python3 mcp_server.py
```

### Environment variables
- `CC_MEM_DB` — DB path (default `~/.claude-cc-mem/memory.db`).
- `CC_MEM_EMBEDDER` — `lexical` (zero-dep, default) or `local` (sentence-transformers). **Use `lexical` for tests** (fast, deterministic); the registered server uses `local`.
- `CC_MEM_MODEL` — local model name (default `all-MiniLM-L6-v2`, 384-dim).
- `CC_MEM_DUP_THRESHOLD` — cosine threshold for the dedup-on-insert guard (default `0.78`).

## Architecture

**Two tiers behind one facade.** `brain.py` (`Brain`) is the single entry point for both the
MCP server and the web API — never bypass it for writes. It unifies:
- **Global tier** — your private, cross-project memory in a persistent SQLite DB (`graph_memory.py`).
- **Project tier** — the current git repo's shared memory, stored as **one file per node** under
  `<repo>/.cc-mem/nodes/*.md` (`project.py`), committed to git so teammates' additions merge as
  new files (no conflicts). Files are the source of truth; embeddings are *derived* — on load
  they're rebuilt into a throwaway in-memory `GraphMemory(":memory:")`.

**Namespaced IDs.** Every node id is a string: `g:<int>` (global) or `p:<uuid>` (project). The
MCP tools and HTTP API take/return these strings; `Brain.parse_id` routes by prefix. Edges/links
only exist **within a tier** (cross-tier linking is rejected). When touching ids, preserve the
prefix end-to-end — project `_neighbors` return raw uuids and must be re-prefixed in `Brain`.

**Default recall is scoped.** `Brain.search(scope="auto")` = global + the current project only,
never other projects. Project identity is the normalized git remote (`project.project_key`), so
it's stable across machines/clones — not the folder path.

**One engine, two uses.** `graph_memory.py` is the storage+retrieval engine (nodes, edges,
vectors, spreading-activation neighbor recall, the §3.5 scoring blend). It backs both the
persistent global DB and the in-memory project store. The project store's int ids are ephemeral
(rebuilt each load); the portable id is the file uuid, and `ProjectMemory` maintains the
uid↔int mapping.

**Embedder is pluggable and dim-locked** (`embedder.py`). A DB is tied to ONE embedder: the
lexical hash is 256-dim, the local model 384-dim. `GraphMemory._ensure_dim` refuses to operate
if stored vectors don't match the active embedder (prevents silent corruption from running the
wrong `CC_MEM_EMBEDDER` against an existing DB).

**Everything loads lazily.** Opening a store does NOT load the model; the model loads on the
first `embed()` (search/insert). The web server binds instantly and read-only views (Table/Graph/
stats) never touch the model. **Do not reintroduce eager model loading** — it was a fixed
regression that delayed server startup and the SessionStart hook.

**MCP transport** (`mcp_server.py`) is hand-rolled stdio JSON-RPC with **zero pip dependencies**
(runs on bare `python3`). 13 tools, all routed through `Brain`. Tool errors are returned as tool
results (`isError: true`), not JSON-RPC errors.

**Web app.** `webapp/server.py` is a single-threaded stdlib HTTP server (one SQLite connection,
no concurrency issues) exposing a JSON API and serving `ui/dist` in prod. `ui/` is Vite + React
(Table / Search / Graph / Pending). The UI polls `/api/version` every 2s and refetches only when
the DB actually changed (`PRAGMA data_version` + counts + project-file fingerprint), so live
updates survive across processes (e.g. the MCP server writing in a Claude session).

**Self-learning loop.**
- `hooks/session_start.py` (SessionStart) injects preferences + project memories at session start
  — reads DB/files directly, **no embedding**, so keep it fast.
- `hooks/session_capture.py` (SessionEnd, opt-in) runs a headless `claude -p` to propose learnings
  into `~/.claude-cc-mem/pending/`, reviewed in the UI's Pending tab (approve → inserts).
- `prompt/memory-loop.md` is the canonical behavior prompt (insert/update/dedup/verify/types),
  synced into `~/.claude/CLAUDE.md` by `setup.py prompt`. Edit that file, not the installed copy.
- Writes are gated: `memory_insert` refuses near-duplicates (steer to `memory_update`) and, if
  `verify="<cmd>"` is passed, runs the command (`runner.py`) and only persists on exit 0.

## Gotchas

- **Adding a node column is a multi-file change**: `SCHEMA` + `_migrate()` + `insert()` + `_full()`
  in `graph_memory.py`, the frontmatter + `update`/`_load`/`get` in `project.py`, and the SELECTs
  in `Brain.list_nodes`/`graph`. Existing DBs are migrated additively in `_migrate()`.
- **Project-tier writes need the same embedder** the files were created under; editing re-embeds.
- The web app attaches the project of the repo it was launched from; launch it from the repo you
  want to inspect.

# cc-mem — self-learning graph memory for Claude Code

A persistent, **shared-across-all-sessions** memory for Claude Code, exposed as
an MCP server. Memories are **nodes** in a graph, connected by **weighted, typed
edges**, each carrying an **embedding** for similarity search. Recall is by
*association* (walk the local neighborhood) rather than by scanning everything,
so the context window stays lean.

See [`index.md`](index.md) for the full vision. This README is how to run it.

- **One SQLite file = the shared brain.** Every session opens the same file
  (`~/.claude-cc-mem/memory.db` by default). What one session learns, the next
  recalls. WAL mode + busy timeout make concurrent sessions safe.
- **Two embedder backends** (set by `$CC_MEM_EMBEDDER`):
  - `local` — a real sentence-transformers model (`all-MiniLM-L6-v2`, 384-dim)
    for true **semantic** recall (synonyms, paraphrase). Runs offline after a
    one-time model download. Needs the venv.
  - `lexical` — pure-stdlib hashing embedder, **zero dependencies**, runs on
    bare `python3`. Matches shared words only. The safe fallback.
- **The store itself is zero-dependency** (pure stdlib SQLite) regardless of
  backend — only the `local` embedder pulls in `sentence-transformers`.

## How sharing works (the thing you were unsure about)

There is **no sync step**. An MCP server is just a process each session spawns;
they all read/write the *same file on disk*. Register it **once at user scope**
and every Claude Code session on the machine — any project, any folder — shares
the one memory automatically. Persistence and sharing are literally the same
fact: the file.

**Isolation when you want it:** every node is tagged with the `project` (cwd) it
was created in and a `scope` (`global` | `project`). Default recall is the whole
brain; pass `scope="project"` + `project="<cwd>"` to a search to narrow to one
project. Two columns, no separate system.

## Quick install (one command)

After cloning, from the repo root:

```bash
python3 setup.py all       # create .venv + install deps, register the MCP server,
                           # and sync the memory-loop prompt into your CLAUDE.md
```

Or run the pieces individually: `python3 setup.py deps` / `mcp` / `prompt`.

**The system prompt lives in the repo** at [`prompt/memory-loop.md`](prompt/memory-loop.md)
— the single source of truth for how Claude should use the memory (the
self-learning loop). To change how Claude behaves, **edit that file and re-run
`python3 setup.py prompt`** — no hunting through `~/.claude`. It's injected
between marker comments, so re-running updates the block in place (never
duplicates), and it only writes to `~/.claude/CLAUDE.md` (plus any config dir
that already has a CLAUDE.md) so it can't shadow your other global rules.

`setup.py mcp` registers in every detected Claude config dir (`~/.claude`,
`$CLAUDE_CONFIG_DIR`, `~/.claude-work`) so all your Claude installs share one
brain.

## Install / register (manual)

**Semantic (recommended)** — needs the venv with `sentence-transformers`:

```bash
python3 -m venv .venv
.venv/bin/pip install sentence-transformers

claude mcp add cc-mem --scope user \
  -e CC_MEM_EMBEDDER=local \
  -e CC_MEM_DB=$HOME/.claude-cc-mem/memory.db \
  -- /ABS/PATH/TO/cc-mem/.venv/bin/python /ABS/PATH/TO/cc-mem/mcp_server.py
```

**Lexical / zero-dependency** — runs on bare system `python3`, no venv:

```bash
claude mcp add cc-mem --scope user -- python3 /ABS/PATH/TO/cc-mem/mcp_server.py
```

Point the DB elsewhere with `CC_MEM_DB`. Default: `~/.claude-cc-mem/memory.db`.
A given DB is tied to one embedder — the store refuses to open if the stored
vector dimension doesn't match the active embedder, so you can't silently
corrupt a semantic DB by falling back to lexical.

Verify: `claude mcp list` should show `cc-mem`, and in a session the
`memory_*` tools become available.

## Make Claude actually use it

Memory only helps if Claude runs the loop — recall at the start, save verified
learnings, and **revise when you correct it**. That guidance lives in
[`prompt/memory-loop.md`](prompt/memory-loop.md) and is installed by
`python3 setup.py prompt`. Edit that file and re-run to change the behavior.

## Tools

| Tool | Mode | Returns |
|------|------|---------|
| `memory_search(query, k, scope?, project?)` | Cold start / global | Compact briefs |
| `memory_search_neighbors(anchor_id, query, k, hops)` | Scoped / lean | Compact briefs |
| `memory_expand(node_id)` | Walk | Direct neighbors + edge kind/weight |
| `memory_get(node_id)` | Drill in | **Full** content (only fat payload) |
| `memory_insert(content, summary, label, importance, links, scope, project, sources, confidence)` | Learn | New node id |
| `memory_reinforce(src, dst, kind, delta)` | Learn the graph | edge weight |
| `memory_stats()` | Introspect | counts |

`links` is `[[other_id, kind, weight], ...]`. Edge kinds: `related`, `causal`,
`co-used`, `part-of`.

## Retrieval, briefly

- **Cold start** (`memory_search`): linear cosine scan over the whole index to
  find an entry node — the only time everything is scanned.
- **Scoped** (`memory_search_neighbors`): **spreading activation** from the
  anchor. Energy flows along edges, decaying with distance and scaled by edge
  weight, so a node reachable weakly from two directions can still light up.
  Multi-hop without a fixed hop count.
- **Scoring blend** (index.md §3.5): `α·similarity + β·activation +
  γ·importance + δ·recency − ε·hop_distance`. Weights live at the top of
  `graph_memory.py`.

## Real semantic recall (optional upgrade)

The default embedder is a dependency-free **lexical** hash (matches by shared
words/substrings). To get true semantic matching, install a real embedder and
register it before opening the store — nothing else changes:

```python
from embedder import set_embedder, EMBED_DIM
import embedder
# embedder.EMBED_DIM = 384   # if your model's dim differs, set BEFORE first insert
set_embedder(lambda text: my_model.encode(text).tolist())
```

(Mixing vector dimensions in one DB breaks cosine — pick a dim before the first
insert, or rebuild the DB if you switch.)

## Browse & manage the memory (web app)

The memory is a binary SQLite file — not human-readable on its own. The web app
gives it a UI: a **Table** (database view of every node, filter + sort), a
**Search** tab (semantic or text), and a **Graph** tab (interactive force
layout). You can **create, edit (markdown), link, and delete** memories from any
view. It's a Vite + React frontend over the same Python JSON API.

**Dev (hot-reload):**

```bash
./dev.sh                          # starts the API + Vite, installs UI deps once
# open http://localhost:5173      (Vite proxies /api to the backend on :8765)
```

**One-command / production:**

```bash
cd ui && npm install && npm run build && cd ..
CC_MEM_EMBEDDER=local .venv/bin/python manage.py      # serves UI + API on :8765
```

Launch the backend with the **same embedder the DB was built with**
(`CC_MEM_EMBEDDER=local` + the venv python for the semantic DB) so edits
re-embed in the same vector space — the dim guard will refuse to mix them.
Read-only views (Table/Graph/stats) never touch the model, so they're instant;
the model loads lazily on the first semantic search or edit.

Frontend lives in `ui/` (edit `src/styles.css`, `src/views/*`, `src/components/*`
freely); backend API in `webapp/server.py`.

## Develop

```bash
python3 test_memory.py     # 30 checks: core, scope, cross-session, MCP stdio
```

## Files

- `graph_memory.py` — SQLite store: nodes + edges + vectors, retrieval, scoring.
- `embedder.py` — pluggable embedder (default: stdlib lexical hash).
- `mcp_server.py` — hand-rolled stdio JSON-RPC MCP server (zero deps).
- `test_memory.py` — core + full MCP-over-stdio session simulation.
- `manage.py` / `webapp/` — Python JSON API + static server for the web app.
- `ui/` — Vite + React frontend (Table / Search / Graph, create/edit/delete).
- `dev.sh` — one command to run the API + Vite dev server together.
- `index.md` — the design & vision doc.

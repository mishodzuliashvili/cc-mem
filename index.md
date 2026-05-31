# Self-Learning Graph Memory for Claude Code

A design and vision document.

---

## 1. The north star

Give Claude Code a memory that works the way a mind works: not a flat log of past
chats, but a **graph of verified knowledge** that grows as it learns, organizes
itself from broad concepts down to specific facts, and is recalled by *association*
rather than by scanning everything. New sessions don't start from zero — they start
from what was learned and proven before, and they pull in only the small, relevant
neighborhood of that knowledge instead of flooding the context window.

The end state is a machine that learns without being retrained: it investigates,
verifies what it finds, writes the verified result into its own living
documentation, and gets better over time — correctable and inspectable at every
step, because the knowledge lives in a graph you can read, not in opaque weights.

---

## 2. Core model

Every **memory is a node**. Nodes are connected by **weighted, typed edges**. Each
node also carries an **embedding vector**. The graph and the vector index live in
the *same* store, so a single "learn this" operation writes the node, its vector,
and its links together — there is never a graph that's out of sync with the vector
index.

- **Node** = a unit of knowledge: a fact, a decision, a pattern, a snippet of
  documentation. Carries content, a short summary, a label, an importance score,
  access stats, and (in the full vision) provenance + confidence.
- **Edge** = a relationship between two memories. Edges have a `kind`
  (`related`, `causal`, `co-used`, `part-of`, …) and a `weight`. They're symmetric
  by default so association works in both directions.
- **Vector** = the embedding of the node's text, used for similarity search.

A node can sit at the intersection of several branches — "Continental system"
belongs to both *Campaigns* and the *Napoleonic era* — which is the whole point.
Knowledge isn't a tree; it's a web with strong and weak strands.

---

## 3. Retrieval: the heart of the system

The intelligence of this system is not in the storage — it's in *how memories are
recalled*. Two modes, and **Claude itself chooses which to use**:

### 3.1 Cold start (global)

When a session begins, Claude has no anchor — no node it's "standing on." So it
does a global similarity search over the entire vector index to find an **entry
node**. This is the "search from the start / empty node" path: the only time the
whole index is scanned. It answers the question *"where in my memory does this
task even live?"*

### 3.2 Neighbor-scoped (the lean path)

Once Claude has an anchor node, it searches **only that node's graph
neighborhood** — never the whole index. The MCP gathers the anchor's neighbors,
scores the query against just those, and returns a handful of hits. This is what
keeps the context window from filling up: the expensive filtering happens *under
the hood inside the MCP*, and Claude only ever sees a small, relevant slice.

### 3.3 Why two tools instead of one

The split is deliberate. The *model* decides strategy: cold-start to find an
entry point or to re-anchor, neighbor-scoped to walk locally. Front-loading a
whole subgraph would defeat the purpose. The default is "anchor + one hop, expand
on demand."

### 3.4 Keeping context lean — three mechanisms

1. **Compact briefs by default.** Search returns `{id, label, summary, score}`,
   never full content. Claude sees *what exists nearby* without paying full token
   cost.
2. **Full content only on demand.** A dedicated `memory_get(node_id)` is the only
   call that returns a node's complete text, and Claude calls it deliberately for
   the one node it actually needs.
3. **On-demand expansion.** Rather than loading everything up front, load the
   anchor + its immediate neighbors, and expose `expand(node_id)` so Claude pulls
   in more neighborhood only when the task calls for it. Retrieval becomes
   model-driven and incremental.

### 3.5 The scoring model

When ranking candidate nodes to load, score them with a weighted blend rather than
similarity alone:

```
score(node) =  α · similarity(node, query)
             + β · edge_strength_to_already_loaded
             + γ · importance(node)
             + δ · recency(node)
             − ε · hop_distance(node)
```

The hop-distance penalty is what stops retrieval from wandering into loosely
related corners of the graph. Greedily add nodes in score order until the token
budget is hit, then stop. (This generalizes the Stanford "Generative Agents"
retrieval score of recency + importance + relevance; the graph edges add the
structural term.)

### 3.6 Multi-resolution loading

Don't load every selected node at the same fidelity. Full content for the top
tier, one-line summaries for the middle tier, bare labels for the frontier. Claude
knows what's adjacent without paying for it, and can request more.

---

## 4. Beyond first-level neighbors

A single hop is too shallow. Recall in a mind is multi-level: think of a person →
their life → the events in it → the other people in those events → the era they
lived in. Some of those links are strong and obvious, others are weak and diverse,
but they're all reachable. The retrieval has to behave the same way.

### 4.1 Spreading activation

Instead of "search N hops," let *relevance flow* through the graph and decay with
distance and edge weight. Seed energy on the anchor node(s); it spreads outward,
strong edges passing more, weak edges less, fading as it travels. Whatever lights
up above a threshold gets loaded — whether it's 1 hop or 4 hops away. The
principled version is **Personalized PageRank** (random-walk-with-restart), which
gives every node a relevance score *relative to where you started* and is
inherently multi-hop and weight-aware.

This is also where edge weights earn their keep: strong edges are the obvious
associations, weak edges are the surprising, creative ones, and a single
activation pass uses both without committing to a fixed hop count. A node reached
weakly from *two* directions can still light up strongly, because the signals sum
— which is exactly how you reach the fact that sits at the crossroads of several
concepts.

### 4.2 Coarse-to-fine: start at the big picture, descend to specifics

Recall is hierarchical. Ask a broad question and you should land on a broad
summary, then drill into specifics only as the question narrows. Two named
techniques implement this, and they're worth knowing because they match the
intuition exactly:

- **HNSW (Hierarchical Navigable Small World)** — the structure underneath most
  vector databases (FAISS, Qdrant, Weaviate, hnswlib). Its layers *are* the
  big-picture-to-specific idea: upper layers hold a few long-range links, lower
  layers get denser and more local. You enter at the coarse level and walk down.
  This is the "small graph database" the whole design keeps reaching for.
- **GraphRAG / RAPTOR** — for the *content* hierarchy. GraphRAG clusters the graph
  into communities and writes a summary node for each, so a broad question reads
  community summaries first and only then drills into specific member nodes.
  RAPTOR recursively summarizes clusters into a tree traversed top-down. Both do
  the coarse-to-fine descent: answer "what was X's significance" from a summary
  node, follow edges down to specifics when the question gets narrow.

In the graph, summary/community nodes are *just nodes* with edges to their members
— so this is additive, not a different system.

---

## 5. The self-learning layer

This is the ambitious part: a machine that learns by investigating and only keeps
what it has proven. The right framing is a **self-maintaining knowledge base with
provenance and verification-gated writes**.

### 5.1 What gets stored

A node stores more than content. It carries **evidence**: its sources, a
**confidence** score, and a **last-verified** timestamp. Claude doesn't save
"X is true." It saves "X is true, I checked it by Y, confidence 0.8, as of today."

### 5.2 Verification-gated writes

Writing to memory is not automatic. A new claim passes through a **verification
gate** before it's persisted. For code and empirical claims the gate is strong and
cheap — the code runs and the tests pass, or the API returns what was expected, or
it doesn't. Only what clears the gate becomes a durable memory.

### 5.3 Decay and re-verification

Facts can carry a decay so that stale, high-stakes nodes get re-checked over time.
The documentation stays alive instead of rotting.

### 5.4 Contradiction handling

When a new fact conflicts with an existing node, that's an event to **reconcile**,
not to silently overwrite. The system flags the conflict, weighs evidence and
recency, and updates deliberately.

### 5.5 Reinforcement (learning the graph's shape)

Beyond learning facts, the system learns *which memories belong near each other*.
When two nodes prove useful together in a session, the edge between them is
strengthened (`reinforce`). Over many sessions the graph's topology comes to
reflect how knowledge is actually used, not just how it was first filed.

The result is what you described: living documentation the machine updates itself,
behaving more like someone who learns by investigation than by retraining.

---

## 6. Honest constraints

Two things determine whether this becomes genuinely powerful or becomes a confident
liar. Worth stating plainly:

1. **"Only save what it proves accurate" hides the whole problem in one word.**
   For *externally checkable* claims (does the code run? does the test pass?
   does the API behave?), verification is real and this vision works today — a
   coding agent can truly accumulate verified, reusable knowledge. For
   *open-world* claims ("this library is fastest," "this is best practice"),
   "proof" is soft, and the danger is the system verifying a belief against its
   own earlier beliefs and compounding an error into documented "truth." The
   strong version leans on externally-checkable claims and stays humble — keeps
   confidence and provenance — on the rest.

2. **This is memory, not training.** It changes behavior by feeding retrieved
   context to the model, not by updating weights. That's a feature: it's
   inspectable, correctable, and instantly updatable, which weight-training is
   not. But it's bounded by what can be retrieved and fit into context — which is
   precisely why the multi-hop, coarse-to-fine retrieval in §3–4 matters so much.
   **The retrieval is the intelligence.** A perfect graph with dumb retrieval
   recalls nothing useful; modest memory with smart spreading activation feels
   like understanding.

---

## 7. Architecture

### 7.1 Components

| Component | Role |
|-----------|------|
| Storage + graph + vector layer | One store (SQLite) holding nodes, edges, and embeddings. Pluggable embedder. |
| MCP server | Thin stdio JSON-RPC layer exposing the memory as tools to Claude Code. Zero pip dependencies. |
| Tests | Core behavior + a full MCP-over-stdio session simulation. |

### 7.2 Storage schema

```sql
CREATE TABLE nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    importance    REAL NOT NULL DEFAULT 1.0,
    created_at    REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count  INTEGER NOT NULL DEFAULT 0,
    embedding     BLOB NOT NULL
    -- future: sources TEXT, confidence REAL, last_verified REAL
);

CREATE TABLE edges (
    src        INTEGER NOT NULL,
    dst        INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'related',
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
```

Provenance and confidence are *three more columns* on `nodes` plus one check
before insert — the schema is built to grow into the full vision without a
rewrite.

### 7.3 Tool API (what Claude Code sees)

| Tool | Mode | Returns |
|------|------|---------|
| `memory_search(query, k)` | Cold start / global | Compact hits |
| `memory_search_neighbors(anchor_id, query, k, hops)` | Scoped / lean | Compact hits |
| `memory_expand(node_id)` | Walk | Direct neighbors + edge kind/weight |
| `memory_get(node_id)` | Drill in | **Full** content (the only fat payload) |
| `memory_insert(content, summary, label, importance, links)` | Learn | New node id |
| `memory_reinforce(src, dst, kind, delta)` | Learn the graph | ok |
| `memory_stats()` | Introspect | counts |

`links` is `[[other_id, kind, weight], …]`, so a single insert can wire the new
memory into existing knowledge.

### 7.4 Pluggable embedder

The offline default is a deterministic bag-of-tokens hash — stable, dependency-free,
good for testing the plumbing, but lexical (queries must share words). Swap in a
real embedding model/API via a single `embed_fn` hook to get semantic matching. The
graph, neighbor, and traversal logic are unchanged by the swap.

### 7.5 Scaling

Global search is a linear cosine scan — fine for thousands of nodes, and the
scoped/neighbor path barely cares because its candidate set is tiny. When the
global index grows large, replace the global scan with an ANN index (FAISS,
hnswlib, sqlite-vec). The graph and neighbor logic stay exactly the same — and an
HNSW index is, conveniently, the coarse-to-fine structure from §4.2.

---

## 8. What exists today vs. what's next

**Built and tested (working prototype):**
- SQLite store holding nodes, edges, and vectors together.
- `insert` writing node + vector + links atomically.
- Global (cold-start) search and neighbor-scoped search as separate tools.
- Compact briefs by default; full content only via `memory_get`.
- Edge reinforcement (learning the graph's shape) that persists across sessions.
- Cross-session persistence verified: a new session cold-starts, finds an entry
  node, walks neighbors, and sees edges learned in a prior session.
- A hand-rolled MCP stdio server (zero dependencies) driven end-to-end in tests.

**Next, additive (no rewrite needed):**
- Spreading activation / Personalized PageRank over the existing edges + weights.
- Weighted, multi-hop traversal that biases toward strong edges.
- Summary / community nodes for coarse-to-fine retrieval (GraphRAG-style).
- ANN index (HNSW) behind global search for scale.
- Provenance + confidence + last-verified columns.
- Verification-gated writes (strongest for code/empirically checkable claims).
- Decay + scheduled re-verification; contradiction reconciliation.
- A real semantic embedder via the `embed_fn` hook.

---

## 9. Roadmap

1. **Skeleton (done).** Two-mode retrieval, learning edges, persistence, MCP
   transport.
2. **Real recall.** Swap in a semantic embedder; add the §3.5 scoring blend.
3. **Multi-hop.** Spreading activation / PPR; weighted traversal; tune the
   decay so strong edges pull harder and weak ones still surface diverse hits.
4. **Hierarchy.** Summary/community nodes; coarse-to-fine descent; HNSW index.
5. **Verified learning.** Provenance + confidence; verification gate on writes;
   start with code/empirical claims where the gate is strong.
6. **Self-maintenance.** Decay, re-verification, contradiction reconciliation —
   the living, self-updating documentation.

---

## 10. Open design questions

- **Traversal weighting.** Should neighbor/scoped search bias toward high-weight
  edges (so reinforced links pull harder), or stay pure cosine within the
  neighborhood and let weights only decide *which* nodes count as neighbors?
- **Edge taxonomy.** Which edge kinds matter — `related`, `causal`, `co-used`,
  `part-of`? The dominant kind changes whether to weight edge-following over
  fresh similarity search at each step.
- **Verification threshold.** What confidence is the bar for a durable write, and
  what's the policy for soft, non-externally-checkable claims?
- **Forgetting.** Should low-confidence, never-re-accessed nodes decay out, and
  on what schedule?

---

*Companion files: `graph_memory.py` (store), `mcp_server.py` (MCP server),
`test_memory.py` (tests), `README.md` (run + install).*
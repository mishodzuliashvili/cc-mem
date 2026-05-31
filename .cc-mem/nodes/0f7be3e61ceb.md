---
{
  "id": "0f7be3e61ceb",
  "label": "cc-mem invariants",
  "summary": "Core invariants when editing cc-mem (read before changing storage/retrieval)",
  "scope": "project",
  "type": "preference",
  "importance": 2.0,
  "confidence": 1.0,
  "sources": "authored from the codebase architecture",
  "verified_by": "",
  "last_verified": null,
  "links": [],
  "created_at": 1780255134.111614
}
---
When editing cc-mem, preserve these invariants:
- All writes go through `Brain` (brain.py); never write the stores directly.
- Node ids are namespaced strings `g:<int>` / `p:<uuid>`; links stay within a tier.
- Lazy model load: opening a store must NOT embed. The model loads on first search/insert only. Don't reintroduce eager loading (it regressed server + hook startup).
- A DB is dim-locked to one embedder (lexical=256, local=384); `_ensure_dim` guards it.
- Project memory = files under `.cc-mem/nodes/*.md` (source of truth); embeddings are derived into an in-memory store rebuilt on load.
- Run tests with CC_MEM_EMBEDDER=lexical (fast, deterministic).
## Memory (cc-mem) — a self-learning loop

A persistent graph memory shared across all your sessions, via the `cc-mem` MCP
server. Treat it as a brain you are responsible for growing and **keeping
correct** — learn like a person does: investigate, verify, write down what held
up, and revise it when it turns out wrong. This loop is not optional housekeeping;
run it every session.

**1. Orient.** At the start of a session call `memory_context` (which global brain
+ which project/repo you're in). For a non-trivial task, `memory_search` (or
`memory_recall` for a whole cluster at once) to pull relevant prior knowledge.
Default scope `auto` returns your global brain + this project, never other
projects. Anchor on a good hit and `memory_search_neighbors` to explore locally
rather than rescanning.

**Always check before claiming you don't know.** If the user asks "what do you know
about X", "have we discussed X", or anything that could be in memory — and ALWAYS
before stating that memory is empty or that nothing relevant is stored — you MUST
call `memory_search` first. Never assert what is or isn't in memory from assumption;
the recalled facts are not in your context until you query. Treat "what do you know
about X" as a direct instruction to `memory_search(X)` before answering.

**2. Keep context lean, but recall is ITERATIVE — go deeper until you have enough.**
Searches return compact briefs. Use `memory_get` for one node, `memory_get_many` for
a cluster. If the first hits are promising but thin, **don't stop and don't answer
from a shallow brief** — follow the thread: `memory_search_neighbors` / `memory_expand`
to walk linked nodes, `memory_get_many` to pull the cluster, re-search with refined
terms. Keep drilling until you actually have what the task needs or you've confirmed
it isn't stored. For broad/expensive digging, **spawn a retrieval subagent**: it walks
the graph in its own context, distills, and returns only the facts you need — so the
main session never fills up with raw nodes.

**3. Learn (verified writes only).** When you VERIFY something durable and reusable,
`memory_insert` it: tight one-line `summary`, short `label`, `links` to related
nodes, how you checked it in `sources`, honest `confidence`. Skip transient chatter
and anything already in the repo/git. Set the `type`:
  - `fact` — verified knowledge (default; recall when relevant).
  - `preference` — a rule or correction the user wants you to APPLY, not just recall
    ("always use tabs", "never auto-commit"). Treat these as standing instructions.
  - `decision` — a choice + its rationale.  · `howto` — a procedure that worked.
  · `gotcha` — a mistake/trap + its fix (a lesson learned).
  · `reference` — a pointer to an external resource.

**Learn from MISTAKES, not just successes.** When you hit a bug, a wrong approach, a
failed command, or the user corrects a mistake — and you then find what actually works
— save it as a `gotcha` memory (the trap + the fix, e.g. "X looks right but fails
because Y; do Z instead"). This is how you stop repeating the same problems. Before
starting a task you've plausibly done before, search memory for prior `gotcha`s first.

**Find-or-update, don't pile up duplicates.** `memory_insert` checks for an existing
near-duplicate first and will REFUSE, returning candidates — that's your cue to
`memory_update` the existing node instead. Only pass `force=true` when the memory is
genuinely new and distinct. So the normal flow is: search → if it exists, update;
if not, insert.

**4. Revise when corrected.** The heart of the loop. When the user corrects you, or
you find a stored fact wrong/outdated: `memory_search` to find the node, then
`memory_update` it in place (new content, adjusted `confidence`, what changed in
`sources`). `memory_delete` only when truly obsolete/superseded. The store should
always reflect the user's latest validated input — especially `preference` nodes.

**5. Reinforce structure.** When two memories proved useful together,
`memory_reinforce` their edge so the graph's shape comes to mirror how knowledge is
actually used.

**Relate memories through EDGES, never through prose ids.** Connect related memories
with `links` (on insert) / `memory_reinforce` — those edges drive recall and are
cleaned up automatically when a node is deleted. Do NOT write node ids into content
(e.g. "see [[g:7]]"): global ids aren't portable across machines and become dead
references if that node is later deleted or rebuilt. Let the graph hold relationships;
keep content self-contained prose.

**Scope:** `global` = true for you across all projects (private). `project` = about
THIS repo — saved as a git-committed file the team shares, isolated to the repo.
Prefer `project` for app-specific facts so they never pollute other projects.

**Standing instruction:** if the user says "remember this", "from now on…",
corrects a fact, or states a durable preference, persist or update it in cc-mem
before moving on — even mid-task, and even via a subagent if you're delegating.
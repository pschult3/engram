# engram

**Give Claude Code a memory that actually works.**

> "What was the margin formula again?" "Didn't we fix that join last week?" "Which files did I change for the auth migration?"
>
> You know the answers. Claude doesn't. Every session starts from zero.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#license)

```
Without engram                          With engram
──────────────                          ──────────────
Session 1: "Analyze margins per SKU"    Session 1: "Analyze margins per SKU"
  → builds query, finds formula           → builds query, finds formula
  → discovers HSA32 is a loss-maker       → discovers HSA32 is a loss-maker

Session 2: "Continue the analysis"      Session 2: "Continue the analysis"
  → "What analysis?"                      → knows: HSA32 loss-maker, formula,
  → re-explain everything                    which files changed, what's pending
  → waste 10 minutes of context           → picks up where you left off
```

---

## Why this exists

Claude Code is great in a single session and forgets almost everything between them. The naive fix — "compile a markdown wiki and load it every session" — burns tokens fast and rots quickly. The naive fix #2 — "bolt on a vector RAG" — gets you recall but no structure, no invalidation, and still loads too much.

`engram` takes a different shape:

1. **One canonical store, three retrieval layers.** Typed memory units in SQLite, with FTS5 lexical search, optional vector similarity via `sqlite-vec`, and graph-linked expansion via rule-derived edges.
2. **Hooks own the write path.** PostToolUse appends to a JSONL queue in sub-millisecond time and never touches SQLite. PreCompact / PostCompact / SessionEnd drain the queue and persist structured summaries.
3. **MCP owns the read path.** A small, coarse tool surface (`memory_search`, `memory_get`, `memory_bootstrap`, `memory_related`, ...) plus pinnable resources.
4. **Tiny digests, never global wikis.** SessionStart injects a hard-capped capsule (default ~400 tokens, configurable per-project) of decisions / open questions / preferences / incidents — nothing else.
5. **Deterministic extraction first.** File edits, test failures, and meaningful shell commands are captured by rule-based parsers. No LLM calls in the hot path.
6. **Self-optimizing summaries.** PreCompact injects formatting rules; Claude structures its own summaries with `[DONE]`/`[DISCUSSED]` markers. A built-in feedback loop lets summary quality improve across sessions — automatically.
7. **Active invalidation, not just expiry.** When a new decision or preference overlaps an older one (shared files or tags), the older unit is superseded — not deleted — so the capsule never shows contradictory "active" entries.
8. **Secrets never reach disk.** The noise gate drops events that touch `.env`, `*.pem`, `.ssh/**`, `.aws/**`, and redacts AWS / GitHub / OpenAI / Anthropic / Slack / JWT patterns in command payloads before anything is written to the queue.
9. **Nothing leaves your machine.** Local SQLite, local queue, local search. Zero cloud dependencies.

---

## Architecture

```
Claude Code
  ├─ MCP client ─── engram mcp serve  (stdio)
  └─ Hooks ──────── engram hook ...
                       │
                       │  PostToolUse → JSONL queue (no SQLite)
                       ▼
            ~/.engram/projects/<project>/
              ├─ memory.db          (SQLite + FTS5)
              │   ├─ memory_units   typed durable knowledge
              │   ├─ memory_fts     lexical index
              │   ├─ memory_vec     vector index (optional, sqlite-vec)
              │   ├─ relations      graph edges between units
              │   ├─ events         raw append-only log
              │   ├─ sessions
              │   └─ search_log     retrieval telemetry
              └─ events.ndjson      hot-path queue (drained on read)
```

### Memory unit types

`fact` · `decision` · `preference` · `task` · `incident` · `entity_relation`
· `session_summary` · `code_change` · `open_question` · `lesson`

Each unit carries `id`, `project`, `title`, `body`, `tags`, `file_paths`,
`source_refs`, `created_at`, `valid_from`, `valid_to`, `confidence`. Code
changes auto-expire after 14 days so the store doesn't bloat.

---

## Install

`engram` is currently installed from source.

```bash
git clone https://github.com/pschult3/engram.git
cd engram

# with uv (recommended)
uv sync

# or plain pip
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

To enable vector search, install the optional extras:

```bash
pip install -e ".[vector]"   # adds sqlite-vec + fastembed (BAAI/bge-small-en-v1.5, ~30 MB, CPU)
```

Verify:

```bash
engram doctor
```

```json
{
  "version": "0.2.0",
  "home": "/Users/you/.engram",
  "project": "myrepo-3a8f1c20",
  "db_path": "/Users/you/.engram/projects/myrepo-3a8f1c20/memory.db",
  "vec_enabled": true,
  "embedder_available": true,
  "embedder_model": "BAAI/bge-small-en-v1.5",
  "units_total": 42,
  "units_embedded": 42,
  "units_pending_embed": 0,
  "relations_total": 17,
  "relations_by_type": {
    "co_occurs_in_file": 9,
    "references_entity": 6,
    "temporal_follows": 2
  }
}
```

The project key defaults to `<git-toplevel-basename>-<short-hash>`, so two
repos called `api` in different paths get distinct stores. Override with
`ENGRAM_PROJECT=...` if you need to share a store across repos.

---

## Hook into Claude Code

Inside the target repo:

### 1. Register the MCP server

Drop [templates/mcp.json](templates/mcp.json) into the repo as `.mcp.json`
(or merge with an existing one):

```json
{
  "mcpServers": {
    "engram": {
      "command": "engram",
      "args": ["mcp", "serve"],
      "env": {
        "ENGRAM_HOME": "${HOME}/.engram"
      }
    }
  }
}
```

### 2. Enable the hooks

Drop [templates/claude-settings.json](templates/claude-settings.json) into
the repo as `.claude/settings.json` (or merge):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "engram hook session-start" }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "engram hook user-prompt-submit" }] }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|NotebookEdit|Bash",
        "hooks": [{ "type": "command", "command": "engram hook post-tool-use" }]
      }
    ],
    "PreCompact": [
      { "hooks": [{ "type": "command", "command": "engram hook pre-compact" }] }
    ],
    "PostCompact": [
      { "hooks": [{ "type": "command", "command": "engram hook post-compact" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "engram hook session-end" }] }
    ]
  }
}
```

### 3. Restart Claude Code in that repo

Verify it's working:

```bash
engram bootstrap         # what would be injected at SessionStart
engram search "auth"     # what would be injected on a related prompt
engram doctor            # paths, queue depth, vec/graph state
```

---

## How a session flows

1. **SessionStart** → drain any pending events from the queue, then return a tiny capsule (active decisions, open questions, preferences, incidents).
2. **UserPromptSubmit** → if the prompt is non-trivial, drain the queue and inject the top 6 ranked memory hits (capped to ~1200 chars).
3. **PostToolUse** runs on every tool call. It only filters and appends to `events.ndjson` — no SQLite, no extraction, no I/O beyond a single line write. Targets sub-5 ms.
4. The next read-side hook drains the queue. Filters apply:
   - `Edit`/`Write`/`NotebookEdit` → `file_edit` event → `code_change` unit (14-day TTL).
   - `Bash pytest …` with a failure marker → `test_failure` event → `incident` unit.
   - `Bash git/npm/pnpm/cargo/…` → `command` event; failures become incidents.
   - `ls`, `cd`, `cat`, `grep`, `Read`, … → dropped silently.
   - New units are embedded (if vec is enabled) and linked to related units via graph edges.
5. **PreCompact** → inject compact formatting instructions as `additionalContext`. Instructs Claude to use `[DONE]`/`[DISCUSSED]` markers, stay under 800 words, and skip XML wrapping. If past `[FEEDBACK]` units exist, they are appended so Claude can self-correct summary quality over time.
6. **PostCompact** → drain, then persist Claude's `compact_summary` as a `session_summary` unit. Three guards run on the way in:
   - **Low-signal drop** — bodies under 120 chars or prompt-echo-only payloads (no `[DONE]` / `[DISCUSSED]` / `[FACT]` / `[DECISION]` / `[INCIDENT]` / `[PREFERENCE]` marker) are silently discarded instead of stored.
   - **XML sanitization** — stray `<summary>` / `<analysis>` tags are stripped before insert (defense-in-depth; Claude sometimes leaks its internal XML into compact bodies).
   - **Session idempotency** — if an active `session_summary` already exists for this `session:<id>`, its `valid_to` is set to `now` and the new row becomes the one active summary. At most one active summary per session, ever.

   If the summary contains a `[FEEDBACK]` section, it is extracted (bounded regex — stops at the next `## `/`[FACT]`/`[DECISION]`/... header so file lists never bleed in) and stored as a separate `lesson` unit (tag: `memory_feedback`) — kept out of the summary itself.
7. **SessionEnd** → drain + persist. If no `compact_summary` is available (session ended without `/compact`), falls back to reading the JSONL transcript at `transcript_path` and extracting a deterministic summary from the first and last user messages.

On drain, every new `decision` / `preference` / `open_question` runs through the supersession check: if it shares **≥ 2 file paths** or **≥ 3 tags** with an older active unit of the same type, the older unit's `valid_to` is set to the new unit's `created_at` and a `supersedes` edge is recorded. The old unit stays in the database but disappears from the bootstrap capsule and default searches.

### Self-optimizing feedback loop

engram improves its own summary quality over time through a built-in feedback loop:

```
Session N                              Session N+1
─────────                              ───────────
PreCompact                             PreCompact
  │ inject rules + past feedback         │ inject rules + UPDATED feedback
  ▼                                      ▼
Claude writes summary                  Claude writes better summary
  │ optionally appends [FEEDBACK]        │ (applies learned lessons)
  ▼                                      ▼
PostCompact                            PostCompact
  ├─ summary → session_summary           ├─ summary → session_summary
  └─ [FEEDBACK] → lesson unit            └─ ...
       │
       └──── persisted, read by ────────────┘
             next PreCompact
```

**How it works:**
- PreCompact injects formatting rules (`[DONE]`/`[DISCUSSED]` markers, word limits, no XML tags) plus any past feedback from `lesson` units tagged `memory_feedback`.
- If Claude notices that retrieved memories were unhelpful (too verbose, missing file paths, hypotheticals presented as facts), it *may* append a `[FEEDBACK]` section — but only when there's a genuine quality issue.
- PostCompact strips the feedback from the summary and stores it as a separate `lesson` unit. Next session's PreCompact reads it and adjusts instructions.
- The loop converges: after a few sessions, summaries stabilize around the quality level the user needs.

---

## Security & privacy

Engram is local-only by design — nothing leaves your machine — but the hot path still sees everything Claude Code does, so the noise gate is also the redaction layer:

**Path denylist (drops the event entirely):**
`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `.ssh/**`, `.aws/**`, `credentials`, `secrets/**`, `.netrc`, `keystore*`, `*.jks`.

**Pattern redaction (replaces the match with `[REDACTED:<kind>]`):**
AWS access keys (`AKIA…`), AWS secret lines, GitHub tokens (`gh[pousr]_…`), OpenAI (`sk-…`), Anthropic (`sk-ant-…`), Slack (`xox[bpoa]-…`), JWTs, PEM private-key headers, and generic `api_key=` / `password=` / `token=` lines with ≥16-char values.

Patterns are **hardcoded** — no user configuration — to keep the security surface small. Redaction happens *before* the event is appended to the JSONL queue, so secrets never touch disk via engram.

> **Caveat:** The regex set is conservative and pragmatic, not exhaustive. Rotate any credential that may have been pasted into a Claude Code session before engram was installed.

---

## Search and retrieval

`engram search` and `memory_search` use hybrid retrieval:

1. **FTS5** (lexical, always available) — BM25-ranked full-text search over title + body + tags.
2. **Vector similarity** (optional, requires `[vector]`) — cosine-nearest-neighbor via `sqlite-vec`. Merges with FTS5 via Reciprocal Rank Fusion (RRF).
3. **Graph expansion** — top seed results are expanded one hop along the relation graph. Directly connected units are appended with a small score penalty.

Results are re-ranked by memory type: `decision` and `incident` units outrank routine `code_change` entries for the same query.

**Per-type diversity cap.** After re-ranking, a per-type cap is applied to the final top-k. By default, `session_summary` is capped at 2 per query — meta-queries ("what did we do last session?") would otherwise flood the results with echo summaries and starve the real knowledge below them. Any type can be capped; the default table is:

| Type | Cap in top-k |
| --- | --- |
| `session_summary` | 2 |
| _(others)_ | uncapped |

The cap is applied *after* type-weight reranking, so high-scoring units still win their slots — the cap just prevents a single type from taking all of them.

### Graph edges

Edges are derived automatically when a unit is ingested:

| Relation type | Rule |
| --- | --- |
| `co_occurs_in_file` | Two units share a `file_paths` entry |
| `temporal_follows` | An `incident` within 30 min of a `code_change` on the same file |
| `references_entity` | Two units share a `tags` entry |
| `supersedes` | A newer `decision` / `preference` / `open_question` invalidated an older one |

Use `memory_related` to explore neighbours of any unit from within Claude Code.

---

## MCP surface

### Tools

| Tool | Purpose |
| --- | --- |
| `memory_bootstrap` | Tiny session-warmup capsule |
| `memory_search`    | Hybrid-ranked retrieval (FTS5 + vec + graph) |
| `memory_get`       | Full body of a single memory unit |
| `memory_list`      | List active units, optionally filtered by type |
| `memory_related`   | Graph neighbours of a unit (depth, limit) |
| `memory_log_event` | Model-facing write path (rarely needed) |
| `memory_flush`     | Force-drain the queue |

### Resources (pinnable via `@`)

| URI | Returns |
| --- | --- |
| `digest://project/current`    | The current bootstrap capsule |
| `decisions://recent`          | Up to 20 recent decisions |
| `incidents://recent`          | Up to 20 recent incidents |
| `open-questions://current`    | Current open questions |
| `memory://unit/{id}`          | Single memory unit (full JSON) |
| `relations://unit/{id}`       | Graph edges for a unit |

In Claude Code: `@engram:decisions://recent`.

---

## CLI reference

```bash
engram mcp serve                         # stdio MCP server (used by .mcp.json)
engram bootstrap [--max-tokens N]        # print the SessionStart capsule
engram search "query"                    # retrieve from the terminal
engram drain                             # process the queue manually
engram reindex [--batch N]               # embed all units missing vector embeddings
engram doctor                            # config, paths, queue depth, vec/graph state
engram config get KEY                    # read a per-project setting
engram config set KEY VALUE              # write a per-project setting
engram hook session-start                # hook handlers (read JSON on stdin)
engram hook user-prompt-submit
engram hook post-tool-use
engram hook pre-compact
engram hook post-compact
engram hook session-end
```

---

## Configuration

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENGRAM_HOME` | `~/.engram` | Where stores live |
| `ENGRAM_PROJECT` | git-toplevel basename + short hash | Project namespace |
| `ENGRAM_LOG_LEVEL` | `INFO` | Logging verbosity |
| `ENGRAM_EMBEDDER` | _(auto)_ | `stub` for deterministic test embeddings, `none` to disable, or a HuggingFace model ID |

### Per-project settings

Stored in the `meta` table of each project's `memory.db` and managed via
`engram config get/set`. Scoped to one project — use once per repo.

| Key | Default | Purpose |
| --- | --- | --- |
| `bootstrap_max_tokens` | `400` | Hard cap on the SessionStart capsule size. Greedy rendering stops once the next bullet would overflow. |

```bash
engram config set bootstrap_max_tokens 250
engram config get bootstrap_max_tokens
engram bootstrap --max-tokens 150    # one-off override, doesn't persist
```

Storage layout:

```
$ENGRAM_HOME/
  projects/
    <project>/
      memory.db       # SQLite + FTS5 + optional vec + relations
      events.ndjson   # JSONL hot-path queue
```

---

## Repository layout

```
engram/
├─ pyproject.toml
├─ src/engram/
│  ├─ config.py            # ENGRAM_HOME / project keying
│  ├─ storage/
│  │   ├─ db.py            # SQLite + FTS5 + vec + relations graph
│  │   ├─ models.py        # MemoryUnit, Relation, Event, MemoryType
│  │   └─ queue.py         # JSONL append/drain
│  ├─ ingest/
│  │   ├─ tools.py         # tool-call → event filter (the noise gate)
│  │   ├─ redact.py        # path denylist + secret-pattern redaction
│  │   ├─ extractor.py     # event → typed memory unit
│  │   ├─ transcript.py    # JSONL transcript → deterministic summary (SessionEnd fallback)
│  │   ├─ edges.py         # unit → rule-derived graph edges
│  │   ├─ supersede.py     # invalidate older units shadowed by new ones
│  │   └─ drain.py         # queue → store + embeddings + edges + supersede
│  ├─ retrieval/
│  │   ├─ embeddings.py    # fastembed / stub embedder singleton
│  │   ├─ graph.py         # BFS graph expansion
│  │   └─ search.py        # FTS5 + vec + RRF fusion + graph expand
│  ├─ digest/
│  │   └─ bootstrap.py     # SessionStart capsule renderer
│  ├─ mcp/
│  │   └─ server.py        # FastMCP stdio server (tools + resources)
│  ├─ hooks/
│  │   └─ handlers.py      # SessionStart / UserPromptSubmit / PreCompact / PostCompact / SessionEnd
│  └─ cli/
│      └─ main.py          # `engram` console script
├─ templates/
│  ├─ mcp.json
│  └─ claude-settings.json
└─ tests/
   ├─ test_storage.py
   ├─ test_queue.py
   ├─ test_extractor.py
   ├─ test_transcript.py       # transcript parser + summarize_session integration
   ├─ test_feedback_loop.py    # feedback extraction, find_by_tag, round-trip
   ├─ test_drain.py
   ├─ test_embeddings.py
   ├─ test_hybrid_search.py
   ├─ test_edges.py
   ├─ test_graph.py
   ├─ test_redact.py
   ├─ test_supersede.py
   └─ test_bootstrap_budget.py
```

---

## Development

```bash
uv sync --extra dev    # or: pip install -e ".[dev]"
pytest -q
```

The full test suite (139 tests) runs in under a second without any ML dependencies (stub embedder, no sqlite-vec required).

---

## Design notes

- **SQLite + FTS5** is the floor, not the ceiling. It's zero-setup, WAL-safe, backed up by copying one file.
- **The queue exists because PostToolUse runs on every tool call.** Cold-starting Python plus opening SQLite per call is ~80-150 ms. Multiplied by 50 edits a session, that's noticeable. Appending one JSONL line is sub-millisecond.
- **`code_change` has a TTL on purpose.** It's telemetry, not knowledge. Decisions, incidents, and lessons are forever; routine edits expire.
- **Summaries are Claude's job, structure is engram's.** PreCompact tells Claude *how* to write summaries (`[DONE]`/`[DISCUSSED]`, no XML, word limits). PostCompact strips feedback and stores it separately. Claude writes better each time without anyone touching a prompt template.
- **Supersession is conservative on purpose.** The thresholds (>= 2 shared files or >= 3 shared tags) start strict so the capsule never silently loses a still-relevant entry. Tune them down once you have enough real data to trust the signal.
- **Vector search degrades gracefully.** If `sqlite-vec` is not installed or extension loading is disabled, `memory_search` falls back to FTS5-only. No configuration change needed.
- **Graph expansion uses a 0.5x score penalty** so direct search hits always outrank graph-expanded neighbours of equal similarity.
- **SessionEnd is never empty.** Even if the user quits without `/compact`, engram reads the JSONL transcript and extracts a deterministic summary from the first and last user messages. Not as rich as a Claude-generated summary, but always better than nothing.
- **Project keys are stable across `cd`** but distinct across repos with the same name in different paths. Moving a repo creates a new namespace by design — moves often change context. Override with `ENGRAM_PROJECT` (e.g., via direnv) to share a store across a workspace with multiple repos.
- **Quality gate at the write path, not the read path.** Low-signal compact bodies (< 120 chars or prompt-echo-only) are dropped at ingest — they never reach SQLite. Filtering at read-time would be cheaper to ship but costs retrieval quality forever, because noise still gets ranked.
- **Session-summary idempotency is enforced in SQL, not Python.** `upsert_memory` runs a `json_each(source_refs)` EXISTS subquery before insert to retire any prior active summary carrying the same `session:<id>` ref. The retired row stays (audit trail via `valid_to`), but the bootstrap capsule and default search never show two summaries for one session.
- **Soft-delete over hard-delete.** Cleanup — whether automatic (supersede, idempotency) or manual (audit scripts) — sets `valid_to`. Rows are never removed. The entire supersede chain stays queryable for provenance; the active view is a window, not a state.

---

## Inspiration & related work

- [Andrej Karpathy — *"Continually-learning LLMs need a wiki, not RAG"*](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler)
- [milla-jovovich/mempalace](https://github.com/milla-jovovich/mempalace)
- [LangMem](https://langchain-ai.github.io/langgraph/), [Mem0](https://github.com/mem0ai/mem0), [Zep / Graphiti](https://github.com/getzep/graphiti), [LongMemEval](https://arxiv.org/abs/2410.10813), [HippoRAG 2](https://arxiv.org/abs/2502.14802)

---

## License

MIT

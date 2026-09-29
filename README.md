# FHA Data Governance Assistant — local-rag

A local, fully on-premises RAG assistant over Fraser Health Authority's **MetadataRepository**
database (the Data Dictionary / data-governance catalog) and a folder of internal technical
documents. It answers schema questions from a vector store, runs live read-only SQL for data
questions, answers documentation questions with citations, and holds a real conversation across
turns — all through either a terminal chat (`chat.py`) or a local browser UI (`webui.py`).

Nothing leaves the machine. The chat/reasoning model, the embedding model, the vector database,
and the SQL connection are all localhost services; there are no calls to any external API.

This document is the single source of truth for how the system works today. `AUDIT.md`,
`SERVER_MIGRATION.md`, `integration.md`, and `CHATBOT_CAPABILITIES.md` are point-in-time
working notes from earlier in the project and are largely superseded by what's described here —
kept for historical context, not as current documentation.

---

## Table of contents

1. [What it can answer](#1-what-it-can-answer)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [Setup](#3-setup)
4. [Running it](#4-running-it)
5. [Mechanism — how each piece works](#5-mechanism--how-each-piece-works)
6. [Request lifecycle by route](#6-request-lifecycle-by-route)
7. [Conversation state & context management](#7-conversation-state--context-management)
8. [Security posture](#8-security-posture)
9. [Known limitations & honest gaps](#9-known-limitations--honest-gaps)
10. [Testing](#10-testing)
11. [Troubleshooting / restart guide](#11-troubleshooting--restart-guide)
12. [Configuration reference](#12-configuration-reference)
13. [Project history](#13-project-history)
14. [Server migration & roadmap](#14-server-migration--roadmap)
15. [File reference](#15-file-reference)

---

## 1. What it can answer

**Database structure** (from the vector store — no live query needed): what columns a table
has, its data types/nullability/identity flags, what schemas and tables exist, what a table's
description says.

**Live data** (a generated, validated, executed SQL query): counts, filters, group-bys,
averages, percentages, top-N/bottom-N, and now — with foreign-key metadata loaded — real
multi-table joins for questions like *"which systems have the most assets?"*.

**Internal technical documentation**: PDFs, Word docs, Excel files, and Markdown in `docs/` —
API/workflow engine internals, SQL Server tuning guides, encryption utilities, team backlog —
answered with source citations.

**Conversation**: follow-ups ("how about consistency_runs?", "which of those are nullable?"),
topic switching, and mixed structured+doc questions in one turn ("both" route).

It is **strictly read-only**. It cannot and will not modify, insert, or delete anything, in the
database or anywhere else — enforced by an allowlist validator (§8), not just prompting.

---

## 2. Architecture at a glance

```
                    ┌──────────────┐        ┌───────────────────┐
   Browser  ───────▶│  webui.py    │        │  chat.py (CLI)     │
   (SSE)            │  stdlib HTTP │        │  Rich console loop │
                    └──────┬───────┘        └─────────┬─────────┘
                           └──────────────┬────────────┘
                                          ▼
                                   chat.ask()  (chat.py)
                                          │
                              ┌───────────┼────────────┐
                              ▼           ▼             ▼
                        router.route  retriever.*   sql_generator.*
                        (llm.py)      (Qdrant +     (llm.py + pyodbc
                                       lexical.py)   + planner.py)
                              │           │             │
                              ▼           ▼             ▼
                     ┌─────────────┐┌───────────┐┌──────────────┐
                     │ llm.py      ││  Qdrant   ││ SQL Server   │
                     │ (reasoning) ││ (vectors) ││ (read-only)  │
                     └─────────────┘└───────────┘└──────────────┘
                              │
                     LM Studio / Ollama /v1 / vLLM
                     (any OpenAI-compatible localhost server)

   Embeddings (query + ingest) always go to Ollama + nomic-embed-text,
   independent of whichever server is serving the chat model.
```

**Five modules do essentially all the thinking:**

| Module | Job |
|---|---|
| `router.py` | classify each question into one of 5 routes |
| `retriever.py` | Qdrant search (dense + optional hybrid BM25/RRF) over the Data Dictionary and documents, plus several non-vector "bypass" paths for exhaustive/enumeration questions |
| `sql_generator.py` | build schema context (incl. real foreign keys), generate T-SQL, validate, execute, answer |
| `planner.py` | decide whether a SQL question is answerable directly or needs schema-wide enumeration first; runs the deterministic enumerate pipeline |
| `chat.py` | ties everything together per turn; owns conversation state and the two system prompts |

Everything that talks to a model goes through **`llm.py`**, a small backend-agnostic client — see
§5.2. Nothing else imports `ollama` or any HTTP client directly for chat.

---

## 3. Setup

### Prerequisites

- Python 3.10+ (developed against 3.12)
- **Qdrant** binary running locally (not Docker in this setup — a standalone `qdrant.exe`/binary
  with its own `storage/` directory)
- **A local OpenAI-compatible chat server** — LM Studio (recommended for GGUF models), or
  Ollama's `/v1` endpoint, or vLLM
- **Ollama** running locally, for embeddings only (`nomic-embed-text`)
- ODBC Driver 17 or 18 for SQL Server
- Network/VPN access to the `MetadataRepository` SQL Server instance

> **Windows note:** keep `QDRANT_HOST` and `OLLAMA_HOST` on `127.0.0.1`, not `localhost`.
> Windows resolves `localhost` to IPv6 `::1` first and stalls ~2s per call before falling back
> to IPv4 — `config.py` prints a warning at import time if either is set to `localhost`.

### Install

```bash
python -m venv .venv
.venv\Scripts\activate                # Windows
pip install -r requirements.txt
pip install httpx                     # used directly by llm.py — see note below
```

> `requirements.txt` does not currently list `httpx` explicitly, even though `llm.py` imports it
> directly (it happens to already be installed as a transitive dependency of `qdrant-client` in
> most environments, but that isn't guaranteed). Install it explicitly, or add it to
> `requirements.txt` yourself, until that's cleaned up.

### Pull the models

Embeddings (always required, regardless of which chat server you use):

```bash
ollama pull nomic-embed-text
```

Chat model — pick one path:

- **LM Studio**: download a GGUF build of your chosen model (this project currently runs
  `qwen/qwen3.8-27b`, Q6_K quant) through the LM Studio app, then start its local server
  (Developer tab → Start Server, default port `1234`).
- **Ollama**: `ollama pull llama3.1:8b` (or any other chat model) — Ollama exposes an
  OpenAI-compatible `/v1` endpoint on the same port as its native API (`11434`).

### Configure

```bash
copy .env.example .env
```

Edit `.env` — at minimum set `DB_SERVER`. Everything else has a sensible localhost default.
See §12 for the full variable reference. `config.py` raises loudly at import time if `DB_SERVER`
is unset — it will not silently fall back to `localhost`.

### Ingest

Two independent ingestion pipelines, into two Qdrant collections:

```bash
python ingest.py          # SQL Server DataDictionary -> "data_dictionary" collection
python ingest_docs.py     # docs/ folder -> "documents" collection
```

`ingest.py` does a **full rebuild** every run (`recreate_collection`) — safe to re-run any time
the DataDictionary changes, but it always re-embeds everything from scratch.

`ingest_docs.py` is **incremental** — it tracks file state in `docs/.registry.json` (new /
modified / unchanged / deleted) and only re-processes what changed. Flags: `--force` (re-ingest
everything), `--delete <path>` (remove one file's chunks).

---

## 4. Running it

### Bring the stack up (in order)

1. **Qdrant** — start the binary; wait for `http://127.0.0.1:6333/collections` to respond.
2. **Ollama** — usually already running as a service; embeddings only.
3. **Chat server** — LM Studio: `lms server start`, then load the model:
   ```bash
   lms load qwen/qwen3.8-27b -y --ttl 21600
   ```
   `--ttl` (seconds) controls how long the model stays loaded when idle — LM Studio **unloads
   the model automatically** after this window, which is the #1 cause of "it's just hanging"
   (`lms ps` shows *"No models are currently loaded"* when this happens; `/v1/models` will
   still *list* the model even when it's unloaded, so that check alone is misleading).
4. **VPN**, if `SPDBSRES117` / your SQL Server host needs it — required for the `sql` route.
5. **The app** — either:
   ```bash
   python chat.py            # terminal
   python webui.py           # browser, http://127.0.0.1:8080
   ```

### Quick health check

```bash
curl http://127.0.0.1:8080/state     # (webui.py) model, effort, online status, in one shot
```
Or check each port individually: **8080** web UI, **1234** LM Studio, **6333** Qdrant,
**11434** Ollama, **1433** SQL Server (default).

### LM Studio: set concurrency to 1

By default LM Studio's **"Max Concurrent Predictions"** is 4, which splits the model's context
window across 4 parallel slots — so a single conversation effectively gets only ~1/4 of the
configured context length (e.g. 16,384 ÷ 4 ≈ 4,096 tokens), and the KV cache is reused worse
between turns. For a single-user local setup, set it to **1** in the model's load settings
(**Context and Performance → Max Concurrent Predictions**) and reload the model — this is a
per-model saved setting in LM Studio, so it persists across future loads. Verify with:
```bash
lms ps      # PARALLEL column should read 1
```

### CLI (`chat.py`)

At startup you're asked to pick a **reasoning effort** (`low` / `medium` / `xhigh` — see §5.2);
Enter accepts the default. Then it's a normal chat loop.

| Command | Effect |
|---|---|
| `/sources` | re-run the last question, showing the rewritten query and retrieved chunks |
| `/effort` | show the current reasoning effort |
| `/effort medium` | change it mid-session |
| `/reset` | clear conversation history and start fresh |
| `/help` | show commands |
| `/quit` | exit |

Non-interactive effort selection: `python chat.py --effort medium`, or set `REASONING_EFFORT`
in `.env` (either skips the startup prompt).

### Web UI (`webui.py`)

```bash
python webui.py
```
Open `http://127.0.0.1:8080`. FHA-branded (`#1e4474` navy header, `#005293` blue / `#e35205`
orange accents), streams tokens live via Server-Sent Events, renders markdown (bold, lists,
inline/fenced code, tables, headers, blockquotes), shows a live `working… m:ss` indicator while
generating, has a **Thinking** dropdown wired to the same reasoning-effort mechanism as the CLI,
and a collapsible **view SQL** panel under any `sql`-route answer.

It's a single stdlib HTTP server — no framework, no build step, no CDN or external asset. Binds
`127.0.0.1` only by default (see §8 for what changes if you expose it further). It holds **one
global `Session`** (history, topic table, last SQL, etc.) — fine for one person driving it, but
two people using it at once would share and clobber each other's conversation; per-browser
sessions haven't been built (not needed yet — see §9).

---

## 5. Mechanism — how each piece works

### 5.1 `config.py` — settings hub

Loads `.env` via `python-dotenv`. Everything else imports `config` rather than reading
environment variables directly. Key groups: DB connection (§8), Qdrant host/ports, the model
triple (`REASON_*` / `EMBED_MODEL` / `OLLAMA_HOST`), retrieval tuning (`TOP_K`, `DOCS_TOP_K`,
`HISTORY_TURNS`), the hybrid-search flags (§5.4), clearance/restricted-doc patterns (§8), and
document-chunking parameters. Two startup-time guards: raises if `DB_SERVER` is unset; prints a
warning if any host is literally `"localhost"` (the Windows IPv6 stall).

`CHAT_MODEL` and `ROUTER_MODEL` are back-compat aliases that both just equal `REASON_MODEL` —
kept so the gitignored test suites and any older call sites that still read those names work
unmodified.

### 5.2 `llm.py` — the model backend

Everything that needs the reasoning/chat model calls **`llm.reason()`** (one-shot) or
**`llm.reason_stream()`** (token-by-token generator); everything that needs an embedding calls
**`llm.embed()`**. This is the only file with a hard dependency on the model transport, which is
what makes the model swap-able by editing `.env` alone.

- **Transport**: plain `httpx` POSTs to `{REASON_BASE_URL}/chat/completions` — the OpenAI
  chat-completions HTTP shape, which LM Studio, Ollama's `/v1`, and vLLM all speak. No `openai`
  package is used (deliberate choice — see §13); everything stays on localhost.
- **Reasoning effort**: a session-level knob (`low` / `medium` / `xhigh`) sent as
  `reasoning_effort` in the request body. `set_reasoning_effort()` / `get_reasoning_effort()`
  hold it as module state; `chat.py`'s `/effort` command and `webui.py`'s dropdown both call
  through to these. Routing calls always pass `effort="low"` explicitly, overriding whatever the
  session is set to — see the `<think>`-budget issue below for why.
- **`<think>` handling**: Qwen3.8 (and similar "thinking" models) can emit a reasoning trace
  either as a separate `reasoning_content` field or inline as `<think>...</think>` tags,
  depending on the server. `reason()` strips `<think>` blocks from the final text via regex.
  `reason_stream()` uses a small stateful `_ThinkFilter` that buffers and suppresses `<think>`
  spans **across streamed chunk boundaries** (a tag can be split token-by-token mid-stream, so a
  naive per-chunk regex would miss it).
- **`REASONING_HEADROOM`**: a real bug this surfaced — **reasoning tokens are billed against
  `max_tokens`**. The router originally capped generation at ~16 tokens (just enough for a
  one-word label); with a thinking model, the entire budget was consumed by the reasoning trace
  and `content` came back **empty**, which silently made the router fall back to `"both"` on
  every single question. Fixed by padding every `max_tokens` request with
  `config.REASONING_HEADROOM` (default 1024) extra tokens reserved for thinking.
- **Graceful degradation**: if the server rejects the `reasoning_effort` parameter (HTTP 400 —
  e.g. a non-thinking model or an older server), both `reason()` and `reason_stream()` retry once
  with that field stripped. This is what lets the exact same code run unmodified against Ollama's
  8B (no thinking support) and LM Studio's Qwen3.8 (thinking support).
- **`health()`**: hits `{REASON_BASE_URL}/models`; used by `chat.py`'s startup banner and
  `webui.py`'s `/state` endpoint.

### 5.3 `router.py` — five-way classification

Classifies every question into exactly one of: `structured`, `unstructured`, `both`, `general`,
`sql`. Two layers:

1. **A deterministic pre-check**, before any LLM call: `planner.is_description_audit_question()`
   catches catalog-wide "which tables/columns are missing descriptions" questions and routes
   straight to `sql` — these need live data the vector store doesn't have (ingest excludes rows
   with no description), so no classifier decision is needed or wanted here.
2. **An LLM classification** (`llm.reason`, `effort="low"`, `max_tokens=16`) against a detailed
   system prompt plus ~40 worked examples covering the genuinely ambiguous cases: "columns" vs
   "rows" questions, dot-notation that could mean live SQL or documentation, named-person
   questions (always `unstructured`, answered from the team backlog doc), and — added this
   session — governance-entity data questions (system inventories, ownership/lineage coverage,
   percentage/completeness figures) that must land on `sql`, not `structured`.

A **deterministic floor** runs after the LLM call: if the classifier said `general` but the
question leans on a bare pronoun ("those", "that", "it"...) *and* the previous turn was an active
data route, the floor overrides back to that previous route. This exists because `general` in
`chat.py` skips retrieval and topic tracking entirely — losing a genuine follow-up to a
misclassified "general" silently breaks the conversation. It's deliberately narrow (pronoun
required) so real greetings ("hello", "thanks!") are never touched by it. An earlier version
tried to fix this by feeding "this is a follow-up" into the classifier prompt itself; that
dragged real greetings into the prior data route, so it was replaced with this narrow
post-hoc rule.

### 5.4 `retriever.py` — retrieval

Two Qdrant collections (`data_dictionary`, `documents`), one client (gRPC preferred). Everything
here is gated by a **deny-by-default clearance filter** — see §8.

**Dense search** (`retrieve()` / `retrieve_docs()`): rewrite the query (resolve pronouns against
the last useful reply and the tracked topic table) → `llm.embed()` → Qdrant cosine search.

**Hybrid search (BM25 + dense, via Reciprocal Rank Fusion)** — `lexical.py` implements a
zero-dependency Okapi BM25 index (no `rank_bm25`/torch pulled in; the corpus is small enough
that a pure-Python in-memory index is instant, tokenizing on `[a-z0-9_]+` runs so underscored
identifiers like `AES_KEY_BASE64` survive as one term). `_rrf_fuse()` combines the dense ranking
and the BM25 ranking by summing `1/(k + rank)` per ranker. **Enabled by default for the
documents collection** (`HYBRID_SEARCH=1`) — it recovers exact technical tokens embeddings blur
(config keys, error codes, dotted API names). **Deliberately disabled by default for the
structured Data Dictionary path** (`HYBRID_STRUCTURED=0`): those chunks are short per-column
rows, so BM25 over generic tokens ("scan", "jobs", "status") boosted near-tie sibling tables —
measured to flip 1 of 18 SQL grounding questions to the *wrong* table (`stg.ScanControl` instead
of `dm_dq.scan_queue`) before this was scoped off. The structured path already grounds correctly
via dense search plus name-matching (below), so hybrid there was a net regression, not a win.

**Several non-vector "bypass" paths**, each added because plain top-k dense search gave a wrong
or incomplete answer for a specific question shape:

- `fetch_all_columns()` — a full Qdrant *scroll* (not search) filtered by exact schema+table,
  sorted by `column_order`. Used whenever the question needs **every** column of a known table
  (list-columns questions, comparisons) — top-k search was returning a partial, arbitrary subset.
- `resolve_list_tables_question()` — for "what tables are in schema X": an exhaustive scroll
  over the schema, not a vector search, which was returning an undercount.
- `all_tables_catalog()` / `format_catalog_context()` — for "which tables support X" theme
  questions: hands the model the **complete** table catalog (schema + description for all ~44
  tables) rather than a partial top-k slice, because partial search was missing thematically
  relevant tables whose description doesn't share the question's exact wording.
- `resolve_bare_table_name()` — resolves a schema-less table name mentioned in a question (e.g.
  "consistency_runs" without "dm_dq.") against every known table name, guarding against generic
  words like "columns"/"table" false-matching via `_GENERIC_STRUCTURE_WORDS`.
- `_TABLE_DISCOVERY_RE` — distinguishes "what/which **table(s)** ..." (asking to *identify* a
  table — should reach the catalog/theme path) from "what **columns** does X have" (asking to
  *list columns*) even when the former happens to contain a column-ish word like "fields" ("what
  table maps glossary terms to assets or fields" was previously misrouted into the column-listing
  branch by that stray word).

**Name-match grounding** (used by `sql_generator.get_schema_context`, not here directly, but the
plural-handling logic lives conceptually alongside retrieval): when a question names a table
that vector search under- or mis-ranks, normalizing both the question and candidate table names
(strip non-alphanumerics, handle `s`/`es`/`ies` plurals) and checking substring containment lets
the named table be pinned as PRIMARY regardless of its vector rank. The plural handling was
originally singular-`s`-only, which missed `es`/`ies` plurals (`"scanbatch"` didn't match
`ScanBatches`) — a real near-tie bug (see §13) fixed by widening the match to try `-es`/`-ies`
suffix forms too.

### 5.5 `sql_generator.py` — the SQL path

`get_schema_context()` decides what schema information the model sees, in priority order:

1. **DataDictionary catalog questions** (`planner.is_catalog_question` /
   `is_description_audit_question`) get a **hand-authored, pinned schema** for
   `[rpt].[DataDictionary]` (`_DATADICTIONARY_SCHEMA`) instead of vector search — retrieval
   reliably fails to surface this table (its own chunks don't embed near phrases like "data
   type" or "identity column", so semantically-adjacent-but-wrong tables outrank it).
2. **Follow-up questions** (vague pronoun + a `last_sql` from the previous turn): extract the
   table from the previous SQL and fetch **all** its columns directly, bypassing search entirely,
   so a follow-up always sees the full schema of the right table.
3. **Standard retrieval + name-match grounding**: vector search for candidate tables; if the
   question names one of them (by the plural-aware match described in §5.4), elevate it to
   **PRIMARY TABLE** with its complete column list; otherwise present all retrieved tables as
   unranked candidates and let the model choose. This asymmetry (full schema for the named table,
   partial for the rest) was a deliberate fix — expanding *every* candidate to full schema let
   the model rationalize a wrong near-tie table just as easily as the right one.
4. **When top-k retrieval collapses to fewer than 3 distinct tables**, admit a table the
   question *names* but retrieval missed entirely (see foreign-key joins below).

**Foreign keys** (`foreign_keys()`): queries `sys.foreign_key_columns` once per process (cached),
building a `{(schema, table): [join predicates]}` map covering both directions of every FK edge.
Every table block rendered into the schema context includes its real join predicates, verbatim,
under a `Joins (foreign keys)` heading. The SQL prompt instructs the model to use these
predicates **exactly as given**, chain them for multi-hop paths, and — critically — to answer
`INSUFFICIENT_SCHEMA` rather than invent a join condition that isn't listed. Before this, the
prompt's rule was simply "don't JOIN unless the question requires it," which the model followed
by guessing join keys from column-name similarity alone, with no way to know a real relationship
existed. Degrades gracefully to an empty map if the database is unreachable (logs a warning,
schema context still renders, joins just aren't offered). The database has 24 FKs across 18
tables — concentrated in the `mdm` schema (Systems/Assets/Columns/LineageMappings/Processes/...);
the `dq` schema (Rules/Results/RuleTargets/Executions) has none defined, so joins there still
rely on column-name matching.

**The SQL system prompt** (`_SQL_SYSTEM`) accumulated a series of narrow, evidence-driven rules
over the session, each added after a specific observed failure (see §13 for the concrete
before/after on each): always fully-qualify table names; choose one table from PRIMARY/CANDIDATE
correctly; a "how many" question must return `COUNT(*)`/`SUM(...)`, never raw rows, and a
grouped count puts the group column first (not the count); a "which X and what category" question
must `GROUP BY` the category rather than dump matching rows; any percentage/ratio/"how complete"
figure must cast to float (`100.0 * ...`) to avoid T-SQL integer-division truncation to 0; `TOP`
is for raw-row answers only, never alongside an aggregate; a superlative ("most recent",
"lowest") **must** pair `TOP` with `ORDER BY` in the right direction; ordering ascending on a
nullable column must exclude NULLs (they sort first in T-SQL, so an unguarded `ORDER BY x ASC`
can return an all-NULL "top 5"); a "how many X are failed/active/etc." question must include the
corresponding `WHERE`, never return unfiltered rows; joins must come from the listed FK
predicates only.

**Deterministic post-generation guards** — cheaper and more reliable than adding yet another
prompt rule for a narrow defect class:

- `_top_without_order_by()` + `repair_missing_order_by()`: if the question has ordering intent
  (regex `_ORDERING_INTENT_RE` — "most recent", "highest", "top N", etc.) and the generated SQL
  has `TOP` but no `ORDER BY`, ask the model for a single targeted repair; the repair is only
  accepted if it validates *and* actually contains an `ORDER BY` — otherwise the original SQL is
  kept, so this guard can only fix things, never make them worse.
- `_guard_null_order()`: if the question asks for the lowest/worst/minimum and the SQL orders
  ascending on some column without a `col IS NOT NULL` guard, injects one deterministically (a
  regex splice, no LLM call) — skipped if the query has `GROUP BY`/`HAVING` (different shape,
  not safe to splice blindly), and validated before being accepted.

**`validate_sql()`** — an **allowlist**, not a blocklist: must be a single statement (rejects
anything after a `;`), must start with `SELECT` or `WITH ... SELECT`, and must not match
`_FORBIDDEN` (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `EXEC`, `xp_`/`sp_` procedures,
`INTO`, `WAITFOR`, `OPENQUERY`/`OPENROWSET`/`OPENDATASOURCE`, etc.). This is the actual security
boundary for what SQL is allowed to run — see §8 for what backs it up (or doesn't) at the
connection level.

**`execute_sql()`** connects via `pyodbc` with `ApplicationIntent=ReadOnly` (required — the DB is
an AlwaysOn availability-group secondary that rejects connections without this hint; it is a
*routing* hint, not a permission), caps results at 100 rows, 30-second timeout.

**`run_sql_pipeline()`** is the full sequence: `planner.resolve_scope` (direct vs. enumerate) →
schema context → generate SQL → validate → the two deterministic guards → execute (with one
self-correction attempt via `repair_sql()` if execution throws) → format results → generate a
natural-language answer.

### 5.6 `planner.py` — scope resolution + the enumerate path

Historical note: this module's docstring still describes an older design (an LLM-driven
plan/execute loop with a tool whitelist and a step cap) that has been superseded — the actual
fan-out planner and complexity classifier described there were stripped as dead code (see §13,
audit finding #9). What remains and is live in production is much smaller:

- **`is_catalog_question()`** / **`is_description_audit_question()`** — the detectors `router.py`
  and `sql_generator.py` both call to identify DataDictionary-catalog territory.
- **`resolve_scope()`** — a fully deterministic (no LLM) gate deciding whether a SQL question is
  answerable directly against identifiable table(s), or genuinely needs to *discover* a table set
  first (schema-wide "across all tables in X" questions). Precedence: catalog questions → direct;
  an explicit enumerate cue (`_ENUMERATE_CUE_RE`) *and* no "columns" wording *and* a resolvable
  schema name → enumerate; everything else → direct. Kept deterministic specifically so that
  aggregate phrasing ("how many... per...") can never accidentally misroute into the
  (much slower, LLM-free-but-multi-query) enumerate path.
- **`run_enumerate_pipeline()`** — for the enumerate branch: list the schema's user tables from
  `sys.tables` (live catalog query, no LLM), build **one** `UNION ALL` row-count query across all
  of them in Python (table names come from the catalog, never from the LLM — no SQL is
  LLM-authored on this path), execute it, answer. Only `_tool_list_tables` from the older
  tool-whitelist design is actually still called, by this pipeline.

### 5.7 `chat.py` — orchestration

`ask()` is the single entry point both `chat.py`'s CLI loop and `webui.py` call. Per turn:

1. Look back up to 3 prior assistant replies for one that mentions a table/column
   (`last_useful_reply`) — used by query rewriting for pronoun resolution.
2. `router.route()` — get the label.
3. Update `topic_table`: if the question names a `schema.table` directly, use it; otherwise, on a
   data route, try `retriever.resolve_bare_table_name()`.
4. **`general`** → no retrieval at all; short system prompt; stream from `llm.reason_stream`.
5. **`sql`** → `sql_generator.run_sql_pipeline()`; prints the generated SQL and a results table
   (via `rich`) in the CLI; the SQL and row count get folded into what's stored in history so a
   later follow-up can reference "those" results.
6. **`structured` / `both` / `unstructured`** → the retrieval branch: a chain of increasingly
   specific checks (schema-listing → multi-table comparison → full-column-list → theme/catalog
   discovery → plain vector search) picks which of §5.4's paths applies, in that precedence
   order, then streams the answer against the full `SYSTEM_PROMPT` (which forbids inferring
   constraints/PKs/FKs not explicitly in the retrieved context, requires citing document sources,
   and requires evaluating multi-condition questions entry-by-entry rather than guessing).

Two system prompts: `SYSTEM_PROMPT` (retrieval-grounded, strict — 9 numbered rules) and
`GENERAL_SYSTEM_PROMPT` (short, conversational, explicitly told not to fabricate answers that
need real data).

---

## 6. Request lifecycle by route

| Route | What runs | Example |
|---|---|---|
| `general` | router only, then a short chat completion, no retrieval | "hello", "thanks!" |
| `structured` | router → one of retriever.py's bypass/search paths → answer | "what columns does scan_queue have?" |
| `unstructured` | router → `retrieve_docs` (hybrid) → answer with citations | "what is the API Engine?" |
| `both` | router → structured path + docs path, context concatenated → one answer | "which tables store employee data and what's the leave entitlement?" |
| `sql` | router → `planner.resolve_scope` → schema context (+ FKs) → generate → validate → guards → execute → answer | "how many active rules are there?" |

---

## 7. Conversation state & context management

Five pieces of state are threaded through every turn (owned by the caller — `chat.main()`'s
locals for the CLI, `webui.Session` for the browser): `history`, `topic_table`, `last_sql`,
`last_intent`, `last_route`.

- **`topic_table`** — the table the conversation is currently "about"; carries across turns so a
  bare follow-up ("which of those are nullable?") resolves against the right table without
  re-naming it.
- **`last_sql`** — the previous turn's executed SQL; lets a SQL follow-up ("how many of those
  failed?") anchor to the same table via §5.5's follow-up bypass.
- **`last_intent`** — currently only meaningfully tracks `"list_columns"`, so a bare topic switch
  ("how about the scan_queue?", no "columns" in it) still returns the full column list if the
  conversation was already in a list-columns context.

**History is capped on what's *sent*, not on what's *retained*.** `config.HISTORY_TURNS`
(default 20) limits how many past messages get replayed into the prompt each turn; the caller's
own `history` list is never truncated, so the full transcript is always available (e.g. for
`/sources`, or a future export). This distinction mattered: retrieved context (schema blocks,
document chunks) used to be **stored permanently in history** as part of each user message. On a
structured turn that's ~600–1,400 tokens of retrieved context that got silently re-sent on every
later turn, whether or not it was still relevant — a conversation would visibly slow down turn
over turn as history grew (measured: a simple "hello" went from ~2 minutes to over 8 minutes
after a few structured questions in the same session, purely from prompt-size growth). Fixed by
sending the current turn's retrieved context **only** in that turn's message and storing just the
bare question in history afterward — history now grows by tens of tokens per turn instead of over
a thousand. Verified: a fresh "hello" after several structured turns dropped from 8+ minutes back
to the ~2-minute baseline, and a 3-turn topic-switch-then-pronoun-follow-up chain
(`scan_queue` → `consistency_runs` → "which of those are nullable?") still resolved correctly,
confirming continuity rides on the structured state above, not on the discarded context blobs.

---

## 8. Security posture

**What's implemented:**

- **Deny-by-default Qdrant clearance filter** — every ingested point is tagged `clearance:
  "general"` or `"restricted"` at ingest time (`ingest_docs.py` auto-classifies by filename
  against `RESTRICTED_DOC_PATTERNS`, e.g. HR data / leave policy). Every retrieval call applies a
  `MatchAny` filter against the caller's allowed clearance set; a point with **no** clearance
  label matches nothing (fails closed, not open). `config.DEFAULT_CLEARANCE = ("general",)` is
  what the CLI and web UI both currently pass as the caller's identity — there is no real
  per-user identity yet, so this is effectively one shared app-level clearance, not RBAC. The
  restricted-content path is exercised in code but currently dormant in practice (nothing in the
  live `docs/` corpus is tagged restricted at the moment).
- **SQL allowlist validation** (`sql_generator.validate_sql`) — single-statement, `SELECT`/`WITH`
  only, explicit forbidden-construct blocklist as a second layer (`INSERT`/`UPDATE`/`DELETE`/
  `DROP`/`EXEC`/`xp_`/`sp_`/`INTO`/`WAITFOR`/`OPENQUERY`/etc.).
- **Read-only SQL login, opt-in** — `sql/create_readonly_login.sql` creates a `rag_readonly`
  SQL login with `db_datareader` only. If `DB_READONLY_USER`/`DB_READONLY_PASSWORD` are set in
  `.env`, `execute_sql()` connects as that login. **If they are not set** (the current default),
  it falls back to the service's own Windows identity via `Trusted_Connection=yes` — a warning is
  logged (not printed to console, to avoid interleaving with streamed answers) the first time
  this happens. In that fallback mode, `validate_sql()`'s allowlist is the *only* barrier between
  a generated query and whatever that Windows identity can do.
- **`ApplicationIntent=ReadOnly`** on every SQL connection — a routing hint for the AlwaysOn
  availability group, **not** a permission boundary; don't rely on it for enforcement.

**What's explicitly not implemented (known, not accidental):**

- No authentication on the web UI, no per-user identity, no session isolation between users.
- No TLS anywhere in this local setup.
- No rate limiting on `webui.py` (stdlib `ThreadingHTTPServer` has none built in).
- No audit log of who asked what (the `Session`/history exists only in-process, per run).
- If you expose `webui.py` beyond `127.0.0.1` (LAN/VPN), all of the above becomes a real exposure
  — this was deliberately deferred rather than half-built, since Teams (§14) will eventually
  supply real per-user identity for free and any auth built into the web UI now would likely be
  thrown away. Do not bind `webui.py` to `0.0.0.0` without first setting up `DB_READONLY_USER`.

---

## 9. Known limitations & honest gaps

- **`dq` schema has no foreign keys defined** — joins across `Rules`/`Results`/`RuleTargets`/
  `Executions` still rely on the model matching column names (`RuleId` in both tables), which
  works when naming is consistent but has no fallback when it isn't. The FK feature (§5.5) only
  helps where FKs actually exist, which today is concentrated in `mdm`.
- **Multi-hop joins need every intermediate table to already be in the retrieved context** — the
  FK predicates for a table are only rendered when that table itself made it into the schema
  context. A question needing `Systems → Assets → LineageMappings` will only see the FKs for
  whichever of those tables retrieval actually surfaced; if the bridge table is missing, the
  model correctly declines (`INSUFFICIENT_SCHEMA`) rather than guessing — a genuine "I don't have
  enough information" rather than a bug, but it does mean some 3+ table join questions still fail.
- **Underspecified questions correctly decline rather than guess** — e.g. "how complete is the
  metadata repository?" has no single defined "completeness" metric anywhere in the schema, so
  the model declines rather than picking an arbitrary one. This is deliberate grounding behavior,
  not a bug, but it means some vague governance questions will get "I don't have enough
  information" even though *a* reasonable numeric answer could technically be computed.
- **`webui.py` has one global session** — fine for one person; concurrent users would share and
  overwrite each other's conversation state. Not built out because the near-term plan is to
  demo/share only from one machine at a time (screen share, or LAN sharing with everyone
  understanding it's a shared session), not to serve multiple simultaneous users locally.
- **LM Studio's model TTL will silently unload the model** — if a turn hangs indefinitely, check
  `lms ps` before assuming something is broken.
- **Hardware-dependent latency** — on an 8GB laptop GPU hosting a 23GB model (mostly CPU
  offload), single-turn latency is commonly 2–14 minutes. This is a hardware ceiling, not a bug
  in the pipeline; the server migration (§14) is what actually fixes it.
- **`requirements.txt` doesn't list `httpx`** even though `llm.py` imports it directly (see §3).
- **Ground truth in the (gitignored) test fixtures drifts** — `test_sql.py`/`test_sql_hard.py`
  hardcode expected row counts against live, growing tables; they need periodic manual refresh
  against the current DB or they report false failures. A self-validating version (compute
  expected values from the DB at test run time instead of hardcoding them) was proposed but not
  built — deferred as lower priority than functional fixes.
- **Qwen3.8-27B markdown formatting** — the model tends to format answers with bold/headers/
  tables even in the terminal, where `chat.py` just prints raw tokens (so you'll see literal
  `**`/`##`/`|` in the CLI). `webui.py` renders it properly; the CLI does not.

---

## 10. Testing

| File | Tracked in git? | Covers |
|---|---|---|
| `test_sql.py` | **No** (gitignored) | 20-question SQL generation + execution suite against known ground truth; checks both SQL-fragment shape and actual result correctness |
| `test_sql_hard.py` | **No** (gitignored) | harder SQL patterns: DataDictionary catalog questions, `CASE` expressions, `SUM`/`AVG`/`DATEDIFF`, cross-table, multi-group |
| `test_sql_multistep.py` | Yes | the enumerate/compose path specifically — pure-logic tests (routing gate, the `UNION ALL` SQL actually built, identifier escaping) plus `@pytest.mark.live` end-to-end tests with **structural** assertions (routes to enumerate, emits a `UNION ALL`, answer contains a number) rather than hardcoded counts, so they don't drift with live data |
| `test_chat.py` | **No** (gitignored) | full pipeline across all 5 routes |
| `test_docs.py` | **No** (gitignored) | the unstructured/documents path specifically |

Run: `python test_sql.py`, etc. — each is a standalone script (not invoked via `pytest` except
`test_sql_multistep.py`, which uses `@pytest.mark.live` — run with `pytest -m "not live"` to skip
the DB-dependent tests). `pytest.ini` registers the `live` marker.

The four gitignored suites contain hardcoded expectations derived from live client data and are
excluded from version control deliberately (`docs/`, `test_results/`, and these four files are
all gitignored for the same reason: they either are, or are derived from, real client data). They
exist locally and get refreshed by hand against the current database periodically — see the
ground-truth-drift note in §9.

---

## 11. Troubleshooting / restart guide

| Symptom | Likely cause | Fix |
|---|---|---|
| A turn hangs indefinitely / times out | Model unloaded (TTL expired) | `lms ps` — if empty, `lms load <model> -y --ttl 21600` |
| "I don't have enough schema information..." on everything | Qdrant is down | start the Qdrant binary; check `curl http://127.0.0.1:6333/collections` |
| SQL questions fail with a connection error | VPN not connected, or SQL Server unreachable | connect VPN; probe with a quick `execute_sql('SELECT 1')` |
| Page won't load at all | `webui.py` not running | `python webui.py` |
| Everything gets progressively slower within one conversation | History bloat (pre-fix) or just very long history | hit `/reset` (CLI) or **Reset** (web UI) |
| Router misclassifies every question as `both` | `<think>` budget exhausted `max_tokens` on an old build without the `REASONING_HEADROOM` fix | update to current `llm.py` |
| Web UI reachable but answers never start | `llm.health()` false — check the banner/`/state` response | confirm the chat server + model are actually up, not just the process |

Full stack, bring-up order: Qdrant → Ollama → chat server (+ load model) → VPN (if needed) →
`chat.py` or `webui.py`. See §4 for exact commands.

---

## 12. Configuration reference

All read by `config.py` from `.env` (see `.env.example`), with sensible localhost defaults except
where marked **required**.

| Variable | Default | Purpose |
|---|---|---|
| `DB_SERVER` | *(none — required)* | SQL Server host/instance |
| `DB_DATABASE` | `MetadataRepository` | database name |
| `DB_DRIVER` | `ODBC Driver 17 for SQL Server` | ODBC driver string |
| `DB_READONLY_USER` / `DB_READONLY_PASSWORD` | *(empty)* | dedicated read-only SQL login (§8); falls back to Windows auth if unset |
| `PLANNER_SCHEMA_DENYLIST` | `sys,INFORMATION_SCHEMA,guest` | schemas the enumerate path refuses to list |
| `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_GRPC_PORT` | `127.0.0.1` / `6333` / `6334` | Qdrant connection |
| `REASON_BASE_URL` | `http://127.0.0.1:1234/v1` | chat/reasoning server (LM Studio default; use `http://127.0.0.1:11434/v1` for Ollama) |
| `REASON_MODEL` | `qwen/qwen3.8-27b` | model id, exactly as the server's `/v1/models` reports it |
| `REASON_TIMEOUT` | `600` | seconds, `httpx` client timeout |
| `REASONING_HEADROOM` | `1024` | extra tokens reserved for `<think>` reasoning beyond the requested `max_tokens` |
| `REASONING_EFFORT` | `low` | default session thinking depth (`low`/`medium`/`xhigh`); setting this also skips the interactive startup prompt |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | embeddings backend (always Ollama, regardless of `REASON_BASE_URL`) |
| `HISTORY_TURNS` | `20` | how many past messages get replayed into the prompt per turn (0 disables the cap) |
| `HYBRID_SEARCH` | `1` (on) | BM25+dense fusion for the **documents** collection |
| `HYBRID_STRUCTURED` | `0` (off) | BM25+dense fusion for the **Data Dictionary** collection — off by default, see §5.4 |
| `HYBRID_CANDIDATE_POOL` | `20` | per-ranker candidate pool before RRF fusion |
| `RRF_K` | `60` | Reciprocal Rank Fusion damping constant |
| `RESTRICTED_DOC_PATTERNS` | `fha_hr_data,fha_leave_policy` | filename substrings auto-tagged `clearance: restricted` at ingest |
| `APP_CLEARANCE` | `general` | clearance set the CLI/web UI pass as the caller's identity |
| `WEBUI_PORT` | `8080` | `webui.py` listen port (host is hardcoded to `127.0.0.1`) |

Non-`.env` constants set directly in `config.py`: `COLLECTION_NAME` (`data_dictionary`),
`DOCS_COLLECTION_NAME` (`documents`), `VECTOR_SIZE` (`768`, must match the embedding model),
`TOP_K`/`DOCS_TOP_K` (`5`), `DOCS_FOLDER` (`docs`), `CHUNK_MAX_TOKENS`/`CHUNK_OVERLAP_TOKENS`
(`512`/`50`), `XLSX_ROWS_PER_CHUNK`/`XLSX_PROSE_THRESHOLD`/`XLSX_PROSE_MIN_CHARS`.

---

## 13. Project history

Condensed, chronological, grouped by theme rather than commit-by-commit (full detail is in
`git log`). Everything below was found through actual dogfooding or direct testing, not
speculative hardening — each fix maps to an observed failure.

**Foundational audit fixes** — Qdrant/Ollama host set to `127.0.0.1` (the Windows IPv6-stall
fix); dead fan-out planner code removed; SQL allowlist validator + read-only login script added
(replacing a leaky blocklist); Qdrant clearance-based access control added (deny-by-default);
`DB_READONLY_USER` warning moved off the console into a log file so it stopped interleaving with
streamed answers; `ingest.py`'s connection string fixed to include `ApplicationIntent=ReadOnly`
(the AlwaysOn secondary was rejecting connections without it, which had been misdiagnosed more
than once as "the SQL Server is down" when it was actually this).

**Router & conversation-state fixes** — router made context-aware via the deterministic pronoun
floor (§5.3) instead of prompt-injected context (which had dragged greetings into data routes);
`topic_table` fixed to update correctly on non-"columns" follow-ups; column-count questions on a
bare topic switch fixed to return the full column list instead of a partial vector-search result;
schema-listing questions ("what tables are in schema X") fixed to use an exhaustive scroll
instead of undercounting via top-k search; two-table comparison questions fixed to get full
schema for each named table instead of one starving the other.

**Q3/Q6/Q9 / Q8/Q11–Q14 fixes** (named from an internal coworker test set) — catalog-wide
"missing descriptions"/"how complete" questions routed to live SQL against
`[rpt].[DataDictionary]` instead of the vector store, which structurally can't answer them
(description-less rows are excluded at ingest); "which tables support X" theme questions given
the complete table catalog instead of a partial top-k slice that was missing thematically
relevant tables; "what/which table..." identity questions fixed to stop being misrouted into
column-listing by a stray word like "fields".

**Hybrid retrieval (BM25 + RRF)** — added for the documents collection (clear win: recovers exact
technical tokens embeddings blur); **measured to regress structured SQL grounding** (1/18
questions flipped to the wrong table) and scoped off for that path specifically — see §5.4.

**SQL grounding & aggregation fixes** — name-match plural handling widened from singular-`s`-only
to also try `-es`/`-ies` forms (fixed `"scanbatch"` failing to match `ScanBatches`, which had
caused a real near-tie grounding failure: the schema context fell back to an unranked candidate
list, and the model picked the wrong table); `ORDER BY`-with-`TOP` guard added (§5.5) after
observing "show me the 5 most recent X" occasionally return an arbitrary, unordered 5 rows;
`GROUP BY`/percentage/ratio prompt rules added after observing raw-row dumps and T-SQL
integer-division-to-0 bugs on live governance questions; a NULL-ordering guard added after
observing a "worst N by rate" follow-up silently return an all-NULL top-N because the standalone
version of the same question happened to include a NULL guard the follow-up dropped.

**Multi-intent decompose — added, then reverted.** A feature that split two-part questions ("how
many X and what does the docs say about Y") into independent sub-questions, answered each on its
own route, and concatenated the results. It measurably fixed several governance questions. It
was then found, via further dogfooding, to **over-split legitimate single-table questions** —
"how many scans are done and how many are pending in the scan_queue table" got split into two
sub-questions and the shared "in the scan_queue table" qualifier was dropped from the first,
sending it to the wrong table. Reverted rather than patched further, because the plain
single-query path already handled that class of question correctly on its own (a single T-SQL
query with two `COUNT(CASE WHEN...)` expressions) — the decompose feature was a net negative once
a stronger model made the simpler path capable enough.

**Model migration: `llama3.1:8b` (Ollama) → `qwen/qwen3.8-27b` (LM Studio).** Decision process:
compared Qwen3-30B-A3B (MoE) vs. Llama 3.3 70B on public benchmarks and settled on Qwen for
better benchmark performance at a fraction of the active parameters, then the user pointed at the
actually-available `Qwen/Qwen3.8-27B` (a newer, dense, dual-mode-reasoning model with a very long
native context) via a model card lookup — clearly stronger than either prior candidate for this
project's workload, so adopted directly instead. Embeddings were **deliberately kept on
Ollama + nomic-embed-text** rather than switched, specifically to avoid a full Qdrant re-ingest —
retrieval recall was never the bottleneck this session surfaced, generation quality was. This
required building `llm.py` (§5.2) as a backend-agnostic seam and surfaced the two real bugs
described there (`reasoning_content` vs inline `<think>`, and the `max_tokens`/thinking-budget
issue that broke routing). Back-tested against the prior model on `test_sql`/`test_chat` (parity)
and an 8-question governance-question set (went from ~1/8 correct to 6/8 — including a case where
the smaller model had conflated "column ownership" with "column description" and the larger model
correctly distinguished them).

**Web UI added** — `webui.py` + `static/index.html` (§4), built specifically so the reasoning
model, streaming, and markdown rendering could all be demonstrated without a terminal.

**Context/history fix + foreign-key joins** — see §7 and §5.5 respectively; both landed in the
same work session, verified with real before/after measurements (history-size growth, an actual
generated `JOIN` on a real FK) rather than just "should work" reasoning.

**Git identity note**: two early commits in this project's history were authored under an
auto-detected `@fraserhealth.ca` address rather than the personal address used for the rest of
history, because no git identity was configured locally at the time. Left as-is once already
pushed rather than rewritten.

---

## 14. Server migration & roadmap

Full plan and code-level before/after snippets live in `SERVER_MIGRATION.md`. Summary of
decisions made and why:

- **Model**: `qwen/qwen3.8-27b`, chosen over both a 70B-class dense model and a 30B-class MoE
  candidate after direct comparison (§13). Swappable later purely by changing
  `REASON_BASE_URL`/`REASON_MODEL` — no code changes, by design of `llm.py`.
- **Serving**: LM Studio for now (good GGUF support, used for local dev); **vLLM** is the target
  for the actual server, for real request batching under multiple concurrent users — this will
  need a non-GGUF quant (AWQ/GPTQ) rather than a code change.
- **Embeddings**: stay on Ollama + `nomic-embed-text` indefinitely unless retrieval quality
  becomes a demonstrated problem — switching would force a full re-ingest of both collections for
  a part of the pipeline that hasn't shown itself to be the bottleneck.
- **Thinking control**: `reasoning_effort` is already a first-class, per-session, swappable
  parameter (§5.2) — nothing further needed for the model swap itself.
- **Identity/RBAC**: deliberately not built into the web UI (§8/§9) in anticipation of putting
  **Microsoft Teams** in front of the assistant eventually, which supplies real per-user Entra ID
  identity for free — building a separate login system now would likely be thrown away. When that
  happens, `webui.py`'s `Session` becomes one of several front-end adapters over the same
  `chat.ask()` core; a Teams adapter would key sessions by Teams user id instead of a browser
  cookie, and the per-user identity would finally give the existing (currently dormant) clearance
  system something real to key off.
- **Still open, not yet started**: real RBAC end-to-end (user → roles → SQL Server row-level
  security), a `pyodbc` connection pool, moving `chat.main()`'s per-conversation locals into a
  proper session object everywhere (partially done for the web UI already), Qdrant running as a
  service on a non-OneDrive-synced data directory, self-validating tests to end the ground-truth
  drift problem (§9/§10).

---

## 15. File reference

```
local-rag/
├── chat.py              # CLI entry point + ask() — the core per-turn pipeline
├── webui.py             # local browser front-end (stdlib HTTP + SSE)
├── static/index.html    # web UI page (markdown renderer, FHA styling, effort selector)
├── router.py            # 5-way question classification
├── retriever.py         # Qdrant search (dense + hybrid) and all non-vector bypass paths
├── lexical.py           # zero-dependency BM25 index (hybrid retrieval)
├── sql_generator.py     # schema context (incl. FK joins) -> SQL -> validate -> execute -> answer
├── planner.py           # SQL scope resolution + the deterministic enumerate pipeline
├── llm.py               # model backend: reason() / reason_stream() / embed() / health()
├── chunker.py            # PDF/DOCX/XLSX/MD -> chunk dicts, for document ingestion
├── doc_registry.py       # tracks docs/ file state (new/modified/deleted) for incremental sync
├── ingest.py              # SQL Server DataDictionary -> Qdrant "data_dictionary" (full rebuild)
├── ingest_docs.py         # docs/ folder -> Qdrant "documents" (incremental)
├── config.py              # all settings, read from .env
├── .env.example           # copy to .env and fill in
├── requirements.txt
├── pytest.ini             # registers the `live` test marker
├── sql/
│   └── create_readonly_login.sql   # optional dedicated read-only SQL login
├── docs/                  # (gitignored) source documents for the "documents" collection
├── test_sql.py            # (gitignored) SQL suite — 20 questions, ground truth
├── test_sql_hard.py       # (gitignored) harder SQL patterns
├── test_sql_multistep.py  # enumerate-path suite (tracked — structural, not hardcoded, assertions)
├── test_chat.py           # (gitignored) full-pipeline suite, all 5 routes
├── test_docs.py           # (gitignored) documents-path suite
├── AUDIT.md               # point-in-time audit from early in the project — historical
├── SERVER_MIGRATION.md    # server-migration plan with code-level snippets
├── integration.md         # original planner-integration notes — superseded, historical
└── CHATBOT_CAPABILITIES.md  # (gitignored) coworker-facing capabilities summary
```

# local-rag — End-to-End Audit

**Date:** 2026-07-10  **Method:** code trace + live execution (services up: Ollama, Qdrant 559 dd / 102 doc points, SQL Server `MetadataRepository`). All findings below were reproduced by running the system, not just reading it.

---

## TL;DR verdict

The RAG core is in good shape — grounding enforcement works, routing is accurate (deterministic SQL gate + 88% on adversarial router queries), docs/chat suites are effectively perfect (46/46, 27/27), and the SQL suites are ~19/20 on correct SQL. **The problems are not in answer quality — they are in performance, security, and prod-readiness.** Three things dominate: a 1-line perf bug costing ~2s on every query, zero access control on a tool that already serves PII, and a leaky SQL guard against a live DB.

Several premises in the audit request are **out of date**: `test_sql_hard.py` is not "13/20 with 7 failures" — it's **19/20 with 1** (a shape quirk). The "Q5 ProcessJson hallucination" is **not a hallucination** — the model answer matches the doc verbatim. `test_docs_hard.py` **does not exist**.

---

## Fix these THREE before anything else

1. **Qdrant `localhost` → `127.0.0.1` (+ gRPC).** Every Qdrant call takes a flat **~2040ms** because Windows resolves `localhost` to IPv6 `::1` first and stalls ~2s before falling back to IPv4. `127.0.0.1` = **15ms**, gRPC = **1ms**. This is the single biggest latency lever, bigger than any model change. One line.
2. **Access control — there is none.** No user identity exists anywhere in the code. Any user of `chat.py` can retrieve every document chunk (including `FHA_HR_Data.xlsx` and the leave policy), read the full schema, and execute arbitrary `SELECT` against the whole database under the service account. For a health-authority governance tool touching PII this cannot ship as-is.
3. **SQL guard is a leaky blocklist against a live DB under a privileged identity.** `SELECT ... INTO`, stacked `SELECT INTO`, `WAITFOR DELAY`, and `OPENQUERY` all pass validation; `ApplicationIntent=ReadOnly` is a routing hint, not a permission. Needs a genuinely read-only SQL login + allowlist parsing.

---

## Top 10 ranked weaknesses

### 1. Qdrant latency: ~2s per call from `localhost` IPv6 stall — CRITICAL (perf)
**Root cause:** `config.py` / `.env` set `QDRANT_HOST=localhost`. On Windows, `localhost` resolves `::1` (IPv6) first; Qdrant listens on IPv4 `127.0.0.1`; each connection waits ~2s for `::1` to fail. Measured: `localhost` 2040ms, `127.0.0.1` 15ms, `127.0.0.1`+gRPC 1ms, on a 559-point collection. The `both` path pays this **~4×** (2 embeds + 2 Qdrant searches). Embedding client is NOT the cause — it's a module singleton at 84ms (suspicion ruled out).
**Fix:**
```dotenv
# .env
QDRANT_HOST=127.0.0.1
```
```python
# retriever.py:18, doc_registry.py:31, sql_generator.py:109 — enable gRPC for another ~15x
_qdrant_client = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT,
                              grpc_port=6334, prefer_grpc=True)
```
Also change the `config.py` default from `"localhost"` to `"127.0.0.1"` so the fast path is the default.

### 2. Zero access control (no RBAC / auth / user identity) — CRITICAL (security)
**Root cause:** No end-user concept exists. `chat.ask()` takes `(question, history, ...)` — no principal. Retrieval returns all chunks; the SQL path runs any `SELECT`. The "planned 3-table schema (roles, user_role_map, resource_permissions)" has nowhere to attach because no user identity is threaded through any path. Grep for `role`/`permission` finds only `{"role": "system"}` message dicts.
**Where enforcement must live (all three, none exist today):**
- **Qdrant payload filter** — tag every point at ingest with a classification/owner label (`ingest.py`, `ingest_docs.py` payloads) and add a `Filter` on `retrieve`/`retrieve_docs`/`fetch_all_columns` keyed to the caller's clearance.
- **SQL Server** — the SQL path has no user context to hand to RLS, so RLS is impossible until identity is threaded. Interim: a read-only login scoped to non-PII schemas (see #3).
- **Application layer** — add an auth front door; `chat.py` is anonymous and single-session.
**Fix (minimum viable):** thread a `user`/`clearance` param from the entry point through `ask()` → `retriever.*` (payload filter) and → `run_sql_pipeline` (schema allowlist). Ship with a deny-by-default doc filter so PII docs require clearance.

### 3. SQL validation is a leaky blocklist; ReadOnly is not enforcement — HIGH (security)
**Root cause:** `sql_generator.validate_sql` requires `startswith("SELECT")` + a keyword blocklist. Reproduced bypasses (all PASS validation):
- `SELECT * INTO evil_table FROM ...` — creates a table (a write)
- `SELECT * FROM x; SELECT name INTO backdoor FROM sys.tables` — stacked write
- `SELECT 1 WHERE 1=1 WAITFOR DELAY '00:00:30'` — resource/DoS
- `SELECT * FROM OPENQUERY(linked, '...')` — linked-server reach
And a false-positive: `WITH x AS (...) SELECT ...` (a legal read-only CTE) is **wrongly blocked**. `execute_sql` connects with `Trusted_Connection=yes` (the service's Windows identity, likely write/DDL-capable) and `ApplicationIntent=ReadOnly`, which only matters for AlwaysOn read routing — it is **not** a permission boundary.
**Fix:**
- Create a dedicated SQL login with `SELECT`-only grants (or `db_datareader`) on the allowed schemas; `REVOKE` `CREATE`/`INSERT`/etc.; connect as that login instead of Trusted_Connection.
- Replace the blocklist with allowlist parsing: single statement only (reject on `;`+more), must be `SELECT` or `WITH…SELECT`, reject `INTO`, `WAITFOR`, `OPENQUERY`, `OPENROWSET`, `OPENDATASOURCE`. Add these tokens to `_FORBIDDEN` immediately and allow a leading `WITH`.

### 4. `test_sql.py` reports 11/20 but 9 failures are stale ground truth — HIGH (correctness/signal)
**Root cause:** `sql_ok=19/20` (SQL structurally correct) but `results_ok=12/20`. Every failing value is **higher** than the frozen expectation — data drift: Q1 34 vs 33, Q13 10220 vs 9653, Q19 30 vs 27, Q20 SCANBATCH 12 vs 10, etc. `test_sql.py`'s ground truth was never refreshed (only `test_sql_hard.py` was). Q6 "got 5 rows, expected 5" is a fragment-match quirk, not a data error. This makes the suite falsely look like a 45% regression when the generator is actually ~19/20.
**Fix:** refresh expected values from authoritative canonical queries (same procedure already applied to `test_sql_hard.py`) — I can regenerate these in one pass. Add a comment dating the ground truth.

### 5. Ollama → vLLM migration: 19 hard-coupled call sites + embedding lock-in + single-user assumptions — HIGH (prod)
**Root cause:** `ollama.Client(...).chat()/.embeddings()` is called from 19 sites across `router`, `sql_generator`, `planner`, `retriever`, `chat`, `ingest*`. vLLM speaks the OpenAI API, not Ollama's — none of it works without a client swap. Embeddings use `nomic-embed-text` (768-dim) served by Ollama; vLLM typically won't serve it, and `VECTOR_SIZE=768` + the ingested collection are tied to it (swap ⇒ full re-ingest via `recreate_collection`). Concurrency: `pyodbc` opens/closes a fresh connection per SQL call (`execute_sql`, `_open_connection`), conversation `history` is a local list in `main()`, clients are process-global — all single-user/sync assumptions that break under multi-user serving.
**Fix:** introduce one `llm.py` abstraction (chat + embed) with an OpenAI-compatible backend; keep embeddings on a dedicated embedding server (or a vLLM-served embedding model, re-ingesting with the new dim); add a `pyodbc` connection pool; move `history`/`topic_table`/`last_sql` into a per-session object instead of `main()` locals.

### 6. No keyword/BM25 retrieval — pure cosine misses exact technical terms — MEDIUM (quality)
**Root cause:** `retriever.py` is dense-vector only (plus a payload-filter scroll bypass). For a technical-docs corpus, exact tokens (`AES_KEY_BASE64`, `StateID = 4`, `api.CallQueueGetNextCallTEST`, error codes, config keys) are exactly what embeddings blur. This already shows up as near-tie mis-retrievals in the SQL path (`stg.ScanControl` vs `dm_dq.scan_queue`).
**Fix:** enable Qdrant hybrid search — add a sparse vector (BM25/SPLADE) at ingest and fuse with dense via RRF; or add a lexical exact-match boost keyed on capitalized/dotted/underscored tokens in the query.

### 7. No reranking — MEDIUM (quality)
**Root cause:** top-k is returned raw by cosine score; on the tiny corpus (102+559 points) a cross-encoder rerank is cheap and would materially sharpen top-k precision, compounding with #6.
**Fix:** retrieve top-20, rerank with a small cross-encoder (`bge-reranker-base`), keep top-5. ~1 model, negligible latency at this corpus size.

### 8. Router: stateless follow-ups + "both" under-detection — MEDIUM (correctness)
**Root cause:** `router.route(question)` sees only the bare question — no history, no `topic_table`. Adversarial set: **32/36 (88%)**. Failures: follow-ups ("what about that table?") route blind; multi-intent ("leave policy AND how many employees") collapses to a single source (missed `both`); one creative OOS → unstructured. `chat.py` tracks `topic_table`/`last_sql` and rewrites downstream, but the *routing decision itself* is made context-free.
**Fix:** pass `last_route`/`topic_table` (and a one-line summary of the last turn) into `route()`; add an explicit "two distinct information needs ⇒ both" instruction + a couple of few-shots; treat a follow-up cue with a live `topic_table` as inheriting the prior route.

### 9. Broken test + dead code — MEDIUM (maintenance)
**Root cause:**
- `test_multistep_20q.py` is **100% broken**: unpacks a 5-tuple (`sql_marker, _, _, answer, _`) from `run_sql_pipeline`, which returns a **4-tuple** → `ValueError` on every test; checks the retired `-- multi-step` marker (now `-- enumerate`); and its ground truth is for a **different database** (schemas `dbo`/`meta`, tables `DDL_Events`/`ServerLogFields` that don't exist in `MetadataRepository`).
- `planner.py` carries ~200 lines of dead fan-out code (`make_plan`, `execute_plan`, `_resolve_ref`, `_resolve_args`, `synthesize_answer`, `_tool_list_schemas/_columns/_count_rows`, `TOOL_REGISTRY`, `TOOL_MANIFEST`, `_PLANNER_SYSTEM`, `_SYNTHESIS_SYSTEM`) — only referenced by each other after the compose refactor.
**Fix:** delete `test_multistep_20q.py` (superseded by `test_sql_multistep.py`) or rewrite it against the 4-tuple/`-- enumerate` API and the real DB; strip the dead planner code (kept only by an earlier "keep everything" call — revisit).

### 10. Config/onboarding gaps — LOW (maintenance)
**Root cause:** `config.py` header says "Copy `.env.example` to `.env`" but **`.env.example` doesn't exist**. No startup validation of required vars — missing `DB_SERVER` silently falls back to `localhost` (which, combined with #1, is a double footgun in prod). No `pytest.ini` (every run prints `PytestUnknownMarkWarning` for `@pytest.mark.live`).
**Fix:** add `.env.example`; validate required env at import and fail loud; add `pytest.ini` registering the `live` marker; default `QDRANT_HOST`/`DB_SERVER` sensibly.

---

## Section findings

### 1. Architecture & path traces
Entry: `chat.ask(question, history, show_sources, topic_table, last_sql)` → `router.route()` → branch. LLM calls counted per turn; **each Qdrant round trip currently = ~2s (finding #1)**.

| Path | LLM calls | Qdrant round trips | DB | Notes |
|---|---|---|---|---|
| general | 2 (route + answer) | 0 | 0 | no retrieval |
| structured | 2 (route + answer) + 1 embed | 1 search **or** 1 scroll (list-columns) | 0 | `fetch_all_columns` scroll bypass for "list all columns" |
| unstructured | 2 + 1 embed | 1 search (documents) | 0 | |
| both | 2 + 2 embed | **2** searches (dd + docs) | 0 | pays the Qdrant tax twice |
| sql/direct | route + gen + answer (3) + 1 embed | 1 search + up to 3 `fetch_all_columns` scrolls (primary+2 secondary) | 1 (connect/query/close) | `resolve_scope` is regex (no LLM) |
| sql/enumerate | route + answer (2) | 0 (uses `sys.tables`) | 1 conn, N+1 queries | deterministic UNION-ALL builder |

**Dead / unwired:** the entire fan-out planner in `planner.py` (see #9). `classify_complexity` and `run_multistep_pipeline` are fully removed (0 refs — good). Only `_tool_list_tables` of the four tools is live.

### 2. Test suites (current code)
| Suite | Result | Reality |
|---|---|---|
| `test_sql.py` | 11/20 | **~19/20** — 9 failures are stale ground truth (drift), `sql_ok=19/20`. Needs refresh (#4). |
| `test_sql_hard.py` | **19/20** | Ground truth already refreshed. Only Q3 fails. |
| `test_chat.py` | 27/27, route 26/27 | Clean. 1 borderline route miss ("dq.Executions used for?" → unstructured). |
| `test_docs.py` | 46/46, 0 route mismatches | Clean. |
| `test_sql_multistep.py` | 12/12 (9 logic + 3 live) | Clean (rebuilt this cycle). |
| `test_multistep_20q.py` | **broken** | Crashes on every test (#9). |

**Root causes, categorized:**
- `test_sql.py` failures → **stale ground truth**, not model/retrieval/prompt. Not a regression.
- `test_sql_hard.py` Q3 ("nullable vs non-nullable") → **prompt/shape**: model emits a `COUNT(CASE…)` pivot (one row, two columns); the validator wants `GROUP BY is_nullable` rows. Numbers are correct. Fix = nudge the prompt toward GROUP BY for "X vs Y" counts, or accept pivots in the validator.
- `test_chat.py` route miss → **prompt/router**: table-purpose question is legitimately borderline structured/unstructured.
- **"7 failures in test_sql_hard.py"** → outdated; it's 1. **"Q5 ProcessJson hallucination"** → not a hallucination; `{JsonReceived}`/`{ApiResponse}` are both in the doc's placeholder table (line 206). The answer is correctly grounded.

### 3. Performance
Measured (min-of-N, box under load): embed **84ms** (nomic, singleton client — suspicion ruled out), Qdrant search **2040ms** (`localhost`) → **15ms** (`127.0.0.1`) → **1ms** (gRPC), Qdrant scroll same ~2s. **Single biggest lever before touching model size: finding #1** — fixing the host turns a ~4-6s retrieval-bound turn into a sub-second one. Generation (llama3.1:8b) is the next lever and is a model/hardware question, not a code bug.

### 4. Router
32/36 adversarial. Categories that fail: multi-intent (`both`), context-dependent follow-ups, and one OOS creative prompt. Strong on the hard SQL-vocab-but-documentation cases (all correct) and dot-notation-in-docs (all correct) — those were the risky ones and they held. See #8.

### 5. RAG quality
- **Chunking:** heading/semantic-aware (`chunker.py`: PDF per-page+heading, DOCX by heading style, MD by `#`, XLSX prose-vs-tabular detection), 512-token cap with 50 overlap only on oversized sections. This is **good**, not naive fixed-size. DataDictionary is 1-row-per-column (fine-grained; the catalog-pin + `fetch_all_columns` compensate for table-level questions).
- **Keyword/BM25:** absent (#6). Highest-value RAG gap for this corpus.
- **Reranking:** absent (#7).
- **Grounding:** **strong.** Direct test — 3 out-of-context questions (undocumented retry count, IT phone number, primary key of dq.Results) all correctly declined with "I don't have that information," including the PK-inference trap the prompt explicitly forbids. No fabrication observed.

### 6. SQL path
- **Model-ceiling vs fixable:** on `llama3.1:8b`, the genuinely hard patterns are (a) choosing between semantically near-tie tables without a name cue (mitigated by the confidence-gated name-match grounding already in place), and (b) shape choices like pivot-vs-GROUP BY (Q3). The old "wrong table" failures (stg.SqlColumns, mdm.Columns, row_count_runs) are **fixed** — every hard-suite query now hits the correct table (`sql_ok=20/20`). What remains is either drift (test artifact) or shape (one case). Nothing here needs a bigger model; it needs prompt/validator tweaks.
- **Injection/sandbox:** see #3 — the material risk.

### 7. Production readiness (Windows Server)
- **Qdrant OneDrive corruption:** current storage is `C:\Users\yameziane\development\qdrant\storage` and the repo is `…\development\local-rag` — **both outside** OneDrive, so the prior failure mode is **not currently at risk**. But `OneDrive - HealthBC` (corporate, Known-Folder-Move capable) is present; the constraint is undocumented and unguarded. **Action:** keep Qdrant `storage/` and `docs/` out of any KFM-redirected folder (Desktop/Documents/Pictures); document it; consider running Qdrant as a service with an explicit non-synced data dir.
- **Secrets/.env:** `.env` is gitignored (good) and currently holds no password (Windows auth). Gaps: no `.env.example` (#10), no validation, and the localhost defaults (#1). When RBAC/AES arrive, there's no secret-management story.
- **Ollama→vLLM:** see #5.
- **RBAC/ABAC:** see #2 — nothing exists; all three enforcement points are gaps.

---

## Backlog (grouped by subsystem)

**Retrieval / RAG**
- Hybrid BM25+dense (#6), cross-encoder rerank (#7).
- Consider raising doc `DOCS_TOP_K` after rerank lands; measure recall on the 6 docs.

**Router**
- History-aware routing + `both` detection (#8).
- Borderline table-purpose questions (structured vs unstructured) — add few-shots.

**SQL**
- Read-only login + allowlist parser + CTE support (#3).
- Q3 pivot-vs-GROUP BY prompt nudge.
- `pyodbc` connection pool (#5).

**Tests**
- Refresh `test_sql.py` ground truth (#4).
- Delete/rewrite `test_multistep_20q.py` (#9).
- Add `pytest.ini` (`live` marker) (#10).

**Platform / prod**
- LLM client abstraction for vLLM (#5); per-session state; embedding-server plan.
- Qdrant as a service on a non-synced data dir; document the OneDrive constraint (#7-prod).
- `.env.example` + startup config validation (#10).

**Cleanup**
- Strip dead fan-out code in `planner.py` (#9).
- Duplicate `_build_sql_prompt` definition in `sql_generator.py` (the 2-arg version is dead; the 3-arg wins).

---

## What's actually good (don't touch)
- **Grounding enforcement** — declines cleanly, resists PK/constraint inference.
- **Deterministic SQL scope gate** (`resolve_scope`) — 0 misroutes on the 20 hard questions, no LLM in the loop.
- **Table-selection grounding** (catalog pin + confidence-gated name-match) — correct table on 20/20 hard questions.
- **Chunking** — semantic/heading-aware, appropriate for the corpus.
- **docs/chat suites** — 46/46 and 27/27; citations fixed; router mismatches at 0.

# Server Migration — status & plan

## Target stack

| Role | Model | Server | Notes |
|---|---|---|---|
| Reasoning: routing, SQL generation/repair, answers, doc Q&A | **Qwen3.8-27B** (Q6_K GGUF) | LM Studio `:1234` (dev) | thinking depth via `reasoning_effort` |
| Embeddings | nomic-embed-text | Ollama `:11434` | **unchanged — no re-ingest** |

Everything talks to **localhost only**. The chat call uses the OpenAI-compatible HTTP
shape (`POST /v1/chat/completions`) because LM Studio, Ollama, vLLM and llama.cpp all
speak it — it is a request format, not a hosted service. Implemented with `httpx`
(already a dependency); the `openai` package is deliberately **not** used.

---

## DONE — code migration (complete, validated)

`llm.py` (new) is the single backend seam:

| Function | Backend |
|---|---|
| `reason(messages, temperature, max_tokens, effort)` | chat server, non-streaming, `<think>` stripped |
| `reason_stream(messages, temperature, effort)` | chat server, SSE; suppresses `<think>` spans across chunks |
| `embed(text)` | Ollama + nomic (identical vectors) |
| `set_reasoning_effort()` / `get_reasoning_effort()` | session thinking depth |
| `health()` | chat-server reachability + model list |

Call sites migrated off `ollama.Client`:
- `router.route` → `llm.reason(..., effort="low")` — a one-word label must never think.
- `chat.ask` (both streaming spots) → `llm.reason_stream`.
- `sql_generator`: `generate_sql`, `repair_sql`, `repair_missing_order_by`, `generate_answer` → `llm.reason`.
- `retriever.embed_query`, `ingest.embed_texts`, `ingest_docs.embed` → `llm.embed`.
- `test_chat.py`, `test_docs.py` → `llm.reason` + `llm.health`.

Config (`config.py`):
```
REASON_BASE_URL   http://127.0.0.1:1234/v1     # LM Studio; Ollama = :11434/v1; vLLM on server
REASON_MODEL      qwen/qwen3.8-27b
REASON_TIMEOUT    600
REASONING_EFFORT  low                          # low | medium | xhigh
EMBED_MODEL       nomic-embed-text  (OLLAMA_HOST unchanged)
CHAT_MODEL / ROUTER_MODEL = REASON_MODEL       # back-compat aliases
```

### Reasoning effort
Chosen **at the start of the conversation**, in this precedence:
1. `--effort xhigh` / `-e medium` on the command line
2. `REASONING_EFFORT` env var
3. interactive prompt at startup (defaults to `low`)

`/effort` shows it mid-session, `/effort medium` changes it. Routing always overrides to
`low`. If a server rejects the `reasoning_effort` field, `llm.py` retries once without it,
so the same code works against Ollama and vLLM.

### Backend switching
Only `.env` changes:
```dotenv
# LM Studio (default)
REASON_BASE_URL=http://127.0.0.1:1234/v1
REASON_MODEL=qwen3.8-27b
# Ollama fallback
# REASON_BASE_URL=http://127.0.0.1:11434/v1
# REASON_MODEL=llama3.1:8b
```

---

## DONE — Qwen3.8-27B brought up and validated
LM Studio, `qwen/qwen3.8-27b` (Q6_K GGUF), port 1234. Back-tested against the
`llama3.1:8b` baselines: `test_sql.py` 19/20 (SQL fragments 20/20 — the one miss
is a rigid test-validator artifact, not a wrong answer), `test_chat.py` 27/27
(route 26/27), matching the 8B on both. The 8-question governance eval went
from ~1/8 correct to 6/8 — including a case where the 8B conflated "column
ownership" with "column description" and Qwen3.8 correctly distinguished them.
Full detail and the rest of the project history: README.md §13.

---

## DONE — production-readiness pass (this repo)

Code changes that don't require the server to exist, so they're written and
tested locally against the dev SQL Server + local Qdrant/LM Studio, per the
project's standing rule of never committing/pushing without being told to:

- **`db_pool.py`** (new) — bounded, thread-safe pyodbc connection pool
  (`config.DB_POOL_SIZE`, default 5). `sql_generator.execute_sql()` and
  `planner.run_enumerate_pipeline()` draw from it instead of opening a fresh
  connection per call — meaningful once concurrent users can trigger
  overlapping SQL calls, which serial per-call connections don't handle well.
  Verified: checkout/reuse (same connection object across calls), pool
  exhaustion blocks correctly at `DB_POOL_SIZE` and releases on checkin, a
  killed/dead pooled connection is detected and transparently replaced.
- **`ingest.py`** normalized to call `sql_generator._build_conn_str()` instead
  of its own inline connection string (matches the pattern `planner.py`
  already used — audit #3) — it now honors `DB_READONLY_USER`/`PASSWORD` when
  set instead of always running under the service's Windows identity.
- **`session.py`** (new) — `Session` (history/topic_table/last_sql/
  last_intent/last_route) extracted from `webui.py` into a shared module;
  `chat.py`'s CLI `main()` now instantiates the same class instead of five
  separate locals. Both front-ends share one implementation.
- **`config.py`**: `DB_POOL_SIZE` env var; a warning if exactly one of
  `DB_READONLY_USER`/`DB_READONLY_PASSWORD` is set (previously silently fell
  back to `Trusted_Connection`, which is surprising).
- **`requirements.txt`**: added `httpx==0.27.2` (was an undeclared transitive
  dependency of `llm.py`).

**Explicitly not done here** (see README §14 / §8): real end-user auth/RBAC.
`ask()`'s `clearance` param is already the seam; building a throwaway login
system before Teams supplies real identity would be wasted work.

## TODO — Windows Server runbook (`ops/`, not executed by this assistant)

The server is **Windows Server**, not Linux — this rules out **vLLM** (no
reliable native Windows support), so it's no longer the recommendation here.

- **Serving engine: Ollama**, not LM Studio, for the server role. Rationale:
  installs as a native Windows service (LM Studio is a desktop app with a
  dev-server bolted on — less suited to unattended server operation); already
  proven in this project for embeddings; speaks the same OpenAI-compatible
  `/v1` `llm.py` already targets, so **zero code changes**; supports
  `OLLAMA_NUM_PARALLEL` for genuinely concurrent request handling (not just a
  GUI-managed per-model setting like LM Studio's "Max Concurrent Predictions").
  `qwen3.8-27b` may not be in Ollama's public library under that exact name —
  the runbook covers the manual GGUF/Modelfile import fallback.
- **Qdrant**: run as a Windows Service (`sc.exe create`, no extra tooling)
  pointed at a plain, non-OneDrive/KFM-synced data directory — see
  `ops/install_qdrant_service.ps1`.
- **SQL login**: `sql/create_readonly_login.sql` already exists — run it
  against the target SQL Server if not already done, then set
  `DB_READONLY_USER`/`DB_READONLY_PASSWORD` in the server `.env`
  (`ops/server.env.example`). The connection-pool + `ingest.py` normalization
  above pick this up automatically once set.
- **Not solved here**: `webui.py`'s `HOST` is hardcoded `127.0.0.1`. Reaching
  it from another machine needs binding `0.0.0.0` plus at minimum a
  shared-token guard (not built — same non-goal as RBAC above). Decide this
  before exposing it beyond the box it runs on.

See `ops/windows_server_setup.md` for the full ordered runbook.

## Quality backlog (revisit on the stronger model)
Value-domain injection (feed real `DISTINCT` values so filters aren't guessed) ·
anchor the SQL follow-up path to `topic_table` · cross-encoder rerank · schema-level
descriptions · self-validating tests (ends the ground-truth drift treadmill).

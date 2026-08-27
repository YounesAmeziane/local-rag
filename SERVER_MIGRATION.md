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
REASON_MODEL      qwen3.8-27b
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

## TODO — bring up Qwen3.8-27B
1. LM Studio → load the Q6_K GGUF → **Developer → Start Server** (port 1234).
2. Set `REASON_MODEL` to the exact id from `GET /v1/models`.
3. Run `test_sql.py`, `test_chat.py`, `test_docs.py`; compare against the 8B baselines
   (sql 20/20, chat 27/27 + route 26/27, docs 46/46).
4. Re-run the 8-question governance eval — the deferred semantic misses
   (`successful`≡`done`, `Owner` vs `Description`, empty-table reads) should improve.
5. Tune: if SQL quality lags, raise effort for SQL calls; if answers are slow, keep `low`.

## TODO — production server
- **Serving:** LM Studio and Ollama effectively serialize requests. For multi-user, use
  **vLLM** (real batching) — needs a non-GGUF quant (AWQ/GPTQ), a re-download, not a re-ingest.
  Only `REASON_BASE_URL`/`REASON_MODEL` change in code terms.
- **Concurrency:** `pyodbc` opens a connection per call — add a pool. Move
  `history`/`topic_table`/`last_sql` out of `main()` locals into a per-session object.
- **Qdrant:** run as a service on a non-synced data dir (keep out of OneDrive/KFM).
- **RBAC:** thread real user identity → clearance (deny-by-default filter exists but is
  dormant); ideally SQL Server RLS on the query path.
- **Config/secrets:** server `.env`, startup validation, secret management.

## Quality backlog (revisit on the stronger model)
Value-domain injection (feed real `DISTINCT` values so filters aren't guessed) ·
anchor the SQL follow-up path to `topic_table` · cross-encoder rerank · schema-level
descriptions · self-validating tests (ends the ground-truth drift treadmill).

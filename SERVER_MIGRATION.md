# Server Migration — Ollama → vLLM

**Target stack**
| Role | Model | Server | Notes |
|---|---|---|---|
| Reasoning (routing, answers, doc Q&A, SQL repair) | Qwen3-32B (→ 70B later) | vLLM `:8001` | thinking **off** |
| SQL generation | SQLCoder-7B-2-AWQ | vLLM `:8002` | completion format, T-SQL |
| Embeddings | nomic-embed-text | Ollama `:11434` | **no re-ingest** |

Everything is endpoint/config-driven, so local (Ollama) and server (vLLM) differ only by `.env`.
The deterministic layers — `validate_sql`, the Q6 ORDER BY guard, `_guard_null_order`, the
execute→repair loop — are **model-agnostic and unchanged**.

---

## 1. Dependency

```
pip install openai        # vLLM + Ollama both speak the OpenAI API; ollama stays for embeddings
```

---

## 2. New file: `llm.py`

```python
# llm.py — model-backend abstraction.
#   reason()/reason_stream() -> Qwen3 via vLLM (thinking disabled)
#   sql_generate()           -> SQLCoder via vLLM (completion)
#   embed()                  -> nomic via Ollama (unchanged vectors)
import re
from openai import OpenAI
import ollama
import config

_reason = OpenAI(base_url=config.REASON_BASE_URL, api_key=config.LLM_API_KEY)
_sql    = OpenAI(base_url=config.SQL_BASE_URL,    api_key=config.LLM_API_KEY)
_embed  = ollama.Client(host=config.OLLAMA_HOST)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}   # Qwen3: no reasoning trace

def reason(messages, temperature=0.1, max_tokens=1024):
    r = _reason.chat.completions.create(
        model=config.REASON_MODEL, messages=messages,
        temperature=temperature, max_tokens=max_tokens, extra_body=_NO_THINK)
    return _THINK_RE.sub("", r.choices[0].message.content or "").strip()

def reason_stream(messages, temperature=0.5):
    s = _reason.chat.completions.create(
        model=config.REASON_MODEL, messages=messages,
        temperature=temperature, stream=True, extra_body=_NO_THINK)
    for chunk in s:
        tok = chunk.choices[0].delta.content
        if tok:
            yield tok

def sql_generate(prompt, temperature=0.0, max_tokens=512):
    r = _sql.completions.create(
        model=config.SQL_MODEL, prompt=prompt,
        temperature=temperature, max_tokens=max_tokens, stop=[";", "```", "###"])
    return r.choices[0].text.strip()

def embed(text):
    return _embed.embeddings(model=config.EMBED_MODEL, prompt=text)["embedding"]
```

---

## 3. `config.py`

**Before** (lines ~35–38)
```python
EMBED_MODEL           = "nomic-embed-text"
CHAT_MODEL            = "llama3.1:8b"
ROUTER_MODEL          = "llama3.1:8b"
OLLAMA_HOST           = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
```

**After**
```python
# Reasoning model (routing, answers, doc Q&A, SQL repair)
REASON_BASE_URL = os.getenv("REASON_BASE_URL", "http://127.0.0.1:8001/v1")
REASON_MODEL    = os.getenv("REASON_MODEL", "Qwen/Qwen3-32B-AWQ")
# SQL model (text->SQL)
SQL_BASE_URL    = os.getenv("SQL_BASE_URL", "http://127.0.0.1:8002/v1")
SQL_MODEL       = os.getenv("SQL_MODEL", "defog/sqlcoder-7b-2")
LLM_API_KEY     = os.getenv("LLM_API_KEY", "not-needed")   # vLLM ignores; SDK requires it
# Embeddings stay on Ollama (nomic) — no re-ingest
EMBED_MODEL     = "nomic-embed-text"
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
CHAT_MODEL      = REASON_MODEL   # back-compat alias for the test suites
```

---

## 4. `router.py`

**Before** (lines 2, 5, 158–166)
```python
import ollama
...
_client = ollama.Client(host=config.OLLAMA_HOST)
...
        resp = _client.chat(
            model=config.ROUTER_MODEL,
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM + _EXAMPLES},
                {"role": "user",   "content": f"Q: {question}"},
            ],
            options={"temperature": 0, "num_predict": 10},
        )
        label = resp["message"]["content"].strip().lower().split()[0]
```

**After** (drop `import ollama` and the `_client` line; add `import llm`)
```python
        out = llm.reason(
            [{"role": "system", "content": _ROUTER_SYSTEM + _EXAMPLES},
             {"role": "user",   "content": f"Q: {question}"}],
            temperature=0, max_tokens=10)
        label = out.strip().lower().split()[0]
```
> `llm.reason` already strips any `<think>` block, so the one-word label parse stays valid.

---

## 5. `chat.py` — two streaming spots (lines ~173 and ~333)

**Before** (identical at both)
```python
        client = ollama.Client(host=config.OLLAMA_HOST)
        response = client.chat(
            model=config.CHAT_MODEL,
            messages=messages,
            stream=True,
            options={"temperature": 0.5},   # 0.1 at the second spot
        )
        ...
        for chunk in response:
            token = chunk["message"]["content"]
            full_response += token
            print(token, end="", flush=True)
```

**After** (drop `import ollama`; add `import llm`)
```python
        full_response = ""
        for token in llm.reason_stream(messages, temperature=0.5):   # 0.1 at the second spot
            full_response += token
            print(token, end="", flush=True)
```

---

## 6. `sql_generator.py`

Drop `import ollama` and `_ollama = ollama.Client(...)`; add `import llm`.

**6a. `generate_sql` → SQLCoder** (lines 361–368)
```python
    sql = llm.sql_generate(_build_sqlcoder_prompt(question, schema_context, last_sql))
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()
```

**6b. `repair_sql` → Qwen3** (lines 382–396) — replace the `_ollama.chat(...)` call with:
```python
    out = llm.reason(
        [{"role": "system", "content": _SQL_SYSTEM},
         {"role": "user", "content": (
             f"Schema context from Data Dictionary:\n\n{schema_context}\n\n---\n\n"
             f"The following T-SQL query failed to execute. Fix it and output ONLY "
             f"the corrected SELECT query (no explanation, no markdown).\n\n"
             f"Question: {question}\n\nFailed query:\n{bad_sql}\n\n"
             f"SQL Server error:\n{error}")}],
        temperature=0)
    sql = re.sub(r"^```(?:sql)?\s*", "", out, flags=re.IGNORECASE)
```
(same `.reason(...)` swap for **`repair_missing_order_by`**, line 428)

**6c. `generate_answer` → Qwen3** (lines 628–639)
```python
    return llm.reason(
        [{"role": "system", "content": _ANSWER_SYSTEM},
         {"role": "user", "content": f"Question: {question}\n\nResults:\n{results_context}"}],
        temperature=0.1)
```
> Note the old code returned `resp` then read `resp["message"]["content"]` below — with `reason()`
> returning the string directly, delete that unwrap line and just return the call.

---

## 7. SQLCoder prompt builder (new, in `sql_generator.py`)

SQLCoder-7B-2 is a **completion** model with the defog template, and it is **Postgres-leaning** —
so we pin T-SQL explicitly and fold in the domain rules that used to live in `_SQL_SYSTEM`.

```python
_SQLCODER_RULES = """- Dialect: Microsoft SQL Server (T-SQL). Use TOP (not LIMIT), GETDATE() (not NOW()),
  and [square brackets] for identifiers. Fully-qualify tables as [DB].[Schema].[Table].
- SELECT only. "how many/number of" -> COUNT(*). "per/by X" -> GROUP BY X (group col first).
- "what percentage/ratio/how complete" -> 100.0 * SUM(CASE WHEN ... THEN 1 ELSE 0 END)/COUNT(*).
- Superlative TOP N ("most recent/lowest") MUST pair with ORDER BY; exclude NULLs when ordering ASC.
- PassRate is a 0-1 fraction (0.8 = 80%). For the DataDictionary catalog use [rpt].[DataDictionary]."""

def _build_sqlcoder_prompt(question, schema_context, last_sql=None):
    ctx = schema_context + (f"\n\nPrevious query (follow-up context):\n{last_sql}" if last_sql else "")
    return (
        f"### Task\nGenerate a T-SQL SELECT query that answers [QUESTION]{question}[/QUESTION]\n\n"
        f"### Instructions\n{_SQLCODER_RULES}\n\n"
        f"### Database Schema\n{ctx}\n\n"
        f"### Answer\nThe T-SQL query that answers [QUESTION]{question}[/QUESTION]:\n[SQL]\n"
    )
```

**Tuning caveats to expect (test these first):**
- SQLCoder won't emit `INSUFFICIENT_SCHEMA` — that guard becomes a no-op; the validator + repair loop still catch bad output.
- Confirm it respects `TOP`/brackets; if it emits Postgres-isms, strengthen `_SQLCODER_RULES` or add them to the `stop` list.
- SQLCoder wants schema as compact `CREATE TABLE`-ish metadata; our `get_schema_context` block usually works, but if quality dips, render columns as `col TYPE` lines under `CREATE TABLE [schema].[table] (...)`.

---

## 8. Embeddings — minimal (no re-ingest)

`retriever.py` (line 474), `ingest.py` (111), `ingest_docs.py` (55): swap the ollama call for `llm.embed`.

**Before**
```python
resp = _ollama_client.embeddings(model=config.EMBED_MODEL, prompt=query)
return resp["embedding"]
```
**After** (`import llm`; drop the local ollama client)
```python
return llm.embed(query)
```
> Same model, same host → identical vectors → existing Qdrant collections stay valid. On the server,
> just point `OLLAMA_HOST` at wherever Ollama runs.

---

## 9. Test suites

`test_sql.py`, `test_sql_hard.py`, `test_chat.py`, `test_docs.py` build their own `ollama.Client`
and call `.chat(...)`. Simplest fix: delete those clients and route their turns through `llm.reason`
/ `run_sql_pipeline` (which now use `llm`). `config.CHAT_MODEL` still resolves (alias in §3) so the
banner/JSON fields keep working. These are gitignored fixtures — update alongside, not blocking.

---

## 10. vLLM launch (reference)

```bash
# reasoning — Qwen3, thinking handled per-request via enable_thinking=false
vllm serve Qwen/Qwen3-32B-AWQ --port 8001 --quantization awq --served-model-name Qwen/Qwen3-32B-AWQ
# sql — SQLCoder
vllm serve defog/sqlcoder-7b-2 --port 8002 --served-model-name defog/sqlcoder-7b-2
# embeddings — keep Ollama running with the model pulled
ollama pull nomic-embed-text
```

---

## 11. Ordered checklist
1. `pip install openai`; add `llm.py` (§2); update `config.py` (§3).
2. Point `.env` at Ollama locally first (`REASON_BASE_URL=http://127.0.0.1:11434/v1`, `REASON_MODEL=llama3.1:8b`) and confirm the abstraction works with the existing suites — **decouples the code change from the model change.**
3. Swap `router.py`, `chat.py`, `sql_generator.py` (§4–6), `retriever/ingest*` (§8).
4. Stand up the two vLLM servers; flip `.env` to the vLLM endpoints + real model names.
5. Build the SQLCoder prompt (§7) and **run `test_sql.py` / `test_sql_hard.py`; tune `_SQLCODER_RULES` for T-SQL** until grounding + dialect are clean.
6. Re-run all suites on the new stack; re-check the governance eval (the 70B/Qwen3 should lift the semantic misses we deferred).
```

# Integration guide: multi-step SQL planner

Three surgical changes wire `planner.py` into the existing `local-rag` codebase. Total edited lines across the two existing files: **7**.

The design keeps the entire multi-step machinery in `planner.py`, so if you ever need to revert or A/B test, the diff below is what you undo.

---

## 1. `config.py` — add one line

Where to put it: right after the existing `DB_*` block (`config.py:8-13`), so all DB/planner config sits together.

```python
# Multi-step planner: schemas the enumeration tools will refuse to list.
# Comma-separated in .env. Defaults to system/utility schemas.
PLANNER_SCHEMA_DENYLIST = [
    s.strip()
    for s in os.getenv("PLANNER_SCHEMA_DENYLIST", "sys,INFORMATION_SCHEMA,guest").split(",")
    if s.strip()
]
```

Optional `.env` entry (leave it out to use the defaults):

```
PLANNER_SCHEMA_DENYLIST=sys,INFORMATION_SCHEMA,guest
```

---

## 2. `sql_generator.py` — hook at the top of `run_sql_pipeline()`

Where: `sql_generator.py:360` (the first line inside `run_sql_pipeline`).

Add these three lines at the very top of the function body, **before** the current `get_schema_context()` call:

```python
def run_sql_pipeline(question: str, last_sql: str | None = None):
    # Multi-step planner branch. Compound questions (e.g. "how many rows
    # across schema X") route here instead of the single-shot NL->SQL path.
    # See planner.py.
    from planner import classify_complexity, run_multistep_pipeline
    if classify_complexity(question) == "multi_step":
        return run_multistep_pipeline(question)

    # ... existing body unchanged from here ...
    schema_context, results = get_schema_context(question, last_sql)
    # ... rest of the existing function ...
```

`from planner import ...` is deferred inside the function (not at module top) to avoid a circular-import risk if we ever want `planner.py` to import a helper from `sql_generator.py`. It's a one-time cost per turn — the module is cached after first import.

The return tuple shape from `run_multistep_pipeline()` matches the existing `run_sql_pipeline()` return exactly, so nothing downstream in `chat.py` needs changing.

---

## 3. Drop `planner.py` next to `sql_generator.py`

No path manipulation needed. `planner.py` imports `config` the same way the rest of the repo does, and creates its own logger writing to `logs/planner.log` (creates `logs/` if it doesn't exist).

---

## What each existing branch looks like after the change

| Question shape | Router | SQL branch |
|---|---|---|
| "How many rows in `[X].[Y].[Z]`?" | → `sql` | classifier → `single_step` → existing NL→SQL path (unchanged) |
| "How many rows across schema `dm_dq`?" | → `sql` | classifier → `multi_step` → planner/executor/synthesis |
| "Tell me about our data quality process" | → `unstructured` | (unchanged) |
| Etc. | (unchanged) | (unchanged) |

Only the `sql` branch behavior changes, and only for questions the classifier flags as compound. Everything else is untouched.

---

## Testing

Run the pure-logic tests (no DB, no Ollama needed):

```bash
pytest test_sql_multistep.py -m "not live"
```

Run the live end-to-end tests (needs Ollama + SQL Server + at least one table in `dm_dq`):

```bash
pytest test_sql_multistep.py -m live
```

Or everything at once:

```bash
pytest test_sql_multistep.py -v
```

Register the `live` marker in your `pytest.ini` / `pyproject.toml` if you want to suppress the "unknown marker" warning:

```ini
[pytest]
markers =
    live: end-to-end tests requiring Ollama and SQL Server
```

**Regression check.** The existing `test_sql.py` and `test_sql_hard.py` all target `run_sql_pipeline()` directly. Every case in those suites should still pass unchanged — they're all single-step questions, so the classifier will route them to the existing code path with zero behavioral change.

---

## Where to look when it misbehaves

`logs/planner.log` — every classifier decision, plan, tool call, tool args, tool result, and synthesized answer. Rotating file, 5 MB × 3 backups. Structured with timestamps and `[LEVEL]` tags:

```
2026-07-07 14:22:11 [INFO] === multistep start: q='how many rows in schema dm_dq' ===
2026-07-07 14:22:11 [INFO] classifier: q='...' raw='multi_step' label=multi_step
2026-07-07 14:22:12 [INFO] planner: 2 steps for q='...'
2026-07-07 14:22:12 [INFO] planner:   step1 tool=list_tables args={'schema': 'dm_dq'}
2026-07-07 14:22:12 [INFO] planner:   step2 tool=count_rows args={'schema': 'dm_dq', 'table': '$step1.tables[*]'}
2026-07-07 14:22:12 [INFO] executor: step1 tool=list_tables args={'schema': 'dm_dq'}
2026-07-07 14:22:13 [INFO] executor: step2 tool=count_rows args={'schema': 'dm_dq', 'table': 'row_count_snapshots'}
2026-07-07 14:22:13 [INFO] executor: step2 tool=count_rows args={'schema': 'dm_dq', 'table': 'stability_targets'}
... (one line per fanned-out invocation) ...
2026-07-07 14:22:14 [INFO] synthesis: 'The dm_dq schema has 4 tables containing a total of ...'
2026-07-07 14:22:14 [INFO] === multistep end: 5 tool calls ===
```

Common failure modes and what they look like:

- **`classifier: exception ...`** → Ollama unreachable. Falls back to single_step (safe).
- **`planner: JSON decode error ...`** → LLM returned malformed JSON. Empty plan; user sees the "couldn't break this question into steps" message.
- **`planner: exception ...`** → Ollama call failed. Same as above.
- **`executor: ... failed`** → Tool call failed (DB error, unknown tool, bad reference). Triggers one replan attempt; if that also fails, user sees the error surfaced in the answer.

---

## What's deliberately not in scope

- **No `run_readonly_query` escape hatch.** If a compound question needs SQL the four fixed tools can't express, the planner will produce an empty plan and the user sees the "couldn't break this down" message. Watch `logs/planner.log` for how often this happens in practice — if it's a real problem, adding the escape hatch is a small follow-up.
- **No validator step.** The synthesis LLM computes final numbers from the trace. If the arithmetic turns out to be unreliable, next iteration is to add a deterministic `_summarize_trace()` function that computes sums/counts in Python and hands the result to synthesis as already-computed values.
- **No cross-turn multi-step state.** After a multi-step turn, `last_sql` is set to `None`, so a follow-up like "of those, which is biggest?" won't have context and will be routed as a fresh question. Fixing this properly means threading a richer state object through `chat.py`, which is outside this change's scope.
- **Not fixing the duplicate `_build_sql_prompt` in `sql_generator.py`** flagged in the map. It's real, but orthogonal.
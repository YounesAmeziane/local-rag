"""
planner.py — Multi-step planner/executor for the SQL branch.

Implements a plan-then-execute loop for compound SQL questions
(e.g. "how many rows across schema X" where X has many tables).
Single-step questions still go through the existing NL->SQL path
in sql_generator.py — this module is only entered when the
complexity classifier flags a question as multi-step.

Design principles (see conversation notes for rationale):

- Fixed tools only. The planner picks from a whitelist
  (list_schemas, list_tables, list_columns, count_rows).
  No free-form SQL generation. The LLM never writes SQL here.
- Live sys.* catalog queries are hardcoded inside each tool.
- Real values flow forward. Step N+1 receives literal data from
  step N's tool output, never the model's memory of it.
- Bounded. Step cap of 8. One replan attempt on failure. Then bail.
- Fully logged to logs/planner.log — the production SQL path
  currently has zero logging, and an agent loop needs it.
"""

import logging
import logging.handlers
import re
from pathlib import Path

import pyodbc

import config

# ---------------------------------------------------------------------------
# Logging setup. This module owns its own rotating file handler because
# production code currently logs nothing (SQL_BRANCH_MAP §4).
# ---------------------------------------------------------------------------

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)

log = logging.getLogger("planner")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "planner.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(_h)
    log.propagate = False

# ---------------------------------------------------------------------------
# Schema denylist for enumeration tools.
# Reads config.PLANNER_SCHEMA_DENYLIST (see INTEGRATION.md).
# Falls back to a sensible default if the config attr isn't set.
# ---------------------------------------------------------------------------

SCHEMA_DENYLIST: set[str] = set(
    getattr(config, "PLANNER_SCHEMA_DENYLIST", None)
    or {"sys", "INFORMATION_SCHEMA", "guest"}
)

# ---------------------------------------------------------------------------
# DB connection helper. One connection per turn, reused across all steps.
# Matches the connection params used by sql_generator.execute_sql().
# ---------------------------------------------------------------------------

def _open_connection() -> pyodbc.Connection:
    # Reuse sql_generator's read-only-login-aware connection string so the
    # enumerate path uses the same dedicated login as the direct path (audit #3).
    import sql_generator
    conn = pyodbc.connect(sql_generator._build_conn_str(), timeout=30)
    conn.timeout = 30
    return conn

# ---------------------------------------------------------------------------
# Tool implementations.
#
# Every tool takes (conn, **kwargs) and returns a JSON-serializable dict.
# The SQL inside each tool is hardcoded and parameterized. The LLM never
# writes it — it can only pick a tool name and supply args from a
# structured schema.
# ---------------------------------------------------------------------------

def _tool_list_tables(conn: pyodbc.Connection, schema: str) -> dict:
    """All tables in a given schema."""
    if schema in SCHEMA_DENYLIST:
        return {"schema": schema, "tables": [], "note": "schema is denylisted"}
    cur = conn.cursor()
    cur.execute(
        "SELECT t.name "
        "FROM sys.tables t "
        "JOIN sys.schemas s ON t.schema_id = s.schema_id "
        "WHERE s.name = ? "
        "ORDER BY t.name",
        schema,
    )
    tables = [r[0] for r in cur.fetchall()]
    return {"schema": schema, "tables": tables}


# ---------------------------------------------------------------------------
# Scope resolution — 'direct' vs 'enumerate'.
#
# This replaces the old LLM single_step/multi_step classifier, which conflated
# two orthogonal axes:
#   - COMPUTATION (SUM / AVG / COUNT / GROUP BY / TOP N / "per" / "most") — a
#     single SQL statement can always express this.
#   - SCOPE (one identifiable table vs. a table set that must be discovered).
# The old classifier read aggregation phrasing as evidence of complexity and
# misrouted 17/20 single-statement aggregate questions into the fan-out planner,
# whose fixed tools cannot aggregate column values. See SQL_BRANCH_MAP / the
# regression analysis.
#
# The gate is now DETERMINISTIC — no probabilistic LLM call — because the real
# signal is structural. 'enumerate' fires ONLY on an explicit schema-wide table
# cue with a resolvable schema name. Everything else (all aggregation, all
# DataDictionary-catalog questions, all named-table questions) is 'direct' and
# flows to the existing, more-capable NL->SQL generator. Novel enumerate
# phrasings that slip the regex degrade safely to 'direct' rather than into a
# capability dead-end.
# ---------------------------------------------------------------------------

# "the DataDictionary" is a specific curated TABLE ([rpt].[DataDictionary], one
# row per tracked column). Questions about it are single-statement GROUP BY /
# COUNT queries against that one table — never live-schema enumeration. This
# override must run FIRST because such questions often also contain an
# enumerate-looking cue ("across all tables in the DataDictionary").
_DATA_DICTIONARY_RE = re.compile(r"\bdata\s*dictionary\b", re.IGNORECASE)

# Column-oriented questions live in DataDictionary / catalog territory and are
# single-statement; keep them out of the row-count enumerate path.
_COLUMN_RE = re.compile(r"\bcolumns?\b", re.IGNORECASE)

# Explicit schema-wide table cue: the question is about a SET of tables that
# must be enumerated, not one named table.
_ENUMERATE_CUE_RE = re.compile(
    r"\b(across (all )?tables|each table|every table|all tables in|"
    r"tables in (the )?schema|tables (with|in) the schema|which tables?|"
    r"in schema)\b",
    re.IGNORECASE,
)

# Resolve the target schema name from "schema X", "schema [X]", or "in the X schema".
_SCHEMA_AFTER_RE = re.compile(r"\bschema\s+\[?([A-Za-z_][A-Za-z0-9_]*)\]?", re.IGNORECASE)
_SCHEMA_BEFORE_RE = re.compile(
    r"\b(?:in|from|within)\s+(?:the\s+)?\[?([A-Za-z_][A-Za-z0-9_]*)\]?\s+schema\b",
    re.IGNORECASE,
)


def _extract_schema(question: str) -> str | None:
    m = _SCHEMA_AFTER_RE.search(question) or _SCHEMA_BEFORE_RE.search(question)
    return m.group(1) if m else None


def is_catalog_question(question: str) -> bool:
    """True if the question is about the DataDictionary catalog table.

    Single source of truth for 'this targets [rpt].[DataDictionary]', shared by
    resolve_scope (routing) and sql_generator.get_schema_context (which pins the
    catalog's authoritative schema instead of trusting vector retrieval)."""
    return bool(_DATA_DICTIONARY_RE.search(question))


def resolve_scope(question: str) -> tuple[str, str | None]:
    """Decide how to answer a SQL question.

    Returns (scope, schema):
      - ('direct', None)       -> answerable by one SQL statement against
                                  identifiable table(s); use the existing
                                  NL->SQL path. This is the default.
      - ('enumerate', schema)  -> requires discovering the tables in `schema`
                                  first; use the compose path
                                  (run_enumerate_pipeline).

    Deterministic, highest-precision-first:
      1. Mentions the DataDictionary catalog table  -> direct.
      2. Column-oriented question                   -> direct (catalog/DD territory).
      3. Explicit schema-wide table cue + resolvable schema -> enumerate.
      4. Otherwise                                  -> direct.
    """
    if is_catalog_question(question):
        log.info(f"scope: q={question!r} -> direct (DataDictionary catalog table)")
        return "direct", None

    if _ENUMERATE_CUE_RE.search(question) and not _COLUMN_RE.search(question):
        schema = _extract_schema(question)
        if schema:
            log.info(f"scope: q={question!r} -> enumerate (schema={schema})")
            return "enumerate", schema
        log.info(f"scope: q={question!r} -> direct (enumerate cue but no schema resolved)")
        return "direct", None

    log.info(f"scope: q={question!r} -> direct")
    return "direct", None

# ---------------------------------------------------------------------------
# Compose pipeline: enumerate -> build ONE UNION-ALL query -> execute -> answer.
#
# This is the 'enumerate' branch of the composed architecture. Scope resolution
# has already decided the question needs a discovered table set in `schema`.
# The steps are DETERMINISTIC where correctness matters:
#   1. enumerate the user tables in `schema` (catalog query, no LLM),
#   2. build ONE per-table row-count UNION-ALL query as a string in Python
#      (no LLM writes SQL — table names come from sys.tables, not user input),
#   3. execute it (one round trip; the DB does the arithmetic),
#   4. hand the small, clean per-table result set to the existing answer LLM,
#      which shapes the final sentence and does the trivial sum/argmax the
#      question asks for — reliable over a handful of rows, unlike summing a
#      free-form JSON tool trace.
#
# The public return tuple mirrors sql_generator.run_sql_pipeline():
#   (answer, sql_used, rows, columns)
# ---------------------------------------------------------------------------

# sys.tables names are trusted (they came from the catalog, not the user), but
# we still bracket-escape defensively so a table named with a ']' can't break
# out of the identifier quoting.
def _bracket(ident: str) -> str:
    return "[" + ident.replace("]", "]]") + "]"


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _list_user_tables(conn: pyodbc.Connection, schema: str) -> list[str]:
    """Concrete list of user tables in `schema`, via the catalog. Deterministic."""
    return _tool_list_tables(conn, schema)["tables"]


def _build_union_count_sql(schema: str, tables: list[str]) -> str:
    """One query returning (TableName, RowCount) per table, ordered desc.

    COUNT_BIG(*) is exact (unlike sys.partitions estimates) and safe for the
    aggregation the answer step may do (total across schema, biggest table)."""
    db = config.DB_DATABASE
    selects = [
        f"SELECT {_sql_literal(t)} AS TableName, "
        f"COUNT_BIG(*) AS [RowCount] "
        f"FROM {_bracket(db)}.{_bracket(schema)}.{_bracket(t)}"
        for t in tables
    ]
    union = " UNION ALL ".join(selects)
    return (
        f"SELECT TableName, [RowCount] "
        f"FROM (\n  {union}\n) AS per_table "
        f"ORDER BY [RowCount] DESC"
    )


def _execute(conn: pyodbc.Connection, sql: str) -> tuple[list[dict], list[str]]:
    cur = conn.cursor()
    cur.execute(sql)
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, r)) for r in cur.fetchmany(1000)]
    return rows, columns


def run_enumerate_pipeline(question: str, schema: str) -> tuple:
    """Compose path for a schema-wide question. See module comment above.

    Returns (answer, sql_used, rows, columns), matching run_sql_pipeline so
    the chat.py call site is unchanged. `sql_used` is the real UNION-ALL query
    (prefixed with an '-- enumerate' comment) for transparency, so /sources and
    the history entry show exactly what ran.
    """
    import sql_generator  # deferred: avoids circular import at module load

    log.info(f"=== enumerate start: q={question!r} schema={schema!r} ===")
    conn = None
    try:
        conn = _open_connection()
        tables = _list_user_tables(conn, schema)
        log.info(f"enumerate: schema={schema} -> {len(tables)} tables: {tables}")
        if not tables:
            return (
                f"I couldn't find any tables in schema '{schema}'.",
                f"-- enumerate: schema={schema}, 0 tables",
                [], [],
            )

        core_sql = _build_union_count_sql(schema, tables)
        sql_used = f"-- enumerate: schema={schema}, {len(tables)} tables\n{core_sql}"

        # Defense-in-depth: the same SELECT-only guard the single-step path uses.
        valid, reason = sql_generator.validate_sql(core_sql)
        if not valid:
            log.error(f"enumerate: built SQL failed validation: {reason}")
            return (
                f"I built a query to enumerate schema '{schema}' but it failed "
                f"safety validation ({reason}).",
                sql_used, [], [],
            )

        rows, columns = _execute(conn, core_sql)
        log.info(f"enumerate: {len(rows)} per-table rows")
    except Exception as e:
        log.error(f"enumerate: exception {e!r}")
        return (
            f"I hit an error enumerating schema '{schema}': {e}",
            f"-- enumerate: schema={schema} (failed)",
            [], [],
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Final NL shaping + trivial arithmetic over the small clean result set.
    results_context = sql_generator.format_results_for_llm(rows, columns, core_sql)
    answer = sql_generator.generate_answer(question, results_context)
    log.info(f"=== enumerate end: {answer[:160]!r} ===")
    return (answer, sql_used, rows, columns)
# sql_generator.py
# Handles the SQL generation and execution path.
#
# Flow:
#   1. Retrieve relevant schema context from DataDictionary (Qdrant)
#   2. LLM generates a SELECT statement
#   3. Validate — reject anything that isn't a clean SELECT
#   4. Execute against SQL Server
#   5. Return results + the generated SQL for transparency

import logging
import logging.handlers
import re
from pathlib import Path

import pyodbc
from qdrant_client.models import ScoredPoint

import config
import retriever
import llm

# Module logger -> logs/sql_generator.log (mirrors planner.py). Operational notices
# like the read-only-login warning go here, NOT to the console — printing them to
# stderr interleaved them into interactive answers mid-stream.
_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("sql_generator")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "sql_generator.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)
    log.propagate = False

# ── SQL generation prompt ─────────────────────────────────────────────────────

_SQL_SYSTEM = """\
You are a SQL Server expert generating T-SQL SELECT queries for Fraser Health Authority.
You will be given schema context from the internal Data Dictionary describing relevant
tables and columns. Use that context to write a correct, efficient SELECT query.

STRICT RULES:
- Output ONLY the SQL query — no explanation, no markdown, no backticks, no preamble.
- Only write SELECT statements. Never write INSERT, UPDATE, DELETE, DROP, EXEC, or any
  DDL/DML that modifies data.
- Always use fully qualified table names: [DatabaseName].[SchemaName].[TableName]
- TABLE SELECTION: the schema context marks either one PRIMARY TABLE (the best
  match — plus optional SECONDARY tables) or several CANDIDATE TABLES. If a PRIMARY
  TABLE is given, query it. If only CANDIDATE TABLES are given, choose the SINGLE
  table whose name and description best fit the question and query only that one.
  Do NOT JOIN tables unless the question explicitly requires combining data from
  more than one. Aggregation on that single table — GROUP BY, COUNT, SUM, AVG,
  TOP N — is expected wherever the question calls for it.
- JOINS: when a table block lists "Joins (foreign keys)", those predicates are the
  real relationships — use them verbatim when the question spans tables. Chain them
  for multi-hop paths when no direct key exists (e.g. Systems -> Assets ->
  LineageMappings). NEVER invent a join condition that is not listed; if the tables
  you need cannot be connected by the listed keys, output INSUFFICIENT_SCHEMA.
- COUNT QUESTIONS: a "how many", "how much", "number of", or "count of" question
  returns a count, not raw column values. If it groups ("per X" / "by X" / "each
  X") write SELECT <group column>, COUNT(*) ... GROUP BY <group column> (group
  column FIRST); otherwise return a single COUNT(*) or SUM(...).
  "how many of those failed" -> SELECT COUNT(*) ... WHERE <failed condition>.
- CATEGORY BREAKDOWN: when the question asks which/what records match a condition AND
  what category/type they are (e.g. "which assets have no description and what types
  are they"), return the breakdown by that category: SELECT <category>, COUNT(*) ...
  GROUP BY <category> — not a raw row dump.
- RATIO/PERCENTAGE: any proportion, percentage, ratio, or "how complete / what
  coverage" figure MUST cast to float to avoid integer division — e.g. SELECT 100.0
  * SUM(CASE WHEN <condition> THEN 1 ELSE 0 END) / COUNT(*) AS Pct FROM <X>. Never
  write integer SUM(...)/COUNT(*) (it truncates to 0).
- TOP 100: add it ONLY when returning raw rows with no aggregation.
  NEVER add TOP when the query contains COUNT, SUM, AVG, MIN, MAX, or GROUP BY.
  Aggregates produce their own natural result set — TOP would be wrong there.
- ORDER BY WITH TOP: when the question asks for the "most recent", "latest",
  "newest"/"oldest", "top/bottom N", "highest", or "lowest" rows, the TOP N MUST be
  paired with an ORDER BY on the relevant column in the correct direction
  (most recent / newest → ORDER BY <datetime> DESC; lowest → ORDER BY <col> ASC).
  TOP without ORDER BY returns an arbitrary set of rows and is wrong for these.
- NULLABLE ORDER BY: when ordering ascending to find the lowest/worst/minimum on a
  nullable column, add WHERE <col> IS NOT NULL so NULLs (sorted first) don't fill
  the top rows.
- Use meaningful column aliases for calculated fields (e.g. COUNT(*) AS TotalCount).
- T-SQL BOOLEAN RULE: SQL Server does NOT support boolean expressions as SELECT column
  values. NEVER write: SELECT ColumnA > 0 AS SomeAlias — this is invalid T-SQL.
  Instead use WHERE to filter: SELECT COUNT(*) AS SomeCount FROM ... WHERE ColumnA > 0
  Or use CASE: SELECT CASE WHEN ColumnA > 0 THEN 'Yes' ELSE 'No' END AS SomeAlias
- WHERE FILTER RULE: When the question asks to show, find, list, or count records
  matching a specific condition (e.g. "failed jobs", "active rules", "results with
  failed rows greater than zero"), you MUST include a WHERE clause. Never return all
  rows when the question clearly asks for a subset.
  Examples:
    "show me failed jobs" → WHERE status = 'failed'
    "how many results have failed rows greater than zero" → WHERE FailedRows > 0
    "active rules" → WHERE IsActive = 1
- DATADICTIONARY TABLE: When the question mentions "DataDictionary", "data dictionary",
  "tracked tables", "tracked columns", or asks about schemas/tables/columns across the
  system, use the table [MetadataRepository].[rpt].[DataDictionary]. Key columns:
  DatabaseName, SchemaName, ObjectName, ColumnName, DataType, is_nullable, is_identity,
  ColumnDescription, ObjectDescription. Do NOT use mdm.Columns, stg.SqlColumns, or
  any other table when the question is about the DataDictionary catalog itself.
  For example PassRate = 0.8 means 80%. Always check the column description in the
  schema context. If a column stores fractions (0.0 to 1.0), use the decimal form in
  WHERE clauses: "below 80%" → WHERE PassRate < 0.8, NOT WHERE PassRate < 80.
- If a previous SQL query is provided as context, and the user's question is a
  follow-up (uses words like "those", "that table", "of those", "same"), base your
  new query on the same table(s) from the previous SQL.
- If the schema context does not contain enough information to answer the question,
  output exactly: INSUFFICIENT_SCHEMA
"""


# ── Fix A: authoritative schema for the DataDictionary catalog table ──────────
#
# "DataDictionary" questions have exactly ONE correct source: [rpt].[DataDictionary].
# Vector retrieval reliably FAILS to surface it — the catalog's own chunks don't
# embed near phrases like "data type" or "identity column", so semantically
# adjacent tables (stg.SqlColumns, mdm.Columns, dm_dq.*) outscore it and the
# generator queries the wrong table. We pin this hand-authored, verified schema
# instead of searching. Column names are the real T-SQL columns of the catalog
# (the SELECT * source of config.INGEST_QUERY); Q1/Q18 already prove them correct.
_DATADICTIONARY_SCHEMA = """\
PRIMARY TABLE (this is the authoritative source — use ONLY this table):
Table: [MetadataRepository].[rpt].[DataDictionary]
  Description: The Data Dictionary catalog. ONE ROW PER TRACKED COLUMN — each row
    describes a single column of a tracked table in the MetadataRepository database.
    To count TABLES use COUNT(DISTINCT ObjectName). To count COLUMNS, count rows.
    Group by SchemaName for per-schema stats; group by DataType for data-type stats.
  Columns:
    - DatabaseName (nvarchar, NOT NULL) — database the tracked column belongs to
    - SchemaName (nvarchar, NOT NULL) — schema of the tracked table
    - ObjectName (nvarchar, NOT NULL) — name of the tracked TABLE; COUNT(DISTINCT ObjectName) = number of tables
    - ObjectTypeDesc (nvarchar, NULL) — type of the tracked object (e.g. USER_TABLE)
    - ObjectDescription (nvarchar, NULL) — description of the tracked table
    - ColumnName (nvarchar, NOT NULL) — name of the tracked column
    - ColumnOrder (int, NOT NULL) — ordinal position of the column in its table
    - DataType (nvarchar, NOT NULL) — SQL data type of the tracked column
    - max_length (int, NULL)
    - precision (int, NULL)
    - scale (int, NULL)
    - is_nullable (bit, NOT NULL) — 1 if the tracked column is nullable, else 0
    - is_identity (bit, NOT NULL) — 1 if the tracked column is an identity column, else 0
    - ColumnDescription (nvarchar, NULL) — description of the tracked column (NULL if undocumented)
"""


# ── Foreign keys ──────────────────────────────────────────────────────────────
# Vector search surfaces columns, never relationships, so the generator had to
# guess join keys from column names -- and multi-hop paths (Systems -> Assets ->
# LineageMappings) were undiscoverable. Load the real FK graph once and render the
# edges touching each table as ready-to-use join predicates.

_FK_CACHE: dict | None = None

_FK_QUERY = """
SELECT ps.name AS pschema, pt.name AS ptable, pc.name AS pcol,
       rs.name AS rschema, rt.name AS rtable, rc.name AS rcol
FROM sys.foreign_key_columns fkc
JOIN sys.objects pt ON pt.object_id = fkc.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.objects rt ON rt.object_id = fkc.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
"""


def foreign_keys() -> dict:
    """{(schema, table): ["a.b.col = c.d.col", ...]} -- every FK edge touching a
    table, in both directions, phrased as a join predicate. Cached per process;
    returns {} if the database is unreachable so schema context still renders."""
    global _FK_CACHE
    if _FK_CACHE is not None:
        return _FK_CACHE
    cache: dict = {}
    try:
        rows, _ = execute_sql(_FK_QUERY)
    except Exception as e:
        log.warning("foreign keys unavailable (%s) -- joins fall back to name matching", e)
        _FK_CACHE = {}
        return _FK_CACHE
    for r in rows:
        pred = (f"{r['pschema']}.{r['ptable']}.{r['pcol']}"
                f" = {r['rschema']}.{r['rtable']}.{r['rcol']}")
        cache.setdefault((r["pschema"], r["ptable"]), []).append(pred)
        cache.setdefault((r["rschema"], r["rtable"]), []).append(pred)
    _FK_CACHE = cache
    return _FK_CACHE


# ── Schema retrieval for SQL context ─────────────────────────────────────────

def get_schema_context(question: str, last_sql: str | None = None, clearance=None) -> tuple[str, list]:
    """
    Retrieves relevant table/column descriptions from the DataDictionary.

    If last_sql is provided and the question looks like a follow-up (contains
    vague pronouns), extracts the table from last_sql and fetches ALL columns
    for that table directly — bypassing vector search entirely.
    This ensures follow-up questions always have the full schema of the right table.

    `clearance` gates the Qdrant retrieval deny-by-default (audit #7).
    """
    import re as _re
    from planner import is_catalog_question, is_description_audit_question

    # Fix A — DataDictionary catalog questions have one authoritative table.
    # Pin its verified schema and skip vector search, which mis-retrieves here.
    # Description-audit questions (which tables/columns lack descriptions, how
    # complete per schema) are also catalog questions answered against this table.
    if is_catalog_question(question) or is_description_audit_question(question):
        return _DATADICTIONARY_SCHEMA, []

    _FOLLOWUP = _re.compile(
        r"\b(those|that|it|same|of those|of them|in that|in those|from those)\b",
        _re.IGNORECASE,
    )

    # Extract schema.table from last_sql if it's a follow-up question
    if last_sql and _FOLLOWUP.search(question):
        # Match [DB].[Schema].[Table] or [DB]..[Table] patterns
        table_match = _re.search(
            r"\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]"
            r"|\[([^\]]+)\]\.\.\[([^\]]+)\]",
            last_sql,
        )
        if table_match:
            if table_match.group(1):  # [DB].[Schema].[Table]
                db, schema, table = table_match.group(1), table_match.group(2), table_match.group(3)
            else:                     # [DB]..[Table]
                db, schema, table = table_match.group(4), None, table_match.group(5)

            # Fetch ALL columns for this table from Qdrant
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            from qdrant_client import QdrantClient
            import config as _config

            qc = QdrantClient(
                host=_config.QDRANT_HOST, port=_config.QDRANT_PORT,
                grpc_port=_config.QDRANT_GRPC_PORT, prefer_grpc=True,
            )
            must = [
                retriever._clearance_condition(clearance),  # deny-by-default (audit #7)
                FieldCondition(key="object_name", match=MatchValue(value=table)),
            ]
            if schema:
                must.append(FieldCondition(key="schema_name", match=MatchValue(value=schema)))

            points, _ = qc.scroll(
                collection_name=_config.COLLECTION_NAME,
                scroll_filter=Filter(must=must),
                limit=200,
                with_payload=True,
                with_vectors=False,
            )

            if points:
                # Render full schema for this table
                cols = sorted(
                    [p.payload for p in points],
                    key=lambda p: p.get("column_order", 0)
                )
                sample = cols[0]
                lines = [
                    f"Table: [{sample.get('database_name', db)}]"
                    f".[{sample.get('schema_name', schema or '')}]"
                    f".[{sample.get('object_name', table)}]",
                    f"  Description: {sample.get('object_description', '')}",
                    "  Columns (ALL columns — use these for your query):",
                ]
                for col in cols:
                    nullable = "NULL" if col.get("is_nullable") else "NOT NULL"
                    identity = " IDENTITY" if col.get("is_identity") else ""
                    lines.append(
                        f"    - {col.get('column_name')} "
                        f"({col.get('data_type')}{identity}, {nullable})"
                        f" — {col.get('column_description', '')}"
                    )
                return "\n".join(lines), []

    # Standard retrieval (Fix B). Pure cosine rank surfaces semantically-adjacent
    # but WRONG tables (e.g. stg.SqlColumns for a DataDictionary question) and gives
    # the generator no authority signal. The one reliable signal is a NAME MATCH: if
    # the question names a table, that table is almost certainly the source. So:
    #   - name match  -> elevate that table to PRIMARY with its FULL schema (so the
    #                    right table is fully specified), others for reference.
    #   - no match    -> fall back to the original behaviour (all retrieved tables,
    #                    matched columns, vector order) which the model reasons over
    #                    well. Crucially we do NOT force a primary or expand wrong
    #                    tables to full schema here — doing so let the model ration
    #                    -alise the wrong near-tied table (scan_queue vs ScanControl).
    results, _ = retriever.retrieve(question, top_k=10, clearance=clearance)
    if not results:
        return "", results


    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for hit in results:
        p = hit.payload
        key = (p["schema_name"], p["object_name"])
        if key not in grouped:
            grouped[key] = {
                "database":    p["database_name"],
                "schema":      p["schema_name"],
                "table":       p["object_name"],
                "description": p["object_description"],
                "matched":     [],
            }
            order.append(key)
        grouped[key]["matched"].append({
            "name":        p["column_name"],
            "type":        p["data_type"],
            "nullable":    p["is_nullable"],
            "identity":    p["is_identity"],
            "description": p["column_description"],
        })

    # Lexical name match: does the question name this table? Normalise both by
    # stripping non-alphanumerics so "rule targets" matches "RuleTargets" and
    # "data quality result" matches "Results" (singular) — while "Rules" does
    # NOT false-match, since normalised "rules" isn't a substring of the question.
    q_norm = _re.sub(r"[^a-z0-9]", "", question.lower())

    def _name_match(tbl: str) -> bool:
        n = _re.sub(r"[^a-z0-9]", "", tbl.lower())
        if not n:
            return False
        forms = {n}
        if n.endswith("ies"):
            forms.add(n[:-3] + "y")
        if n.endswith("es") and n[:-2] and n[:-2][-1] in "sxzh":  # batches->batch, processes->process
            forms.add(n[:-2])
        if n.endswith("s"):
            forms.add(n[:-1])
        return any(f in q_norm for f in forms)

    def _cols_full(meta: dict) -> list[dict]:
        full = retriever.fetch_all_columns(meta["schema"], meta["table"], clearance=clearance)
        if not full:
            return meta["matched"]
        return [
            {
                "name":        c.get("column_name"),
                "type":        c.get("data_type"),
                "nullable":    c.get("is_nullable"),
                "identity":    c.get("is_identity"),
                "description": c.get("column_description", ""),
            }
            for c in sorted(full, key=lambda c: c.get("column_order", 0))
        ]

    def _render(meta: dict, cols: list[dict], role: str | None) -> str:
        lines = []
        if role:
            lines.append(role)
        lines.append(f"Table: [{meta['database']}].[{meta['schema']}].[{meta['table']}]")
        lines.append(f"  Description: {meta['description']}")
        fks = foreign_keys().get((meta["schema"], meta["table"]))
        if fks:
            lines.append("  Joins (foreign keys — use these exact conditions):")
            for pred in sorted(set(fks)):
                lines.append(f"    - {pred}")
        lines.append("  Columns:")
        for col in cols:
            nullable = "NULL" if col["nullable"] else "NOT NULL"
            identity = " IDENTITY" if col["identity"] else ""
            lines.append(
                f"    - {col['name']} ({col['type']}{identity}, {nullable})"
                f" — {col['description']}"
            )
        return "\n".join(lines)

    name_matched = [k for k in order if _name_match(grouped[k]["table"])]

    if name_matched:
        primary = name_matched[0]
        blocks = [_render(
            grouped[primary], _cols_full(grouped[primary]),
            "PRIMARY TABLE (best match — query this unless the question requires another):",
        )]
        for key in order:
            if key == primary:
                continue
            blocks.append(_render(grouped[key], grouped[key]["matched"], "OTHER TABLE (for reference):"))
        return "\n\n".join(blocks), results

    # No name match — original behaviour: all retrieved tables, matched columns.
    blocks = [_render(grouped[key], grouped[key]["matched"], None) for key in order]
    return "\n\n".join(blocks), results


# ── SQL generation ────────────────────────────────────────────────────────────

def _build_sql_prompt(question: str, schema_context: str, last_sql: str | None = None) -> str:
    parts = [f"Schema context from Data Dictionary:\n\n{schema_context}\n\n---\n\n"]
    if last_sql:
        parts.append(
            f"Previous SQL query (user may be asking a follow-up about the same table):\n"
            f"{last_sql}\n\n---\n\n"
        )
    parts.append(f"Generate a T-SQL SELECT query to answer this question:\n{question}")
    return "".join(parts)


def generate_sql(question: str, schema_context: str, last_sql: str | None = None) -> str:
    """
    Calls the LLM to generate a T-SQL SELECT query.
    Returns the raw SQL string or 'INSUFFICIENT_SCHEMA'.
    """
    sql = llm.reason(
        [
            {"role": "system", "content": _SQL_SYSTEM},
            {"role": "user",   "content": _build_sql_prompt(question, schema_context, last_sql)},
        ],
        temperature=0,
    )

    # Strip markdown fences if the model adds them despite instructions
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()


def repair_sql(question: str, schema_context: str, bad_sql: str, error: str) -> str:
    """Fix C — one-shot self-correction. Feeds a failed query and its SQL Server
    error back to the model for a single corrected attempt. Catches malformed
    aggregates (ORDER BY COUNT() without GROUP BY), alias-binding errors, and
    similar syntax faults that survive generation. Returns the corrected SQL."""
    sql = llm.reason(
        [
            {"role": "system", "content": _SQL_SYSTEM},
            {"role": "user", "content": (
                f"Schema context from Data Dictionary:\n\n{schema_context}\n\n---\n\n"
                f"The following T-SQL query failed to execute. Fix it and output ONLY "
                f"the corrected SELECT query (no explanation, no markdown).\n\n"
                f"Question: {question}\n\n"
                f"Failed query:\n{bad_sql}\n\n"
                f"SQL Server error:\n{error}"
            )},
        ],
        temperature=0,
    )
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()


# ── Ordering-determinism guard (Q6) ───────────────────────────────────────────
#
# A superlative / ranked-subset question ("the 5 most recent jobs", "results with
# the lowest pass rate") must pair TOP N with an ORDER BY, or TOP returns an
# ARBITRARY N rows — a silent wrong answer (right count, wrong rows). The 8B
# occasionally drops the ORDER BY, so we detect that exact defect deterministically
# (ordering intent in the question + TOP-without-ORDER-BY in the SQL) and repair it.
_ORDERING_INTENT_RE = re.compile(
    r"\b(most recent|recent|latest|newest|oldest|earliest|highest|lowest|"
    r"top\s+\d+|bottom\s+\d+|first\s+\d+|last\s+\d+|largest|smallest|"
    r"biggest|greatest|worst|best)\b",
    re.IGNORECASE,
)
_HAS_TOP_RE = re.compile(r"\bTOP\b", re.IGNORECASE)
_HAS_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


def _top_without_order_by(sql: str) -> bool:
    return bool(_HAS_TOP_RE.search(sql)) and not _HAS_ORDER_BY_RE.search(sql)


def repair_missing_order_by(question: str, schema_context: str, sql: str) -> str:
    """Q6 guard repair. The query uses TOP N to answer a superlative question but has
    no ORDER BY, so its rows are arbitrary. Ask the model to add the correct ORDER BY
    (column + direction), changing nothing else. Returns the corrected SQL."""
    out = llm.reason(
        [
            {"role": "system", "content": _SQL_SYSTEM},
            {"role": "user", "content": (
                f"Schema context from Data Dictionary:\n\n{schema_context}\n\n---\n\n"
                f"The query below answers a question that asks for a ranked subset "
                f"(e.g. most recent / highest / lowest), but it uses TOP without an "
                f"ORDER BY, so it returns an arbitrary set of rows. Add an ORDER BY on "
                f"the appropriate column in the correct direction (most recent / newest "
                f"→ a datetime column DESC; lowest → that column ASC). Keep everything "
                f"else the same. Output ONLY the corrected SELECT query.\n\n"
                f"Question: {question}\n\nQuery:\n{sql}"
            )},
        ],
        temperature=0,
    )
    out = re.sub(r"^```(?:sql)?\s*", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s*```$", "", out)
    return out.strip()


_ASC_SUPERLATIVE_RE = re.compile(r"\b(lowest|worst|least|minimum|smallest|fewest|bottom)\b", re.IGNORECASE)
_ORDER_ASC_COL_RE = re.compile(r"ORDER\s+BY\s+(\[?[\w.]+\]?)\s+ASC", re.IGNORECASE)


def _guard_null_order(question: str, sql: str) -> str:
    # "lowest/worst by <nullable col>" ordered ASC returns NULLs first -> empty top
    # rows. Add a NULL guard deterministically; skip GROUP BY/HAVING to stay safe.
    if not _ASC_SUPERLATIVE_RE.search(question):
        return sql
    m = _ORDER_ASC_COL_RE.search(sql)
    if not m:
        return sql
    col = m.group(1)
    if re.search(re.escape(col) + r"\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE):
        return sql
    idx = sql.upper().rfind("ORDER BY")
    head, tail = sql[:idx], sql[idx:]
    if re.search(r"\b(GROUP\s+BY|HAVING)\b", head, re.IGNORECASE):
        return sql
    kw = "AND" if re.search(r"\bWHERE\b", head, re.IGNORECASE) else "WHERE"
    candidate = f"{head.rstrip()}\n{kw} {col} IS NOT NULL\n{tail}"
    ok, _ = validate_sql(candidate)
    return candidate if ok else sql


# ── Validation ────────────────────────────────────────────────────────────────

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|EXEC|EXECUTE|MERGE|"
    r"GRANT|REVOKE|DENY|BULK|INTO|WAITFOR|OPENQUERY|OPENROWSET|OPENDATASOURCE|"
    r"xp_|sp_)\b",
    re.IGNORECASE,
)

def validate_sql(sql: str) -> tuple[bool, str]:
    """
    Allowlist validation: the query must be a SINGLE read-only statement — a plain
    SELECT or a WITH...SELECT CTE — with no data-modifying, external-data, or
    stalling constructs. Returns (is_valid, reason).
    """
    if sql == "INSUFFICIENT_SCHEMA":
        return False, "insufficient_schema"

    stripped = sql.strip()
    if not stripped:
        return False, "empty query"

    # Single statement only: allow one optional trailing ';', reject anything
    # after it (stacked queries like "SELECT 1; SELECT ... INTO backdoor").
    core = stripped.rstrip().rstrip(";").rstrip()
    if ";" in core:
        return False, "multiple statements are not allowed"

    # Must be a read-only SELECT, or a WITH...SELECT CTE.
    upper = core.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, f"query must start with SELECT or WITH: {core[:60]}"
    if upper.startswith("WITH") and not re.search(r"\bSELECT\b", upper):
        return False, "WITH clause without a SELECT"

    match = _FORBIDDEN.search(core)
    if match:
        return False, f"forbidden construct: {match.group().upper()}"

    return True, "ok"


# ── Execution ─────────────────────────────────────────────────────────────────

_warned_trusted = False

def _build_conn_str() -> str:
    base = (
        f"DRIVER={{{config.DB_DRIVER}}};"
        f"SERVER={config.DB_SERVER};"
        f"DATABASE={config.DB_DATABASE};"
        "ApplicationIntent=ReadOnly;"  # AlwaysOn routing hint, NOT a permission
    )
    if config.DB_READONLY_USER and config.DB_READONLY_PASSWORD:
        return base + f"UID={config.DB_READONLY_USER};PWD={config.DB_READONLY_PASSWORD};"

    global _warned_trusted
    if not _warned_trusted:
        log.warning(
            "DB_READONLY_USER not set -- executing generated SQL under the service's "
            "Windows identity, not a read-only login. The validator is the only barrier. "
            "See sql/create_readonly_login.sql (audit #3)."
        )
        _warned_trusted = True
    return base + "Trusted_Connection=yes;"


def execute_sql(sql: str) -> tuple[list[dict], list[str]]:
    """
    Executes a validated SELECT against SQL Server.
    Returns (rows, columns) where rows is a list of dicts.
    Caps at 100 rows and enforces a 30-second timeout.
    """
    conn = pyodbc.connect(_build_conn_str(), timeout=30)
    conn.timeout = 30

    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description]
        rows = []
        for row in cursor.fetchmany(100):  # hard cap at 100 rows
            rows.append(dict(zip(columns, row)))
        return rows, columns
    finally:
        conn.close()


# ── Result formatting ─────────────────────────────────────────────────────────

def format_results_for_llm(rows: list[dict], columns: list[str], sql: str) -> str:
    """
    Renders SQL results into a compact context block for the answer LLM.
    """
    if not rows:
        return f"Query executed successfully but returned no results.\nSQL: {sql}"

    lines = [f"Query results ({len(rows)} row{'s' if len(rows) != 1 else ''}):"]
    lines.append("  |  ".join(columns))
    lines.append("-" * 80)
    for row in rows:
        lines.append("  |  ".join(str(row[c]) if row[c] is not None else "NULL" for c in columns))

    if len(rows) == 100:
        lines.append("(results capped at 100 rows)")

    lines.append(f"\nSQL executed: {sql}")
    return "\n".join(lines)


def format_results_table(rows: list[dict], columns: list[str]) -> str:
    """
    Renders a human-readable ASCII table for the terminal.
    """
    if not rows:
        return "(no results)"

    # Calculate column widths
    widths = {c: max(len(c), max(len(str(r[c]) if r[c] is not None else "NULL") for r in rows))
              for c in columns}

    sep = "+" + "+".join("-" * (widths[c] + 2) for c in columns) + "+"
    header = "|" + "|".join(f" {c:<{widths[c]}} " for c in columns) + "|"

    lines = [sep, header, sep]
    for row in rows:
        lines.append("|" + "|".join(
            f" {str(row[c]) if row[c] is not None else 'NULL':<{widths[c]}} "
            for c in columns
        ) + "|")
    lines.append(sep)
    if len(rows) == 100:
        lines.append("(capped at 100 rows)")
    return "\n".join(lines)


# ── Natural language answer ───────────────────────────────────────────────────

_ANSWER_SYSTEM = """\
You are a data governance assistant for Fraser Health Authority.
You have just executed a SQL query against the FHA database and received results.
Answer the user's question in clear, natural language using the query results.
Be concise and precise. If the result is a single number, state it directly.
If it is a table of results, summarize the key findings and mention notable values.
Do not reproduce the full table in your answer unless it is very small (5 rows or fewer).
Always mention what the query was counting or selecting so the answer is unambiguous.
"""

def generate_answer(question: str, results_context: str) -> str:
    """
    Generates a natural language answer from the SQL results.
    """
    return llm.reason(
        [
            {"role": "system", "content": _ANSWER_SYSTEM},
            {"role": "user", "content": (
                f"Question: {question}\n\n"
                f"Results:\n{results_context}"
            )},
        ],
        temperature=0.1,
    )


# ── Full SQL pipeline ─────────────────────────────────────────────────────────

def run_sql_pipeline(
    question: str,
    last_sql: str | None = None,
    clearance=None,
) -> tuple[str, str, list[dict], list[str]]:
    """
    Full pipeline: schema context → generate SQL → validate → execute → answer.

    Args:
        question: the user's natural language question
        last_sql: the SQL from the previous turn (for follow-up context)
        clearance: caller clearance set (audit #7); gates the Qdrant schema-context retrieval below.

    Returns:
        (natural_language_answer, sql_used, rows, columns)
    """
    # Scope resolution. Aggregation (SUM/AVG/GROUP BY/"most"/"per") stays on the
    # direct NL->SQL path below; only a genuine schema-wide question that must
    # first DISCOVER a table set is composed via the enumerate pipeline. The gate
    # is deterministic (no LLM), so aggregate phrasing can't misroute here.
    # See planner.resolve_scope.
    from planner import resolve_scope, run_enumerate_pipeline
    scope, schema = resolve_scope(question)
    if scope == "enumerate":
        # Enumerate reads only the live catalog (sys.tables) — schema structure,
        # not data — so it is not clearance-gated at the Qdrant layer.
        return run_enumerate_pipeline(question, schema)

    # 1. Get schema context — for follow-ups, anchors to the previous table
    schema_context, _ = get_schema_context(question, last_sql, clearance=clearance)

    # 2. Generate SQL — pass last_sql so follow-ups stay on the right table
    sql = generate_sql(question, schema_context, last_sql)

    # 3. Validate
    valid, reason = validate_sql(sql)

    if not valid:
        if reason == "insufficient_schema":
            return (
                "I don't have enough schema information in the Data Dictionary "
                "to generate a query for that question.",
                "INSUFFICIENT_SCHEMA", [], []
            )
        return (
            f"I generated a query but it failed safety validation: {reason}",
            sql, [], []
        )

    # 3b. Q6 ordering guard — a superlative question answered with TOP but no
    # ORDER BY returns arbitrary rows. Repair that exact defect before executing;
    # only accept the repair if it validates AND actually added an ORDER BY (else
    # keep the original — never a regression).
    if _ORDERING_INTENT_RE.search(question) and _top_without_order_by(sql):
        fixed = repair_missing_order_by(question, schema_context, sql)
        ok_fixed, _ = validate_sql(fixed)
        if ok_fixed and _HAS_ORDER_BY_RE.search(fixed):
            sql = fixed

    sql = _guard_null_order(question, sql)

    # 4. Execute — with one self-correction attempt on failure
    try:
        rows, columns = execute_sql(sql)
    except Exception as e:
        repaired = repair_sql(question, schema_context, sql, str(e))
        ok, why = validate_sql(repaired)
        if not ok or repaired == sql:
            return (
                f"The query failed to execute: {str(e)}",
                sql, [], []
            )
        try:
            rows, columns = execute_sql(repaired)
            sql = repaired  # report the query that actually ran
        except Exception as e2:
            return (
                f"The query failed to execute: {str(e2)}",
                repaired, [], []
            )

    # 5. Generate natural language answer
    results_context = format_results_for_llm(rows, columns, sql)
    answer = generate_answer(question, results_context)

    return answer, sql, rows, columns
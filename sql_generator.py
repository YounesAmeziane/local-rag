# sql_generator.py
# Handles the SQL generation and execution path.
#
# Flow:
#   1. Retrieve relevant schema context from DataDictionary (Qdrant)
#   2. LLM generates a SELECT statement
#   3. Validate — reject anything that isn't a clean SELECT
#   4. Execute against SQL Server
#   5. Return results + the generated SQL for transparency

import re
import pyodbc
import ollama
from qdrant_client.models import ScoredPoint

import config
import retriever

_ollama = ollama.Client(host=config.OLLAMA_HOST)

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
- TOP 100: add it ONLY when returning raw rows with no aggregation.
  NEVER add TOP when the query contains COUNT, SUM, AVG, MIN, MAX, or GROUP BY.
  Aggregates produce their own natural result set — TOP would be wrong there.
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

def _build_sql_prompt(question: str, schema_context: str) -> str:
    return (
        f"Schema context from Data Dictionary:\n\n{schema_context}\n\n"
        f"---\n\nGenerate a T-SQL SELECT query to answer this question:\n{question}"
    )


# ── Schema retrieval for SQL context ─────────────────────────────────────────

def get_schema_context(question: str, last_sql: str | None = None) -> tuple[str, list]:
    """
    Retrieves relevant table/column descriptions from the DataDictionary.

    If last_sql is provided and the question looks like a follow-up (contains
    vague pronouns), extracts the table from last_sql and fetches ALL columns
    for that table directly — bypassing vector search entirely.
    This ensures follow-up questions always have the full schema of the right table.
    """
    import re as _re

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

            qc = QdrantClient(host=_config.QDRANT_HOST, port=_config.QDRANT_PORT)
            must = [FieldCondition(key="object_name", match=MatchValue(value=table))]
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

    # Standard vector search for non-follow-up questions
    results, _ = retriever.retrieve(question, top_k=10)

    tables: dict[str, dict] = {}
    for hit in results:
        p = hit.payload
        key = f"{p['schema_name']}.{p['object_name']}"
        if key not in tables:
            tables[key] = {
                "database":    p["database_name"],
                "schema":      p["schema_name"],
                "table":       p["object_name"],
                "description": p["object_description"],
                "columns":     [],
            }
        tables[key]["columns"].append({
            "name":        p["column_name"],
            "type":        p["data_type"],
            "nullable":    p["is_nullable"],
            "identity":    p["is_identity"],
            "description": p["column_description"],
        })

    lines = []
    for tbl in tables.values():
        lines.append(
            f"Table: [{tbl['database']}].[{tbl['schema']}].[{tbl['table']}]"
        )
        lines.append(f"  Description: {tbl['description']}")
        lines.append("  Columns:")
        for col in tbl["columns"]:
            nullable = "NULL" if col["nullable"] else "NOT NULL"
            identity = " IDENTITY" if col["identity"] else ""
            lines.append(
                f"    - {col['name']} ({col['type']}{identity}, {nullable})"
                f" — {col['description']}"
            )
        lines.append("")

    return "\n".join(lines), results


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
    resp = _ollama.chat(
        model=config.CHAT_MODEL,
        messages=[
            {"role": "system", "content": _SQL_SYSTEM},
            {"role": "user",   "content": _build_sql_prompt(question, schema_context, last_sql)},
        ],
        options={"temperature": 0},
    )
    sql = resp["message"]["content"].strip()

    # Strip markdown fences if the model adds them despite instructions
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()


# ── Validation ────────────────────────────────────────────────────────────────

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|EXEC|EXECUTE|"
    r"xp_|sp_|OPENROWSET|BULK|GRANT|REVOKE|DENY)\b",
    re.IGNORECASE,
)

def validate_sql(sql: str) -> tuple[bool, str]:
    """
    Validates that the SQL is a safe SELECT statement.
    Returns (is_valid, reason).
    """
    if sql == "INSUFFICIENT_SCHEMA":
        return False, "insufficient_schema"

    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        return False, f"Query does not start with SELECT: {stripped[:60]}"

    match = _FORBIDDEN.search(stripped)
    if match:
        return False, f"Forbidden keyword detected: {match.group()}"

    return True, "ok"


# ── Execution ─────────────────────────────────────────────────────────────────

def execute_sql(sql: str) -> tuple[list[dict], list[str]]:
    """
    Executes a validated SELECT against SQL Server.
    Returns (rows, columns) where rows is a list of dicts.
    Caps at 100 rows and enforces a 30-second timeout.
    """
    conn_str = (
        f"DRIVER={{{config.DB_DRIVER}}};"
        f"SERVER={config.DB_SERVER};"
        f"DATABASE={config.DB_DATABASE};"
        "Trusted_Connection=yes;"
        "ApplicationIntent=ReadOnly;"  # hint to SQL Server: read-only intent
    )
    conn = pyodbc.connect(conn_str, timeout=30)
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
Do not reproduce the full table in your answer unless it is very small (3 rows or fewer).
Always mention what the query was counting or selecting so the answer is unambiguous.
"""

def generate_answer(question: str, results_context: str) -> str:
    """
    Generates a natural language answer from the SQL results.
    """
    resp = _ollama.chat(
        model=config.CHAT_MODEL,
        messages=[
            {"role": "system", "content": _ANSWER_SYSTEM},
            {"role": "user", "content": (
                f"Question: {question}\n\n"
                f"Results:\n{results_context}"
            )},
        ],
        options={"temperature": 0.1},
        stream=False,
    )
    return resp["message"]["content"].strip()


# ── Full SQL pipeline ─────────────────────────────────────────────────────────

def run_sql_pipeline(
    question: str,
    last_sql: str | None = None,
) -> tuple[str, str, list[dict], list[str]]:
    """
    Full pipeline: schema context → generate SQL → validate → execute → answer.

    Args:
        question: the user's natural language question
        last_sql: the SQL from the previous turn (for follow-up context)

    Returns:
        (natural_language_answer, sql_used, rows, columns)
    """
    # 1. Get schema context — for follow-ups, anchors to the previous table
    schema_context, _ = get_schema_context(question, last_sql)

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

    # 4. Execute
    try:
        rows, columns = execute_sql(sql)
    except Exception as e:
        return (
            f"The query failed to execute: {str(e)}",
            sql, [], []
        )

    # 5. Generate natural language answer
    results_context = format_results_for_llm(rows, columns, sql)
    answer = generate_answer(question, results_context)

    return answer, sql, rows, columns
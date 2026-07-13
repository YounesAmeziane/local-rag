"""
test_sql_multistep.py — Tests for the multi-step (enumerate) SQL path.

This path handles schema-wide questions that must DISCOVER a table set before
answering (e.g. "how many rows across schema dm_dq"). Its design is compose:

    resolve_scope -> enumerate tables (catalog) -> build ONE UNION-ALL count
    query (deterministic) -> execute -> answer.

There is no LLM planner / fan-out executor on this path anymore; the pure-logic
tests below therefore target the REAL primitives that run in production
(resolve_scope, _build_union_count_sql, identifier escaping), not the retired
fan-out machinery.

Two categories:

1. Pure-logic tests (no DB, no LLM) — deterministic, fast, safe for CI. They
   cover the routing gate and the SQL the enumerate path actually builds.

2. Live end-to-end tests (@pytest.mark.live) — hit the real DB via
   run_sql_pipeline(). Assertions are STRUCTURAL (routes to enumerate, emits a
   UNION-ALL, answer contains a number) rather than hardcoded row counts, so
   they never need updating as data drifts.

Run:
    pytest test_sql_multistep.py -m "not live"    # fast, no DB/LLM
    pytest test_sql_multistep.py -m live           # end-to-end (needs DB+Ollama)
    pytest test_sql_multistep.py                    # everything
"""

import re

import pytest

import config


# ---------------------------------------------------------------------------
# Category 1a: scope resolution — the deterministic routing gate.
# Aggregation phrasing must stay 'direct'; only a schema-wide table cue with a
# resolvable schema becomes 'enumerate'.
# ---------------------------------------------------------------------------

def test_scope_flags_schema_question_as_enumerate():
    """The original bug routes to the enumerate path, with the right schema."""
    from planner import resolve_scope
    scope, schema = resolve_scope(
        "how many rows of data we have in the tables with the schema dm_dq"
    )
    assert scope == "enumerate"
    assert schema == "dm_dq"


def test_scope_keeps_datadictionary_question_direct():
    """'DataDictionary' is a specific catalog table -> direct, never enumerate,
    even though the question is aggregate-shaped."""
    from planner import resolve_scope
    scope, _ = resolve_scope(
        "how many rows are in [MetadataRepository].[rpt].[DataDictionary]"
    )
    assert scope == "direct"


def test_scope_aggregate_is_not_enumerate():
    """The core regression guard: aggregation phrasing must NOT trigger enumerate."""
    from planner import resolve_scope
    for q in (
        "what is the total number of failed rows across all data quality results?",
        "how many rules have no domain assigned?",
        "how many assets are there per asset type?",
        "which scan type has the most failures?",
    ):
        scope, _ = resolve_scope(q)
        assert scope == "direct", f"{q!r} should be direct, got {scope!r}"


def test_scope_extracts_schema_from_phrasings():
    """The schema name is resolved from the common enumerate phrasings."""
    from planner import resolve_scope
    for q, expect in (
        ("how many rows across all tables in schema dm_dq", "dm_dq"),
        ("count the rows in the tables with the schema dq", "dq"),
        ("which table in schema gov has the most rows?", "gov"),
    ):
        scope, schema = resolve_scope(q)
        assert scope == "enumerate", f"{q!r} should enumerate, got {scope!r}"
        assert schema == expect, f"{q!r}: expected schema {expect!r}, got {schema!r}"


def test_catalog_detection():
    from planner import is_catalog_question
    assert is_catalog_question("how many tables in the DataDictionary?")
    assert is_catalog_question("columns in the data dictionary")
    assert not is_catalog_question("how many rows in schema dm_dq")


# ---------------------------------------------------------------------------
# Category 1b: the enumerate SQL builder — the primitive that actually runs.
# Deterministic string construction; no DB, no LLM.
# ---------------------------------------------------------------------------

def test_build_union_count_sql_shape():
    from planner import _build_union_count_sql
    sql = _build_union_count_sql("dm_dq", ["alpha", "beta", "gamma"])
    # One counted SELECT per table, unioned, ordered by the count.
    assert sql.count(" UNION ALL ") == 2               # 3 tables -> 2 joins
    assert sql.count("COUNT_BIG(*)") == 3
    assert "ORDER BY [RowCount] DESC" in sql
    # Fully-qualified [db].[schema].[table] for each table.
    db = config.DB_DATABASE
    for t in ("alpha", "beta", "gamma"):
        assert f"[{db}].[dm_dq].[{t}]" in sql


def test_build_union_count_sql_carries_table_names():
    """Table names travel as literals so the answer step can name the winner
    (e.g. 'which table has the most rows')."""
    from planner import _build_union_count_sql
    sql = _build_union_count_sql("dq", ["Results", "Rules"])
    assert "'Results' AS TableName" in sql
    assert "'Rules' AS TableName" in sql


def test_identifier_and_literal_escaping():
    """sys.tables names are trusted, but we still escape defensively so a table
    named with a ']' or a quote can't break out of its identifier/literal."""
    from planner import _bracket, _sql_literal
    assert _bracket("a]b") == "[a]]b]"
    assert _sql_literal("O'Brien") == "'O''Brien'"
    # End to end through the builder.
    from planner import _build_union_count_sql
    sql = _build_union_count_sql("dq", ["Wei'rd]Name"])
    assert "[Wei'rd]]Name]" in sql          # bracket-escaped identifier
    assert "'Wei''rd]Name' AS TableName" in sql  # quote-escaped literal


# ---------------------------------------------------------------------------
# Category 1c: enumeration filtering config.
# ---------------------------------------------------------------------------

def test_schema_denylist_defaults():
    from planner import SCHEMA_DENYLIST
    assert "sys" in SCHEMA_DENYLIST
    assert "INFORMATION_SCHEMA" in SCHEMA_DENYLIST


# ---------------------------------------------------------------------------
# Category 2: live end-to-end tests. Require Ollama + SQL Server.
# Skip with:  pytest -m "not live"
# Assertions are structural, so they do NOT need updating as data drifts.
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_dm_dq_row_count_end_to_end():
    """The original bug, end-to-end via run_sql_pipeline.

    - Pipeline routes to the enumerate branch (sql_used starts with '-- enumerate').
    - The executed query is a single UNION-ALL over the discovered tables.
    - One row per discovered table is returned; the answer contains a number.

    run_sql_pipeline returns (answer, sql_used, rows, columns) — same shape and
    order as the direct path, so chat.py's unpacking works for both branches.
    """
    from sql_generator import run_sql_pipeline
    answer, sql_used, rows, _columns = run_sql_pipeline(
        "how many rows of data we have in the tables with the schema dm_dq", None,
    )
    assert sql_used.startswith("-- enumerate"), (
        f"Expected enumerate routing. Got sql_used={sql_used!r}"
    )
    assert "UNION ALL" in sql_used, (
        f"Expected a UNION-ALL query over the discovered tables. sql_used={sql_used!r}"
    )
    assert rows, "enumerate should return one row per discovered table"
    assert answer, "answer should not be empty"
    assert re.search(r"\d", answer), (
        f"answer should contain at least one number. Got: {answer!r}"
    )


@pytest.mark.live
def test_simple_question_bypasses_enumerate():
    """A trivially simple question should NOT go through the enumerate path.
    We detect this by the absence of the enumerate marker in the result."""
    from sql_generator import run_sql_pipeline
    result = run_sql_pipeline(
        "how many rows are in [MetadataRepository].[rpt].[DataDictionary]", None,
    )
    sql_used = result[1]
    assert not sql_used.startswith("-- enumerate"), (
        f"Simple question should skip enumerate. Got sql_used={sql_used!r}"
    )


@pytest.mark.live
def test_which_table_biggest_in_schema():
    """'which table in schema X has the most rows' — enumerate + per-table count,
    then argmax handled by the DB's ORDER BY and the answer step. The composed
    UNION-ALL query carries the table names, so the answer can name the winner."""
    from sql_generator import run_sql_pipeline
    answer, sql_used, rows, _cols = run_sql_pipeline(
        "which table in schema dm_dq has the most rows?", None,
    )
    assert sql_used.startswith("-- enumerate")
    assert "UNION ALL" in sql_used
    assert rows
    assert answer

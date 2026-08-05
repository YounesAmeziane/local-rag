# retriever.py
# Query-time retrieval with query rewriting.
#
# Two retrieval paths:
#   1. Full-table fetch — "list all columns in X" questions
#      → Qdrant scroll by payload filter, sorted by column_order
#      → bypasses vector search entirely, returns ALL columns
#   2. Semantic search — all other questions
#      → rewrite → embed → Qdrant cosine search → top-k chunks

import re
from dataclasses import dataclass
import ollama
from qdrant_client import QdrantClient
from qdrant_client.models import ScoredPoint, Filter, FieldCondition, MatchValue, MatchAny
import config
import lexical


# ── Access control (audit #7) ─────────────────────────────────────────────────

def _clearance_condition(clearance) -> FieldCondition:
    """Deny-by-default clearance gate. Returns a Qdrant condition matching only
    points whose `clearance` label is in the caller's allowed set. Points with no
    `clearance` field never match MatchAny, so they are excluded (fail closed).
    `clearance=None` falls back to config.DEFAULT_CLEARANCE."""
    allowed = list(clearance) if clearance else list(config.DEFAULT_CLEARANCE)
    return FieldCondition(key="clearance", match=MatchAny(any=allowed))

_ollama_client = ollama.Client(host=config.OLLAMA_HOST)
_qdrant_client = QdrantClient(
    host=config.QDRANT_HOST, port=config.QDRANT_PORT,
    grpc_port=config.QDRANT_GRPC_PORT, prefer_grpc=True,
)


# ── Hybrid lexical index + Reciprocal Rank Fusion (audit #6) ───────────────────

@dataclass
class Hit:
    """A fused (dense+lexical) search result. Downstream formatters read only
    .payload and .score; .id exists for de-duplication during fusion. Shaped to be
    a drop-in for the Qdrant ScoredPoints the retrieve* functions used to return."""
    id: object
    score: float
    payload: dict


_lexical_indexes: dict[str, lexical.BM25Index] = {}


def _lexical_index(collection: str) -> lexical.BM25Index:
    """Lazily build (once per process, then cache) a BM25 index over every point's
    `text` in a collection. Built from a single Qdrant scroll — the collections
    change only on re-ingest, so a per-process cache is correct for a chat session.
    A long-running server rebuilds by restarting after ingest."""
    idx = _lexical_indexes.get(collection)
    if idx is None:
        docs: list[tuple] = []
        offset = None
        while True:
            points, offset = _qdrant_client.scroll(
                collection_name=collection,
                limit=500, offset=offset,
                with_payload=True, with_vectors=False,
            )
            docs.extend((p.id, p.payload.get("text", ""), p.payload) for p in points)
            if offset is None:
                break
        idx = lexical.BM25Index(docs)
        _lexical_indexes[collection] = idx
    return idx


def _clearance_allows(payload: dict, clearance) -> bool:
    """Python mirror of _clearance_condition, for filtering lexical candidates that
    did not pass through Qdrant's server-side filter. Deny-by-default: a point with
    no `clearance` label is excluded (fail closed), matching MatchAny semantics."""
    allowed = set(clearance) if clearance else set(config.DEFAULT_CLEARANCE)
    label = payload.get("clearance")
    if label is None:
        return False
    if isinstance(label, (list, tuple, set)):
        return bool(set(label) & allowed)
    return label in allowed


def _rrf_fuse(dense_hits, sparse_hits, top_k: int, k: int | None = None) -> list[Hit]:
    """Reciprocal Rank Fusion of the dense ranker (Qdrant ScoredPoints) and the
    sparse ranker (BM25 (id, score, payload) tuples). A point's fused score is the
    sum of 1/(k + rank) over each ranker it appears in (rank 0-based); points seen
    by both rankers are thus boosted. Payload comes from whichever ranker saw the
    point first (dense preferred)."""
    if k is None:
        k = config.RRF_K
    scores: dict[object, float] = {}
    payloads: dict[object, dict] = {}
    for rank, h in enumerate(dense_hits):
        scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (k + rank + 1)
        payloads[h.id] = h.payload
    for rank, (pid, _score, pl) in enumerate(sparse_hits):
        scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank + 1)
        payloads.setdefault(pid, pl)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [Hit(id=pid, score=score, payload=payloads[pid]) for pid, score in ordered]


# ── Regex helpers ─────────────────────────────────────────────────────────────

# Signals that a question is a follow-up referring to prior context
_VAGUE_PATTERNS = re.compile(
    r"\b(it|its|that|those|this|these|the column|the table|the field|"
    r"that column|that table|that field|the same|the one|the primary key|"
    r"can that|is that|does that|what about it|what is it)\b",
    re.IGNORECASE,
)

# Extract schema.table references (e.g. "dq.RuleTargets")
_TABLE_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b")

# Extract column names
_COLUMN_PATTERN = re.compile(
    r"(?:column(?:\s+name)?[:\s]+([A-Z][a-zA-Z0-9_]+))"
    r"|(?:([A-Z][a-zA-Z0-9_]+)\s+column\b)"
    r"|(?:\b([A-Z][a-zA-Z0-9_]{2,})\s+\[(?:bigint|int|nvarchar|bit|uniqueidentifier|datetime2|decimal|sysname)\])"
)

# Detects "list all columns" intent
_LIST_COLUMNS_PATTERN = re.compile(
    r"\b(list|show|what|give me|tell me|all|every|how many)[\w\s]+"
    r"(columns?|fields?|attributes?|schema|structure|definition)\b"
    r"|\b(columns?|fields?)[\w\s]+(does|in|of|for|has|are in)\b"
    r"|\bwhat does .{0,30}(look like|contain|have)\b",
    re.IGNORECASE,
)


# ── "List all columns" detection ──────────────────────────────────────────────

def is_list_columns_question(question: str) -> bool:
    return bool(_LIST_COLUMNS_PATTERN.search(question))


# ── Full-table fetch (bypasses vector search) ─────────────────────────────────

def fetch_all_columns(schema: str, table: str, clearance=None) -> list[dict]:
    """
    Fetches ALL columns for schema.table from Qdrant, sorted by column_order.
    Uses scroll (not search) — payload filter only, no vector similarity.
    """
    table_filter = Filter(
        must=[
            _clearance_condition(clearance),
            FieldCondition(key="schema_name", match=MatchValue(value=schema)),
            FieldCondition(key="object_name", match=MatchValue(value=table)),
        ]
    )

    results, _ = _qdrant_client.scroll(
        collection_name=config.COLLECTION_NAME,
        scroll_filter=table_filter,
        limit=200,
        with_payload=True,
        with_vectors=False,
    )

    payloads = [r.payload for r in results]
    payloads.sort(key=lambda p: p.get("column_order", 0))
    return payloads


# Generic schema vocabulary that must never be treated as a candidate table name,
# even if a real table happens to be named exactly this (e.g. mdm.Columns) — these
# words appear in virtually every column/structure question by construction
# (is_list_columns_question triggers on them), so matching them as "the table the
# user means" would collide on nearly every call.
_GENERIC_STRUCTURE_WORDS = {
    "column", "columns", "field", "fields", "attribute", "attributes",
    "schema", "structure", "definition", "table", "tables",
}


def resolve_bare_table_name(question: str, clearance=None) -> tuple[str, str] | None:
    """Resolve a schema-less table name mentioned in the question (e.g.
    "consistency_runs" instead of "dm_dq.consistency_runs") against the known
    tables in the Data Dictionary. Returns (schema, table) if exactly one known
    table name appears as a whole word in the question, else None (not found,
    or ambiguous across schemas).

    Callers should only invoke this once a question has already been routed into
    DataDictionary territory (structured/both/sql) — not on every message — both to
    avoid the collection scroll on unrelated questions and because a stray word
    matching a table name is more likely a false positive outside that context.
    The _GENERIC_STRUCTURE_WORDS exclusion below guards the main false-positive
    case (e.g. a real table literally named "Columns") regardless of caller.
    """
    known: dict[str, set[tuple[str, str]]] = {}
    offset = None
    while True:
        points, offset = _qdrant_client.scroll(
            collection_name=config.COLLECTION_NAME,
            scroll_filter=Filter(must=[_clearance_condition(clearance)]),
            limit=500,
            offset=offset,
            with_payload=["schema_name", "object_name"],
            with_vectors=False,
        )
        for p in points:
            obj = p.payload.get("object_name")
            schema = p.payload.get("schema_name")
            if obj and schema:
                known.setdefault(obj.lower(), set()).add((schema, obj))
        if offset is None:
            break

    q_lower = question.lower()
    matches: set[tuple[str, str]] = set()
    for name_lower, pairs in known.items():
        if name_lower in _GENERIC_STRUCTURE_WORDS:
            continue
        if re.search(rf"\b{re.escape(name_lower)}\b", q_lower):
            matches |= pairs

    if len(matches) == 1:
        return matches.pop()
    return None  # not found, or ambiguous (same table name in multiple schemas)


def format_all_columns_context(payloads: list[dict]) -> str:
    """
    Renders a complete column listing for the LLM prompt.
    Compact tabular style — one line per column.
    """
    if not payloads:
        return "No columns found for this table."

    sample = payloads[0]
    schema  = sample.get("schema_name", "")
    obj     = sample.get("object_name", "")
    desc    = sample.get("object_description", "")

    lines = [
        f"Table: {schema}.{obj}",
        f"Description: {desc}",
        f"Total columns: {len(payloads)}",
        "",
        f"{'#':<5} {'Column Name':<30} {'Data Type':<22} {'Nullable':<13} {'Identity':<10} Description",
        "-" * 115,
    ]
    for p in payloads:
        nullable = "nullable" if p.get("is_nullable") else "NOT nullable"
        identity = "yes"      if p.get("is_identity") else "no"
        lines.append(
            f"{p.get('column_order', ''):<5} "
            f"{p.get('column_name', ''):<30} "
            f"{p.get('data_type', ''):<22} "
            f"{nullable:<13} "
            f"{identity:<10} "
            f"{p.get('column_description', '')}"
        )
    return "\n".join(lines)


# ── Full-schema table listing (bypasses vector search) ────────────────────────
#
# "What tables are in schema X" is an enumeration question — like "list all columns
# in table Y" — and must be answered by an EXHAUSTIVE scroll, not top-k vector
# search. Vector search returns a partial, imprecise set (it surfaced only 2 of the
# 4 dq tables and 2 tables from the wrong schema), so the model reported the wrong
# count. This is the schema-level sibling of fetch_all_columns.

_TABLES_INTENT_RE = re.compile(r"\btables?\b", re.IGNORECASE)
# "which/what table(s) does X" asks the model to IDENTIFY a table (the answer is a
# table name), as opposed to "what columns/fields does <table> have" (the answer is
# a column list). The head noun immediately after what/which is the discriminator:
# "table(s)" => discovery, "column(s)/field(s)" => column listing. Kept tight (table
# right after what/which) so it never matches a column question that merely mentions
# a table elsewhere, e.g. "what columns does the scan_queue table have".
_TABLE_DISCOVERY_RE = re.compile(r"\b(what|which)\s+tables?\b", re.IGNORECASE)
# The "<word> schema" alternative excludes common articles/prepositions so that
# "in schema sec" doesn't capture "in" (from "in schema") ahead of "sec" (from
# "schema sec"); leftmost-match would otherwise grab the wrong token.
_SCHEMA_REF_RE = re.compile(
    r"\bschema\s+\[?([a-zA-Z_][a-zA-Z0-9_]*)\]?"       # "schema dq"
    r"|\b(?!the\b|in\b|a\b|an\b|this\b|that\b|which\b|each\b|every\b|any\b|no\b|of\b)"
    r"\[?([a-zA-Z_][a-zA-Z0-9_]*)\]?\s+schema\b",       # "dq schema" / "the dq schema"
    re.IGNORECASE,
)


def resolve_list_tables_question(question: str, clearance=None) -> tuple[str, list[dict]] | None:
    """If the question asks to list/count the tables in a specific schema, return
    (canonical_schema, tables) via ONE exhaustive scroll, else None. tables is
    [{"object_name","object_description"}] sorted by name.

    Recognizes both phrasings: an explicit schema reference ("in the dq schema",
    "schema dq") AND a bare known-schema name ("what tables exist in dq") — the
    bare form is validated against the Data Dictionary's actual schema names, so a
    stray word can't false-match. Case-insensitive; returns None (falls through to
    normal handling) if no schema resolves or the reference is ambiguous."""
    if not _TABLES_INTENT_RE.search(question):
        return None

    by_schema: dict[str, tuple[str, dict]] = {}
    offset = None
    while True:
        points, offset = _qdrant_client.scroll(
            collection_name=config.COLLECTION_NAME,
            scroll_filter=Filter(must=[_clearance_condition(clearance)]),
            limit=500, offset=offset,
            with_payload=["schema_name", "object_name", "object_description"],
            with_vectors=False,
        )
        for p in points:
            s = p.payload.get("schema_name")
            obj = p.payload.get("object_name")
            if not s or not obj:
                continue
            canonical, tbls = by_schema.setdefault(s.lower(), (s, {}))
            tbls.setdefault(obj, p.payload.get("object_description", ""))
        if offset is None:
            break

    # 1. Explicit "schema X" / "X schema" phrasing.
    candidate = None
    m = _SCHEMA_REF_RE.search(question)
    if m:
        tok = (m.group(1) or m.group(2) or "").lower()
        if tok in by_schema:
            candidate = tok

    # 2. Fall back to a bare known-schema name mentioned as a whole word and not
    #    part of a schema.table reference (the "(?!\.)" excludes e.g. the "dq" in
    #    "dq.Results"). Require exactly one distinct schema to avoid ambiguity.
    if candidate is None:
        q_lower = question.lower()
        hits = {
            s for s in by_schema
            if re.search(rf"\b{re.escape(s)}\b(?!\.)", q_lower)
        }
        if len(hits) == 1:
            candidate = hits.pop()

    if candidate is None or candidate not in by_schema:
        return None
    canonical, tbls = by_schema[candidate]
    tables = [{"object_name": k, "object_description": v} for k, v in sorted(tbls.items())]
    return canonical, tables


def format_all_tables_context(schema: str, tables: list[dict]) -> str:
    """Renders a complete table listing for one schema for the LLM prompt."""
    if not tables:
        return f"No tables found in schema '{schema}'."
    lines = [
        f"Schema: {schema}",
        f"Total tables: {len(tables)}",
        "",
        f"{'Table Name':<40} Description",
        "-" * 100,
    ]
    for t in tables:
        lines.append(f"{t['object_name']:<40} {t.get('object_description', '')}")
    return "\n".join(lines)


# ── Whole-catalog listing for theme/group discovery ──────────────────────────
#
# "Which tables support X" asks for a GROUP of thematically-related tables. Top-k
# column-chunk vector search answers this partially — it surfaces the obvious table
# and misses siblings whose columns don't individually match the theme (e.g. found
# AuditEvents but missed AccessRequests/ErrorLog for "audit evidence"). With a small
# catalog, the robust fix is complete recall: hand the model EVERY table + description
# and let it filter by theme. (At thousands of tables, switch to a table-level index.)

def all_tables_catalog(clearance=None) -> list[dict]:
    """Every table in the Data Dictionary as {schema, object_name, object_description},
    sorted. One scroll. For theme/group discovery where complete recall matters."""
    seen: dict[tuple[str, str], str] = {}
    offset = None
    while True:
        points, offset = _qdrant_client.scroll(
            collection_name=config.COLLECTION_NAME,
            scroll_filter=Filter(must=[_clearance_condition(clearance)]),
            limit=500, offset=offset,
            with_payload=["schema_name", "object_name", "object_description"],
            with_vectors=False,
        )
        for p in points:
            s = p.payload.get("schema_name")
            o = p.payload.get("object_name")
            if s and o:
                seen.setdefault((s, o), p.payload.get("object_description", ""))
        if offset is None:
            break
    return [
        {"schema": s, "object_name": o, "object_description": d}
        for (s, o), d in sorted(seen.items())
    ]


def format_catalog_context(catalog: list[dict]) -> str:
    """Renders the complete table catalog (schema.table — description) for the LLM to
    filter by theme."""
    if not catalog:
        return "No tables found."
    lines = [
        f"Complete list of all {len(catalog)} tables in the MetadataRepository "
        f"Data Dictionary (schema.table — description). This is the FULL set of tables. "
        f"To answer 'which tables ...' select EVERY table whose described purpose belongs "
        f"to the functional area the question asks about — include a table when its "
        f"description shows it is part of that area, even if the description does not "
        f"repeat the exact words in the question. Do not restrict to a single table.",
        "",
    ]
    for t in catalog:
        lines.append(f"{t['schema']}.{t['object_name']} — {t.get('object_description', '')}")
    return "\n".join(lines)


# ── Query rewriting ───────────────────────────────────────────────────────────

def _extract_context(text: str) -> tuple[str | None, str | None]:
    table = None
    tables = _TABLE_PATTERN.findall(text)
    if tables:
        schema, obj = tables[-1]
        if schema.lower() not in ("information_schema", "sys", "dbo"):
            table = f"{schema}.{obj}"

    column = None
    col_matches = _COLUMN_PATTERN.findall(text)
    if col_matches:
        candidates = [g for match in col_matches for g in match if g]
        if candidates:
            column = candidates[-1]

    return table, column


def rewrite_query(
    question: str,
    last_assistant_reply: str | None,
    topic_table: str | None = None,
) -> str:
    if not last_assistant_reply and not topic_table:
        return question
    if _TABLE_PATTERN.search(question):
        return question
    if not _VAGUE_PATTERNS.search(question):
        return question

    if topic_table:
        table = topic_table
        _, column = _extract_context(last_assistant_reply or "")
    else:
        table, column = _extract_context(last_assistant_reply or "")

    if not table and not column:
        return question

    parts = [question.rstrip("?").strip()]
    if column:
        parts.append(f"specifically the {column} column")
    if table:
        parts.append(f"in {table}")

    return " ".join(parts)


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_query(query: str) -> list[float]:
    resp = _ollama_client.embeddings(model=config.EMBED_MODEL, prompt=query)
    return resp["embedding"]


# ── Semantic search ───────────────────────────────────────────────────────────

def retrieve(
    question: str,
    last_assistant_reply: str | None = None,
    topic_table: str | None = None,
    top_k: int = config.TOP_K,
    clearance=None,
) -> tuple[list[ScoredPoint], str]:
    """
    Rewrites the question, embeds it, searches Qdrant.
    Always uses vector similarity search — for full-table column listing
    use fetch_all_columns() instead.
    """
    rewritten = rewrite_query(question, last_assistant_reply, topic_table)
    query_vector = embed_query(rewritten)

    # Clearance gate is always applied (deny-by-default); topic_table narrows further.
    clearance_cond = _clearance_condition(clearance)
    topic_active = bool(topic_table and "." in topic_table)
    must = [clearance_cond]
    if topic_active:
        schema, obj = topic_table.split(".", 1)
        must += [
            FieldCondition(key="schema_name", match=MatchValue(value=schema)),
            FieldCondition(key="object_name", match=MatchValue(value=obj)),
        ]

    # Hybrid only helps broad questions; a topic-anchored follow-up is already
    # filtered to one table's columns, where lexical adds nothing. When hybrid is
    # active we pull a larger candidate pool per ranker before fusing down to top_k.
    hybrid = config.HYBRID_SEARCH and not topic_active
    dense_limit = config.HYBRID_CANDIDATE_POOL if hybrid else top_k

    results = _qdrant_client.search(
        collection_name=config.COLLECTION_NAME,
        query_vector=query_vector,
        query_filter=Filter(must=must),
        limit=dense_limit,
        with_payload=True,
    )

    # Fallback when the topic_table narrowing yields nothing — but STILL enforce
    # clearance (never drop the security filter).
    if not results and topic_active:
        results = _qdrant_client.search(
            collection_name=config.COLLECTION_NAME,
            query_vector=query_vector,
            query_filter=Filter(must=[clearance_cond]),
            limit=top_k,
            with_payload=True,
        )

    if not hybrid:
        return results, rewritten

    # Broad question: fuse dense with a BM25 lexical ranker so exact technical tokens
    # the embedding blurred still surface. Lexical candidates get the same deny-by-
    # default clearance filter the dense side received server-side.
    sparse = [
        cand
        for cand in _lexical_index(config.COLLECTION_NAME).search(rewritten, config.HYBRID_CANDIDATE_POOL)
        if _clearance_allows(cand[2], clearance)
    ]
    return _rrf_fuse(results, sparse, top_k), rewritten


# ── Context formatting ────────────────────────────────────────────────────────

def format_context(results: list[ScoredPoint]) -> str:
    parts = []
    for i, hit in enumerate(results, 1):
        p = hit.payload
        parts.append(
            f"ENTRY {i}\n"
            f"Source: {p['schema_name']}.{p['object_name']}, column: {p['column_name']}\n"
            f"{p['text']}"
        )
    return "\n\n".join(parts)


def format_context_debug(results: list[ScoredPoint]) -> str:
    parts = []
    for i, hit in enumerate(results, 1):
        p = hit.payload
        parts.append(
            f"[{i}] {p['schema_name']}.{p['object_name']} → {p['column_name']} "
            f"(score: {hit.score:.3f})\n"
            f"{p['text']}"
        )
    return "\n\n---\n\n".join(parts)


# ── Document search ───────────────────────────────────────────────────────────

def retrieve_docs(
    question: str,
    top_k: int = config.DOCS_TOP_K,
    clearance=None,
) -> list[ScoredPoint]:
    """
    Semantic search against the documents collection.
    Deny-by-default clearance gate; no topic_table filter — documents span all files.
    """
    query_vector = embed_query(question)
    hybrid = config.HYBRID_SEARCH
    dense_limit = config.HYBRID_CANDIDATE_POOL if hybrid else top_k
    results = _qdrant_client.search(
        collection_name=config.DOCS_COLLECTION_NAME,
        query_vector=query_vector,
        query_filter=Filter(must=[_clearance_condition(clearance)]),
        limit=dense_limit,
        with_payload=True,
    )
    if not hybrid:
        return results

    # Docs are the corpus BM25 helps most (exact tokens: AES_KEY_BASE64, error codes,
    # config keys). No topic anchoring here, so hybrid always applies. Same deny-by-
    # default clearance filter on the lexical side.
    sparse = [
        cand
        for cand in _lexical_index(config.DOCS_COLLECTION_NAME).search(question, config.HYBRID_CANDIDATE_POOL)
        if _clearance_allows(cand[2], clearance)
    ]
    return _rrf_fuse(results, sparse, top_k)


def format_docs_context(results: list[ScoredPoint]) -> str:
    """
    Renders document chunks for the LLM prompt.
    Includes source file, page/sheet, and heading so the model can cite properly.
    """
    if not results:
        return "No relevant document content found."

    parts = []
    for i, hit in enumerate(results, 1):
        p = hit.payload
        source_parts = [p.get("file_name", "unknown")]
        if p.get("page"):
            source_parts.append(f"page {p['page']}")
        if p.get("sheet"):
            source_parts.append(f"sheet '{p['sheet']}'")
        if p.get("heading"):
            source_parts.append(f"section '{p['heading']}'")
        source = ", ".join(source_parts)

        parts.append(
            f"DOCUMENT ENTRY {i}\n"
            f"Source: {source}\n"
            f"{p.get('text', '')}"
        )
    return "\n\n".join(parts)


def format_docs_context_debug(results: list[ScoredPoint]) -> str:
    parts = []
    for i, hit in enumerate(results, 1):
        p = hit.payload
        parts.append(
            f"[{i}] {p.get('file_name')} "
            f"(score: {hit.score:.3f}, chunk {p.get('chunk_index')}/{p.get('chunk_total')})\n"
            f"{p.get('text', '')[:200]}..."
        )
    return "\n\n---\n\n".join(parts)
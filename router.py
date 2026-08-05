import re
import ollama
import config

_client = ollama.Client(host=config.OLLAMA_HOST)

_ROUTER_SYSTEM = """\
You are a query router for a data governance assistant at Fraser Health Authority.
You have these knowledge sources and capabilities:
  1. STRUCTURED — the internal Data Dictionary: table names, column names, data types,
     nullability, descriptions, schemas. Questions about database structure/metadata.
     This ONLY covers the MetadataRepository database (known schemas: mdm, stg, gov,
     dm_dq, dq, sec, dbo, rpt).
  2. UNSTRUCTURED — uploaded documents: HR policies, procedures, reports, guidelines,
     Excel data files, Word documents, PDFs, AND internal technical documentation
     (e.g. API Engine internals, Workflow Engine internals, SQL Server tuning guides,
     stored procedure references, encryption utilities, team backlogs). Table names,
     column names, stored procedure names, or SQL syntax mentioned INSIDE these
     documents are still UNSTRUCTURED — they describe application internals or teach
     SQL concepts, they are NOT the live MetadataRepository DataDictionary.
  3. SQL — execute a live SELECT query against the database to retrieve actual data
     values, counts, aggregates, or records. Use this when the question needs real
     data from the database, not just descriptions of the schema.

Classify the user's question into exactly one of these five labels:
  structured    → question is about the MetadataRepository Data Dictionary schema,
                   table structure, or column definitions
  unstructured  → question is about document content — including technical docs that
                   mention table names, stored procedures, or SQL syntax as subject
                   matter to explain, not as a live query to run
  both          → question requires both Data Dictionary schema descriptions and
                   document content
  general       → greeting, small talk, thanks, or question about the assistant itself
                   (NOT a definitional question about a documented system, and NOT a
                   question about what a specific person is working on)
  sql           → question needs actual data values from the MetadataRepository
                   database (counts, records, aggregates, lookups) — NOT a request to
                   explain what a document says about SQL syntax or stored procedures

When a question mentions dot-notation like "core.Connection" or "api.CallQueue", or
SQL Server concepts like execution plans, DMVs, or SARGability, check: is this asking
you to run a live query or describe the MetadataRepository schema (structured/sql), or
is it asking what an uploaded document explains (unstructured)? Default to unstructured
unless the question explicitly references MetadataRepository/DataDictionary concepts or
asks for current data values.

IMPORTANT: A question asking what a specific named person (a coworker/employee name,
e.g. "What is Priya working on?", "Who owns the X item?") is doing, owns, or is
assigned is ALWAYS unstructured (it is answered from the team backlog document) — it
is never "general", even though it resembles a casual conversational question. Only
classify as "general" if the message does not ask about any person, system, table, or
document topic at all (e.g. pure greetings, thanks, or meta questions about you).

IMPORTANT: Asking how many COLUMNS a table has, or what columns/fields a table has, is
STRUCTURED — the Data Dictionary already stores one row per column, so this is a schema
lookup, never a live query. This applies even when phrased as "how many" (e.g. "how many
columns does X have"). Contrast with asking how many ROWS a table has, or any question
about the table's actual data/values — that IS "sql", since it needs a live query against
real data, not schema metadata. "Columns" = structured. "Rows"/"records"/actual data = sql.

Output ONLY the label — one word, lowercase, no punctuation, no explanation.
"""

_EXAMPLES = """
Examples:
Q: hello → general
Q: hi there → general
Q: thanks! → general
Q: what can you help me with? → general
Q: who are you? → general
Q: What columns does the RuleTargets table have? → structured
Q: What is the data type of AssetId? → structured
Q: What tables are in the dq schema? → structured
Q: What is Fraser Health's leave policy? → unstructured
Q: How many vacation days do employees get? → unstructured
Q: What does the Q3 report say about data quality? → unstructured
Q: Which tables store employee data and what is the leave entitlement? → both
Q: Tell me about PII columns and the privacy policy → both
Q: How many active rules are there? → sql
Q: How many tables are in the MetadataRepository database? → sql
Q: What are the most recently scanned targets? → sql
Q: Show me all rules that are currently inactive → sql
Q: How many rule targets exist per asset? → sql
Q: What is the total number of columns across all tables? → sql
Q: What is the API Engine and what problem does it solve? → unstructured
Q: What is the Workflow Engine and what does it execute? → unstructured
Q: What stored procedure does a worker thread call to get its next task? → unstructured
Q: What are the required fields in a core.Connection row? → unstructured
Q: What does StateID = 4 mean in api.CallQueue? → unstructured
Q: What should you look for in an execution plan to spot a non-SARGable query? → unstructured
Q: Which DMVs are used to monitor SQL Server wait stats and missing indexes? → unstructured
Q: What SQL statement does the utility run to update a password? → unstructured
Q: What's the difference between the FULL, SIMPLE, and BULK_LOGGED recovery models? → unstructured
Q: What is Tanveer currently working on? → unstructured
Q: What priority is the ICIMS Resume item, and who owns it? → unstructured
Q: Who is responsible for the Environment Design project? → unstructured
Q: How many columns does the consistency_runs table have? → structured
Q: What columns does consistency_runs have? → structured
Q: How many rows are in consistency_runs? → sql
Q: How many rows does the scan_queue table have? → sql
Q: Where should draft or imported lineage be stored? → structured
Q: Where should validated lineage be stored? → structured
Q: What table should I use to store a business glossary term? → structured
Q: What table maps glossary terms to assets or fields? → structured
Q: Which tables support security audit evidence? → structured
"""

_DATA_ROUTES = ("structured", "unstructured", "both", "sql")

# A short follow-up that leans on a pronoun ("those", "that one", "them") cannot be
# understood in isolation. The LLM tends to file it under 'general', which in chat.py
# short-circuits BEFORE any topic tracking or retrieval runs. When the conversation is
# already in an active data route, such a reference should continue that route instead.
_PRONOUN_FOLLOWUP_RE = re.compile(
    r"\b(those|these|that|this|it|its|them|the same|the one|the ones)\b",
    re.IGNORECASE,
)


def route(question: str, last_route: str | None = None) -> str:
    """Classify a question into structured / unstructured / both / general / sql.

    `last_route` carries the previous turn's route so a context-dependent follow-up
    ("what about those?") continues the active conversation instead of being judged as
    a bare sentence. Falls back to 'both' on any error or unexpected output.

    Design note: the conversation context is applied as a DETERMINISTIC floor, not by
    feeding context into the classifier prompt. Injecting "this is a follow-up, don't
    say general" into the LLM was tried and dragged genuine greetings ("hello",
    "thanks!") into the prior data route. The floor below is precise — it only rescues
    a message that (a) the LLM already called 'general' AND (b) leans on a pronoun with
    no standalone subject — so greetings/meta (no pronoun) are untouched.
    """
    # Deterministic pre-check (before the LLM, so it can't shift other classifications):
    # catalog-wide "which tables/columns are missing descriptions / how complete" questions
    # need live SQL against rpt.DataDictionary — the description-less rows aren't in the
    # vector store, so the structured RAG path literally can't answer them.
    from planner import is_description_audit_question
    if is_description_audit_question(question):
        return "sql"

    try:
        resp = _client.chat(
            model=config.ROUTER_MODEL,
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM + _EXAMPLES},
                {"role": "user",   "content": f"Q: {question}"},
            ],
            options={"temperature": 0, "num_predict": 10},
        )
        label = resp["message"]["content"].strip().lower().split()[0]
        if label not in ("structured", "unstructured", "both", "general", "sql"):
            label = "both"
    except Exception:
        return "both"

    # Deterministic floor: a pronoun-based follow-up inside an active data conversation
    # must never collapse to 'general' (which in chat.py skips topic tracking + retrieval
    # entirely). Inherit the prior route. Kept deterministic so classifier variance can't
    # reintroduce the bug, and narrow (pronoun required) so greetings stay 'general'.
    if label == "general" and last_route in _DATA_ROUTES and _PRONOUN_FOLLOWUP_RE.search(question):
        return last_route
    return label


_CONJUNCTION_RE = re.compile(r"\b(and|also|as well as|plus|additionally)\b|;", re.IGNORECASE)
_AGG_CUE_RE = re.compile(
    r"\b(how many|how much|number of|count of|count the|total number|total count|"
    r"average|avg|sum of|percentage of|what percentage|how long)\b",
    re.IGNORECASE)

_DECOMPOSE_SYSTEM = """You split a user question into independent sub-questions ONLY when it clearly asks for TWO OR MORE SEPARATE things that need different answers or data sources (for example a data count AND a documentation topic).

Output rules:
- If it is really ONE request, output exactly: SINGLE
- A single request that uses "and" only to list attributes, compare values, or return extra columns of ONE thing is SINGLE. Examples -> SINGLE:
    "show me failed jobs and their error messages"
    "how many nullable and non-nullable columns are there"
    "what retry behavior is documented across the API engine and workflow engine"
- Split only when the parts are about genuinely different things. Example:
    "how many active rules are there and what does the workflow engine documentation say about affinities"
    ->
    how many active rules are there
    what does the workflow engine documentation say about affinities
- Output each self-contained sub-question on its own line. No numbering, no other text."""


def decompose_question(question: str) -> list[str] | None:
    """Return 2+ independent sub-questions for genuine multi-intent, else None.
    Gated: LLM split runs only when a conjunction AND an aggregate cue are present,
    so pure-docs 'and' questions and 'show me X and Y' never reach it."""
    if not (_CONJUNCTION_RE.search(question) and _AGG_CUE_RE.search(question)):
        return None
    try:
        resp = _client.chat(
            model=config.ROUTER_MODEL,
            messages=[
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user",   "content": f"Q: {question}"},
            ],
            options={"temperature": 0, "num_predict": 120},
        )
        out = resp["message"]["content"].strip()
    except Exception:
        return None
    if out.upper().startswith("SINGLE"):
        return None
    subs = [re.sub(r"^[-*\d.)\s]+", "", ln).strip() for ln in out.splitlines() if ln.strip()]
    subs = [s for s in subs if s and not s.upper().startswith("SINGLE")]
    return subs if len(subs) >= 2 else None
# router.py
# LLM-based query router that classifies each question into one of five paths:
#   "structured"   → data_dictionary collection (schema/column questions)
#   "unstructured" → documents collection (policy/content questions)
#   "both"         → retrieve from both, merge context
#   "general"      → no retrieval — greetings, small talk, meta questions
#   "sql"          → generate and execute a SQL SELECT query
#
# Uses llama3.1:8b at temperature=0 — classification, not creative work.
# Falls back to "both" on error/unexpected output.

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
"""

def route(question: str) -> str:
    """
    Classifies a question into one of: structured, unstructured, both, general, sql.
    Falls back to 'both' on any error or unexpected output.
    """
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
        if label in ("structured", "unstructured", "both", "general", "sql"):
            return label
        return "both"
    except Exception:
        return "both"
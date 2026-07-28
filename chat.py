# Commands:
#   /sources  — show rewritten query + retrieved chunks for the last question
#   /reset    — clear conversation history and start fresh
#   /help     — show commands
#   /quit     — exit

import ollama
from rich.console import Console
import retriever
import router
import sql_generator
import config

console = Console()

SYSTEM_PROMPT = """\
You are a data governance assistant for Fraser Health Authority.
You have access to two knowledge sources:

1. DATA DICTIONARY — describes every table, schema, and column in the \
MetadataRepository database (structured metadata).
2. DOCUMENTS — HR policies, procedures, reports, guidelines, and data files \
uploaded by the team (unstructured content).

Context entries are labelled either ENTRY (Data Dictionary) or \
DOCUMENT ENTRY (uploaded documents) so you know which source you are drawing from.

STRICT RULES — follow these exactly:

1. Answer using ONLY the context entries provided. Never guess or infer \
   information that is not explicitly present in the context.

2. If the answer is not in the context, say exactly: \
   "I don't have that information in the provided context." \
   Do not reference, speculate about, or cite chunks that are unrelated to \
   the question.

3. NEVER reproduce or mirror the raw context entries in your answer. \
   Always write a proper natural language response using the information \
   from the context. The context is source material — not your output. \
   Do not output raw key-value pairs like "Data type: bit\nMax length: 1".

4. When a question requires comparing or checking a condition across multiple \
   columns (e.g. "which columns have X and Y both false"), you MUST read \
   every context entry and evaluate each one individually before answering. \
   Check is_nullable and is_identity directly from each entry's values — \
   do not guess or infer. List only entries where ALL conditions are true.

5. NEVER infer database constraints that are not explicitly stored in the \
   Data Dictionary. Specifically:
   - Do NOT infer primary keys from identity columns or NOT nullable columns.
   - Do NOT infer foreign key constraint names from column descriptions.
   - Do NOT infer index definitions, default values, or check constraints.
   - Do NOT infer relationships between tables beyond what column descriptions \
     explicitly state.
   If asked about any of these, apply rule 2 and refuse cleanly.

6. When answering about a specific table or column, include:
   - Full table name (schema.table)
   - Column name
   - Data type
   - Nullable or NOT nullable
   - Whether it is an identity column (if relevant)
   - max_length, precision, scale (if the question involves those)

7. When answering from DOCUMENT ENTRIES, always cite the source file name \
   and section/page so the user knows where the information came from.

8. Never contradict yourself. If you cite a source, it must support the claim.

9. You have memory of this conversation. Refer to prior turns naturally when \
   relevant, but always ground your answer in the retrieved context entries.
"""


# ── General conversation prompt (no retrieval — greetings, small talk) ───────

GENERAL_SYSTEM_PROMPT = """\
You are a data governance assistant for Fraser Health Authority.
You help answer questions about the Data Dictionary (database schema) and \
uploaded documents (HR policies, reports, guidelines).

The user's current message is general conversation — a greeting, small talk, \
thanks, or a question about what you can do. Respond naturally and briefly.

If asked what you can help with, mention you can answer questions about:
- Database tables, columns, and schemas (Data Dictionary)
- Uploaded HR policies, procedures, and reports

Keep responses short and friendly. Do not fabricate information about \
specific tables, columns, or document contents in this mode — if the user \
asks something requiring real data, let them know you'll need them to ask \
that as a specific question.
"""


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_user_message(question: str, context: str) -> str:
    return (
        f"Context from Data Dictionary:\n\n{context}\n\n"
        f"---\n\nQuestion: {question}"
    )


# ── Core ask function ─────────────────────────────────────────────────────────

def ask(
    question: str,
    history: list[dict],
    show_sources: bool = False,
    topic_table: str | None = None,
    last_sql: str | None = None,
    last_intent: str | None = None,
    last_route: str | None = None,
    clearance=None,
) -> tuple[str, str | None, str, str | None, str | None]:
    """
    Full RAG pipeline for one turn.
    Returns (answer, topic_table, route, last_sql, last_intent).
    last_sql is updated when route == 'sql' so follow-up questions
    stay anchored to the same table.

    `last_intent` tracks whether the previous structured/both turn was a
    "list columns" question. Only meaningful value today is "list_columns";
    None means "something else / not applicable". It persists unchanged across
    general/sql/unstructured turns (same convention as topic_table/last_sql) and
    is only set/reset on structured/both turns. This lets a bare topic-switch
    follow-up ("how about the scan_queue", no "columns" in it) still get the
    FULL column list instead of a partial vector-search result, as long as the
    conversation was already in a list-columns context.

    `clearance` is the caller's allowed clearance set (audit #7); None falls back
    to config.DEFAULT_CLEARANCE. It gates Qdrant retrieval deny-by-default. NOTE:
    real end-user RBAC (per-user identity -> roles -> SQL Server RLS on the query
    path) is the documented next step; this call still runs under one app identity.
    """
    if clearance is None:
        clearance = config.DEFAULT_CLEARANCE
    old_topic_table = topic_table
    last_useful_reply = None
    assistant_turns = [m["content"] for m in reversed(history) if m["role"] == "assistant"]
    for reply in assistant_turns[:3]:
        if retriever._COLUMN_PATTERN.search(reply) or retriever._TABLE_PATTERN.search(reply):
            last_useful_reply = reply
            break

    # ── Route ─────────────────────────────────────────────────────────────────
    # Computed before topic_table tracking below, since bare-table-name resolution
    # is gated on the route (only worth a Data Dictionary lookup when the question
    # actually landed in DataDictionary territory).
    route = router.route(question, last_route=last_route)

    # Update topic_table from the user's question
    table_match = retriever._TABLE_PATTERN.search(question)
    if table_match:
        schema, obj = table_match.group(1), table_match.group(2)
        if schema.lower() not in ("information_schema", "sys", "dbo"):
            topic_table = f"{schema}.{obj}"
    elif route in ("structured", "both", "sql"):
        resolved = retriever.resolve_bare_table_name(question, clearance=clearance)
        if resolved:
            topic_table = f"{resolved[0]}.{resolved[1]}"

    # ── General chit-chat skip retrieval entirely ──────────────────────────
    if route == "general":
        if show_sources:
            console.print(f"[dim]Route: [bold]general[/bold] — no retrieval[/dim]")

        history.append({"role": "user", "content": question})
        messages = [{"role": "system", "content": GENERAL_SYSTEM_PROMPT}] + history

        client = ollama.Client(host=config.OLLAMA_HOST)
        response = client.chat(
            model=config.CHAT_MODEL,
            messages=messages,
            stream=True,
            options={"temperature": 0.5},
        )

        console.print()
        console.print("[bold green]Assistant[/bold green]")
        full_response = ""
        for chunk in response:
            token = chunk["message"]["content"]
            full_response += token
            print(token, end="", flush=True)
        print()

        history.append({"role": "assistant", "content": full_response})
        return full_response, topic_table, route, last_sql, last_intent

    # ── SQL path — generate, execute, answer ──────────────────────────────────
    if route == "sql":
        if show_sources:
            console.print(f"[dim]Route: [bold]sql[/bold][/dim]")

        console.print()
        console.print("[bold green]Assistant[/bold green]")
        console.print("[dim]Generating query...[/dim]")

        answer, sql_used, rows, columns = sql_generator.run_sql_pipeline(question, last_sql, clearance=clearance)

        if sql_used not in ("INSUFFICIENT_SCHEMA",):
            console.rule("[dim]Generated SQL[/dim]")
            console.print(f"[cyan]{sql_used}[/cyan]")
            console.rule()

        if rows:
            console.print()
            console.print(sql_generator.format_results_table(rows, columns))
            console.print()

        print(answer)

        # Store in history for conversational memory
        history.append({"role": "user", "content": question})
        history.append({
            "role": "assistant",
            "content": f"[SQL: {sql_used}]\n\nResults: {len(rows)} rows\n\n{answer}"
        })
        new_last_sql = sql_used if sql_used not in ("INSUFFICIENT_SCHEMA",) else last_sql
        return answer, topic_table, route, new_last_sql, last_intent

    # ── Retrieve based on route ───────────────────────────────────────────────
    context_parts = []
    rewritten_query = question

    if route in ("structured", "both"):
        # "What tables are in schema X" is an enumeration question: answer it from an
        # exhaustive scroll, not partial vector search (which returned the wrong count).
        tables_hit = retriever.resolve_list_tables_question(question, clearance=clearance)
        topic_switched = topic_table != old_topic_table
        want_full_columns = retriever.is_list_columns_question(question) or (
            topic_switched
            and last_intent == "list_columns"
            and not retriever._COLUMN_PATTERN.search(question)
        )
        # Comparison / multi-table questions name 2+ tables ("difference between
        # mdm.Assets and mdm.Columns"). Give the model the FULL schema of each named
        # table, instead of the topic_table filter starving all but the first-named
        # one (which made comparisons falsely answer "I don't have that").
        named_tables: list[tuple[str, str]] = []
        for _s, _o in retriever._TABLE_PATTERN.findall(question):
            if _s.lower() not in ("information_schema", "sys") and (_s, _o) not in named_tables:
                named_tables.append((_s, _o))
        multi_blocks = []
        if len(named_tables) >= 2:
            for _s, _o in named_tables[:3]:
                _cols = retriever.fetch_all_columns(_s, _o, clearance=clearance)
                if _cols:
                    multi_blocks.append(retriever.format_all_columns_context(_cols))
            if len(multi_blocks) < 2:
                multi_blocks = []  # fewer than 2 resolved to real tables — not a comparison

        if tables_hit:
            schema_name, tables = tables_hit
            structured_context = retriever.format_all_tables_context(schema_name, tables)
            rewritten_query = f"[schema tables] {schema_name}"
            structured_results = []
            last_intent = None
        elif multi_blocks:
            structured_context = "\n\n".join(multi_blocks)
            rewritten_query = "[multi-table] " + ", ".join(f"{s}.{o}" for s, o in named_tables[:3])
            structured_results = []
            last_intent = None
        elif want_full_columns and topic_table and "." in topic_table:
            schema, obj = topic_table.split(".", 1)
            payloads = retriever.fetch_all_columns(schema, obj, clearance=clearance)
            structured_context = retriever.format_all_columns_context(payloads)
            rewritten_query = f"[full-table fetch] {topic_table}"
            structured_results = []
            last_intent = "list_columns"
        else:
            structured_results, rewritten_query = retriever.retrieve(
                question, last_useful_reply, topic_table, clearance=clearance
            )
            structured_context = retriever.format_context(structured_results)
            last_intent = None
        context_parts.append(structured_context)

    if route in ("unstructured", "both"):
        doc_results = retriever.retrieve_docs(question, clearance=clearance)
        docs_context = retriever.format_docs_context(doc_results)
        context_parts.append(docs_context)
    else:
        doc_results = []

    context = "\n\n".join(context_parts)

    if show_sources:
        console.print(f"[dim]Route: [bold]{route}[/bold][/dim]")
        if route in ("structured", "both"):
            if rewritten_query.startswith("[full-table fetch]"):
                console.rule("[dim]Full-table fetch[/dim]")
                console.print(f"[dim]Table: {topic_table}[/dim]")
            else:
                console.rule("[dim]Query rewriting[/dim]")
                console.print(f"[dim]Original:  {question}[/dim]")
                console.print(f"[dim]Rewritten: {rewritten_query}[/dim]")
                if structured_results:
                    console.rule("[dim]Structured chunks[/dim]")
                    console.print(retriever.format_context_debug(structured_results), style="dim")
        if route in ("unstructured", "both") and doc_results:
            console.rule("[dim]Document chunks[/dim]")
            console.print(retriever.format_docs_context_debug(doc_results), style="dim")
        console.rule()

    user_message = build_user_message(question, context)
    history.append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    client = ollama.Client(host=config.OLLAMA_HOST)
    response = client.chat(
        model=config.CHAT_MODEL,
        messages=messages,
        stream=True,
        options={"temperature": 0.1},
    )

    console.print()
    console.print("[bold green]Assistant[/bold green]")
    full_response = ""
    for chunk in response:
        token = chunk["message"]["content"]
        full_response += token
        print(token, end="", flush=True)
    print()

    history.append({"role": "assistant", "content": full_response})
    return full_response, topic_table, route, last_sql, last_intent


# ── CLI ───────────────────────────────────────────────────────────────────────

def print_help():
    console.print(
        "  [bold]/sources[/bold]  — show rewritten query + retrieved chunks for last question\n"
        "  [bold]/reset[/bold]    — clear conversation history\n"
        "  [bold]/help[/bold]     — show this message\n"
        "  [bold]/quit[/bold]     — exit\n",
        style="dim",
    )


def main():
    console.rule("[bold cyan]DataDictionary RAG — Fraser Health Authority[/bold cyan]")
    console.print("Ask questions about tables, columns, schemas, or data types.\n")
    print_help()

    history: list[dict] = []
    last_question: str | None = None
    topic_table: str | None = None
    last_sql: str | None = None
    last_intent: str | None = None
    last_route: str | None = None

    while True:
        try:
            user_input = console.input("[bold cyan]You:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
            console.print("[dim]Goodbye.[/dim]")
            break

        if user_input.lower() == "/reset":
            history.clear()
            last_question = None
            topic_table = None
            last_sql = None
            last_intent = None
            last_route = None
            console.print("[dim]Conversation history cleared.[/dim]\n")
            continue

        if user_input.lower() == "/sources":
            if last_question:
                ask(last_question, [], show_sources=True, topic_table=topic_table,
                    last_sql=last_sql, last_intent=last_intent, last_route=last_route,
                    clearance=config.APP_CLEARANCE)
            else:
                console.print("[dim]No previous question to show sources for.[/dim]")
            continue

        if user_input.lower() == "/help":
            print_help()
            continue

        last_question = user_input
        _, topic_table, last_route, last_sql, last_intent = ask(
            user_input, history, topic_table=topic_table, last_sql=last_sql,
            last_intent=last_intent, last_route=last_route, clearance=config.APP_CLEARANCE,
        )
        console.print(
            f"[dim](turns: {len(history) // 2}  |  "
            f"route: {last_route}  |  "
            f"topic: {topic_table or 'none'}  |  "
            f"/sources to inspect  |  /reset to clear)[/dim]\n"
        )


if __name__ == "__main__":
    main()
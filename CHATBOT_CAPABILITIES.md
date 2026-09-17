# Data Governance Assistant — What You Can Ask

A quick guide so you can throw realistic questions and conversations at the chatbot.
**Please just ask naturally** — including follow-up questions, changing your mind,
switching topics mid-conversation. That's exactly the kind of thing that finds
problems. If an answer looks wrong, copy the exact question + answer and flag it.

The assistant can draw on **three** things:

---

## 1. Database structure (the Data Dictionary)

It knows the schema of the **MetadataRepository** database — every table, column,
data type, whether columns are nullable, identity columns, and the descriptions —
across **43 tables in 7 areas**:

| Area | What's in it | Example tables |
|---|---|---|
| **dq** | Data quality rules & results | Rules, Results, RuleTargets, Executions |
| **dm_dq** | Data quality scan operations | scan_queue, consistency_runs, validity_scan_result, row_count_snapshots |
| **gov** | Governance | AssetOwnership, PiiTypes, SensitivityLabels, CriticalDataElements, CertifiedDatasets, ColumnClassifications |
| **mdm** | Master data / catalog | Assets, Systems, GlossaryTerms, LineageMappings, Processes, ScanBatches, Columns |
| **sec** | Security & access | AccessRequests, AccessApprovals, AuditEvents, ErrorLog |
| **stg** | Staging / import | SqlTables, SqlColumns, ScanControl, CatalogExports |
| **dbo** | Misc | profiles |

**Things you can ask (answered from the dictionary, no live query needed):**
- "What columns does the `scan_queue` table have?"
- "How many columns does `consistency_runs` have?"
- "What tables are in the `gov` schema?"
- "What's the data type of the `PassRate` column?"
- "Is the `job_id` column nullable? Is it an identity column?"
- "What does the `RuleTargets` table store?"
- "Which columns in `Results` can be null?"

---

## 2. Live data from the database (read-only)

For questions that need **actual numbers or records**, it writes and runs a live
read-only query. It can count, filter, group, average, sort, etc.

**Things you can ask:**
- "How many rows are in `consistency_runs`?"
- "How many active rules are there?"
- "How many rules are there per rule type?"
- "What's the average pass rate across all results?"
- "How many data quality results have failed rows greater than zero?"
- "Show me the 5 most recent scan jobs."
- "How many audit events, broken down by event type?"
- "How many rows of data are there across every table in the `dm_dq` schema?"
- "Which table in `dm_dq` has the most rows?"

It's **read-only** — it can look things up but can never change, add, or delete data.

---

## 3. Internal technical documentation

It has **6 internal docs** loaded and can answer questions about them and cite the source:

| Document | Topic |
|---|---|
| **API Engine** | The multithreaded C# service that runs REST API calls from a queue |
| **Workflow Engine** | The job/step orchestration Windows service |
| **Workflow Connection Encryption Utility** | Encrypting connection passwords (AES) |
| **SARGability Guide** | Writing index-friendly SQL Server queries |
| **SQL Server Performance Configuration** | Server tuning (memory, MAXDOP, TempDB, etc.) |
| **Team Backlog / Status** | Who's working on what, project status |

**Things you can ask:**
- "What is the API Engine and what problem does it solve?"
- "What stored procedure does a worker thread call to get its next task?"
- "What does StateID = 4 mean in api.CallQueue?"
- "Why is `WHERE YEAR(OrderDate) = 2025` non-SARGable, and what's the fix?"
- "What's the recommended max server memory for a 64 GB server?"
- "What environment variable stores the AES key?"
- "What is Tanveer working on?" / "Who owns the ICIMS Resume item?"

---

## Having a conversation (follow-ups work)

You don't have to restate everything each time — it remembers the current topic:

> **You:** What columns does `scan_queue` have?
> **Bot:** *(lists all 8 columns)*
> **You:** how about `consistency_runs`?          ← switches topic, lists its columns
> **You:** which of those are nullable?           ← "those" = consistency_runs' columns
> **You:** how many rows does it have?            ← "it" = consistency_runs, runs a live count

**Please test this heavily** — ask a question, then follow up with "what about…",
"and the…?", "which of those…", "how many rows does it have", switch to a different
table, ask a doc question in the middle, then come back. Mixed, messy, real
conversations are the most useful.

---

## What it can't do (yet) — so you don't chase these

- **HR / leave / vacation / policy questions.** People assume these work — they
  **don't right now.** The HR data and leave-policy documents aren't loaded into the
  assistant yet, so it'll say it doesn't have that information. (If HR content is a
  priority, that's a quick add — let us know.)
- **Other databases or systems.** It only knows the one MetadataRepository database
  and the 6 docs above.
- **General knowledge, web search, coding help, math** — it's not a general chatbot.
- **Changing data** — strictly read-only.
- **Very elaborate analytical SQL** may occasionally come out wrong (it's a small
  local model). Simple counts, lookups, filters, and group-bys are reliable; if a
  complicated one looks off, that's great to flag.

---

## How to help us test

1. Ask **naturally**, the way you actually would — don't try to phrase it "correctly."
2. Use **follow-ups** and **switch topics** mid-conversation.
3. When something's wrong (wrong number, wrong table, made-up info, "I don't have
   that" when it should), **copy the exact question and the exact answer** and send it over.
4. Weird/ambiguous phrasings are welcome — those find the most bugs.

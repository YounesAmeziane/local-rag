# DataDictionary RAG — Setup & Usage

## Prerequisites

- Python 3.10+
- Qdrant binary running on `localhost:6333`
- Ollama running on `localhost:11434`
- ODBC Driver 17 or 18 for SQL Server installed
- Access to `[MetadataRepository].[rpt].[DataDictionary]` via Windows Authentication

---

## 1. Ollama — pull required models

```bash
ollama pull nomic-embed-text
ollama pull llama3.1:8b
```

---

## 2. Python environment

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows

pip install -r requirements.txt
```

---

## 3. Configure

Edit `config.py` — set your SQL Server name:

```python
DB_SERVER = r"YOUR_SERVER_NAME"   # e.g. "localhost" or "DESKTOP-ABC\SQLEXPRESS"
```

Everything else works out of the box if Qdrant and Ollama are on defaults.

---

## 4. Ingest

Pulls from SQL Server, embeds every row, loads into Qdrant.
Run once, or re-run any time the DataDictionary changes.

```bash
python ingest.py
```

Expected output:
```
── DataDictionary → Qdrant Ingestion ──
✓ Loaded 1,234 rows from DataDictionary
Built 1,234 chunks — starting embedding...
Embedding chunks... ━━━━━━━━━━━━━━━━━━━━ 100%
✓ Upserted 1,234 points into 'data_dictionary'
── Ingestion complete ──
```

---

## 5. Chat

```bash
python chat.py
```

### Example questions

```
What does the RuleTargets table do?
What columns are in the dq schema?
Is the RuleTargetId column an identity column?
Which columns are nullable in RuleTargets?
What is the purpose of the AssetId column?
```

### Commands

| Command    | Effect                                      |
|------------|---------------------------------------------|
| `/sources` | Re-run last question, show retrieved chunks |
| `/quit`    | Exit                                        |

---

## Project structure

```
rag_datadictionary/
├── config.py        # All settings (server, models, collection name, top-k)
├── ingest.py        # SQL Server → embed → Qdrant
├── retriever.py     # Query-time embedding + Qdrant search
├── chat.py          # CLI chat loop
├── requirements.txt
└── README.md
```

---

## Tuning

| Setting          | File       | Default            | Notes                              |
|------------------|------------|--------------------|------------------------------------|
| `TOP_K`          | config.py  | 8                  | Chunks fed to LLM. Raise for broader questions, lower for precision. |
| `CHAT_MODEL`     | config.py  | `llama3.1:8b`      | Swap for any Ollama model          |
| `EMBED_MODEL`    | config.py  | `nomic-embed-text` | 768-dim; change `VECTOR_SIZE` too if swapping |
| `COLLECTION_NAME`| config.py  | `data_dictionary`  | Re-ingest after changing           |

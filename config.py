# config.py — all settings loaded from .env
# Copy .env.example to .env and fill in your values before running.

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── SQL Server ────────────────────────────────────────────────────────────────
DB_SERVER   = os.getenv("DB_SERVER", "localhost")
DB_DATABASE = os.getenv("DB_DATABASE", "MetadataRepository")
DB_DRIVER   = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")

INGEST_QUERY = """
    SELECT *
    FROM [MetadataRepository].[rpt].[DataDictionary]
    WHERE ObjectDescription IS NOT NULL
      AND ColumnDescription IS NOT NULL
"""

# ── Qdrant ────────────────────────────────────────────────────────────────────
QDRANT_HOST           = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT           = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME       = "data_dictionary"
DOCS_COLLECTION_NAME  = "documents"
VECTOR_SIZE           = 768

# ── Ollama ────────────────────────────────────────────────────────────────────
EMBED_MODEL           = "nomic-embed-text"
CHAT_MODEL            = "llama3.1:8b"
ROUTER_MODEL          = "llama3.1:8b"
OLLAMA_HOST           = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# ── Retrieval ─────────────────────────────────────────────────────────────────
TOP_K                 = 5
DOCS_TOP_K            = 5

# ── Document ingestion ────────────────────────────────────────────────────────
DOCS_FOLDER           = "docs"
CHUNK_MAX_TOKENS      = 512
CHUNK_OVERLAP_TOKENS  = 50
XLSX_ROWS_PER_CHUNK   = 20
XLSX_PROSE_THRESHOLD  = 0.30
XLSX_PROSE_MIN_CHARS  = 50
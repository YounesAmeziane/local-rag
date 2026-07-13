# config.py — all settings loaded from .env
# Copy .env.example to .env and fill in your values before running.

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── SQL Server ────────────────────────────────────────────────────────────────
# DB_SERVER is REQUIRED and has NO default — a silent fallback to "localhost"
# would point at the wrong server without any error (audit #10). Validated below.
DB_SERVER   = os.getenv("DB_SERVER", "").strip()
DB_DATABASE = os.getenv("DB_DATABASE", "MetadataRepository")
DB_DRIVER   = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")

# Multi-step planner: schemas the enumeration tools will refuse to list.
# Comma-separated in .env. Defaults to system/utility schemas.
PLANNER_SCHEMA_DENYLIST = [
    s.strip()
    for s in os.getenv("PLANNER_SCHEMA_DENYLIST", "sys,INFORMATION_SCHEMA,guest").split(",")
    if s.strip()
]

INGEST_QUERY = """
    SELECT *
    FROM [MetadataRepository].[rpt].[DataDictionary]
    WHERE ObjectDescription IS NOT NULL
      AND ColumnDescription IS NOT NULL
"""

# ── Qdrant ────────────────────────────────────────────────────────────────────
# Default to 127.0.0.1, NOT "localhost": on Windows "localhost" resolves to IPv6
# ::1 first and stalls ~2s per call before falling back to IPv4 (audit #1).
QDRANT_HOST           = os.getenv("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT           = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_GRPC_PORT      = int(os.getenv("QDRANT_GRPC_PORT", "6334"))
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


# ── Startup validation (audit #10) ────────────────────────────────────────────
# Fail loud on misconfiguration at import instead of silently running against the
# wrong host. Kept minimal on purpose: only DB_SERVER has no safe default.
if not DB_SERVER:
    raise RuntimeError(
        "config: DB_SERVER is not set. Copy .env.example to .env and set DB_SERVER "
        "-- refusing to silently fall back to 'localhost'."
    )

if QDRANT_HOST == "localhost":
    import sys
    print(
        "config WARNING: QDRANT_HOST='localhost' incurs a ~2s IPv6 (::1) stall per "
        "Qdrant call on Windows -- set QDRANT_HOST=127.0.0.1 (audit #1).",
        file=sys.stderr,
    )
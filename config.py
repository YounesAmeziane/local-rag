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

# Dedicated read-only SQL login for the query path (audit #3). When both are set,
# execute_sql connects as this login (SELECT-only) instead of the service's
# Windows identity. Setup: sql/create_readonly_login.sql.
DB_READONLY_USER     = os.getenv("DB_READONLY_USER", "").strip()
DB_READONLY_PASSWORD = os.getenv("DB_READONLY_PASSWORD", "")

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
# Use 127.0.0.1, NOT "localhost", for the same Windows IPv6 (::1) reason as
# QDRANT_HOST (audit #1). The connection pool hides it at the median but the tail
# is worse — measured embed stalls up to ~8s on localhost vs ~85ms on 127.0.0.1.
OLLAMA_HOST           = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# ── Retrieval ─────────────────────────────────────────────────────────────────
TOP_K                 = 5
DOCS_TOP_K            = 5

# ── Access control (audit #7) ─────────────────────────────────────────────────
# Minimal clearance model. Full RBAC — roles / user_role_map / resource_permissions
# tables + SQL Server row-level security — is a documented NEXT STEP (see AUDIT.md
# #2); this only gates Qdrant retrieval. Every point carries a `clearance` label and
# retrieval is DENY-BY-DEFAULT: a point whose clearance is not in the caller's
# allowed set — including any untagged point — is excluded.
DEFAULT_CLEARANCE = ("general",)          # what a caller with no explicit grant sees
# Doc filename substrings whose chunks are tagged 'restricted' at ingest.
RESTRICTED_DOC_PATTERNS = [
    p.strip().lower()
    for p in os.getenv("RESTRICTED_DOC_PATTERNS", "fha_hr_data,fha_leave_policy").split(",")
    if p.strip()
]
# Clearance the local CLI (chat.py) runs with. Deny-by-default means the CLI sees
# only 'general' unless the operator elevates here (e.g. APP_CLEARANCE=general,restricted).
APP_CLEARANCE = tuple(
    p.strip() for p in os.getenv("APP_CLEARANCE", ",".join(DEFAULT_CLEARANCE)).split(",")
    if p.strip()
)

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

if QDRANT_HOST == "localhost" or "//localhost" in OLLAMA_HOST:
    import sys
    print(
        "config WARNING: a host is set to 'localhost', which incurs an intermittent "
        "multi-second IPv6 (::1) stall per call on Windows -- use 127.0.0.1 for "
        "QDRANT_HOST and OLLAMA_HOST (audit #1).",
        file=sys.stderr,
    )
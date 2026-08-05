import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DB_SERVER   = os.getenv("DB_SERVER", "").strip()
DB_DATABASE = os.getenv("DB_DATABASE", "MetadataRepository")
DB_DRIVER   = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")

DB_READONLY_USER     = os.getenv("DB_READONLY_USER", "").strip()
DB_READONLY_PASSWORD = os.getenv("DB_READONLY_PASSWORD", "")

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
OLLAMA_HOST           = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# ── Retrieval ─────────────────────────────────────────────────────────────────
TOP_K                 = 5
DOCS_TOP_K            = 5

# ── Hybrid lexical+dense retrieval (audit #6) ─────────────────────────────────
# When on, broad (non-topic-anchored) searches fuse dense cosine hits with a BM25
# lexical ranker via Reciprocal Rank Fusion, recovering exact technical-token
# matches that embeddings blur. Set HYBRID_SEARCH=0 to fall back to the exact
# dense-only behavior (the reversibility valve — nothing else changes).
HYBRID_SEARCH         = os.getenv("HYBRID_SEARCH", "1").strip().lower() not in ("0", "false", "no", "off")
HYBRID_CANDIDATE_POOL = int(os.getenv("HYBRID_CANDIDATE_POOL", "20"))  # per-ranker pool before fusion
RRF_K                 = int(os.getenv("RRF_K", "60"))                  # RRF damping constant

DEFAULT_CLEARANCE = ("general",)
RESTRICTED_DOC_PATTERNS = [
    p.strip().lower()
    for p in os.getenv("RESTRICTED_DOC_PATTERNS", "fha_hr_data,fha_leave_policy").split(",")
    if p.strip()
]
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
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

# ── Models ────────────────────────────────────────────────────────────────────
# Chat/reasoning: any OpenAI-compatible server on localhost (LM Studio default
# port 1234; Ollama exposes /v1 on 11434; vLLM on the server).
REASON_BASE_URL       = os.getenv("REASON_BASE_URL", "http://127.0.0.1:1234/v1")
REASON_MODEL          = os.getenv("REASON_MODEL", "qwen/qwen3.8-27b")
REASON_TIMEOUT        = float(os.getenv("REASON_TIMEOUT", "600"))

# Qwen3.8 thinking depth, chosen per session at startup.
# Reasoning tokens count against max_tokens, so a small cap can be consumed
# entirely by thinking and return empty content. llm.py adds this headroom.
REASONING_HEADROOM    = int(os.getenv("REASONING_HEADROOM", "1024"))
REASONING_EFFORTS     = ("low", "medium", "xhigh")
REASONING_EFFORT      = os.getenv("REASONING_EFFORT", "low").strip().lower()
if REASONING_EFFORT not in REASONING_EFFORTS:
    REASONING_EFFORT = "low"

# Embeddings stay on Ollama+nomic: identical vectors, so the existing Qdrant
# collections stay valid (switching would force a full re-ingest).
EMBED_MODEL           = "nomic-embed-text"
OLLAMA_HOST           = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# Back-compat aliases (test suites and older call sites read these).
CHAT_MODEL            = REASON_MODEL
ROUTER_MODEL          = REASON_MODEL

# ── Retrieval ─────────────────────────────────────────────────────────────────
TOP_K                 = 5
DOCS_TOP_K            = 5

# How many past messages (user+assistant entries) to replay into the prompt.
# Retrieved context is NOT stored in history -- it is sent only for the turn it
# was fetched for -- so this caps prompt growth without discarding continuity.
# The Session/CLI keeps the full transcript regardless; this only bounds what is
# re-sent each turn. 0 disables the cap.
HISTORY_TURNS         = int(os.getenv("HISTORY_TURNS", "20"))

# ── Hybrid lexical+dense retrieval (audit #6) ─────────────────────────────────
# Fuse dense cosine hits with a BM25 lexical ranker via Reciprocal Rank Fusion,
# recovering exact technical-token matches embeddings blur (AES_KEY_BASE64, MAXDOP,
# error codes). This is a clear win for the DOCUMENTS corpus.
#
# It is deliberately NOT applied to the structured data-dictionary path: those
# chunks are short per-column rows, so BM25 boosts near-tie sibling tables that
# share generic tokens (e.g. "scan jobs status" pulls stg.ScanControl above the
# correct dm_dq.scan_queue). That path already grounds via name-match / PRIMARY-
# table pinning, which dense-only feeds correctly. Measured: enabling hybrid there
# flipped 1/18 SQL questions to the wrong table. Opt in with HYBRID_STRUCTURED=1.
HYBRID_SEARCH         = os.getenv("HYBRID_SEARCH", "1").strip().lower() not in ("0", "false", "no", "off")  # documents path
HYBRID_STRUCTURED     = os.getenv("HYBRID_STRUCTURED", "0").strip().lower() not in ("0", "false", "no", "off")  # data-dictionary path (default OFF)
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
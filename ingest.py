# ingest.py
# Pulls DataDictionary rows from SQL Server, converts each row to a rich text
# chunk, embeds with nomic-embed-text via Ollama, and upserts into Qdrant.
#
# Run once (or re-run to refresh after DataDictionary changes):
#   python ingest.py

import sys

import pyodbc
import pandas as pd
import ollama
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from rich.console import Console
from rich.progress import track
import config

# Windows consoles default to cp1252, which can't encode the ✓/✗ status glyphs this
# script prints -- forcing UTF-8 stops a run from dying on the success message.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()


# ── 1. Load data from SQL Server ─────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    console.print("[bold cyan]Connecting to SQL Server...[/bold cyan]")
    conn_str = (
        f"DRIVER={{{config.DB_DRIVER}}};"
        f"SERVER={config.DB_SERVER};"
        f"DATABASE={config.DB_DATABASE};"
        "Trusted_Connection=yes;"
        # MetadataRepository is an AlwaysOn availability-group DB currently served
        # from a read-only secondary, which REFUSES connections without read-only
        # intent (error 978). Ingest is read-only anyway.
        "ApplicationIntent=ReadOnly;"
    )
    conn = pyodbc.connect(conn_str)
    df = pd.read_sql(config.INGEST_QUERY, conn)
    conn.close()
    console.print(f"[green]✓ Loaded {len(df):,} rows from DataDictionary[/green]")
    return df


# ── 2. Build a text chunk from each row ──────────────────────────────────────
#
# One row = one chunk. Every technically meaningful field is explicitly
# labelled so the embedding captures full semantic meaning and the LLM
# can answer precise questions (data type, max_length, nullability, identity)
# without having to infer from partial context.

def row_to_text(row: pd.Series) -> str:
    nullable = "nullable" if row["is_nullable"] else "NOT nullable"
    identity = "yes" if row["is_identity"] else "no"
    return (
        f"Database: {row['DatabaseName']}\n"
        f"Schema: {row['SchemaName']}\n"
        f"Table: {row['SchemaName']}.{row['ObjectName']}\n"
        f"Table type: {row['ObjectTypeDesc']}\n"
        f"Table description: {row['ObjectDescription']}\n"
        f"Column name: {row['ColumnName']}\n"
        f"Column order: {row['ColumnOrder']}\n"
        f"Data type: {row['DataType']}\n"
        f"Max length: {row['max_length']}\n"
        f"Precision: {row['precision']}\n"
        f"Scale: {row['scale']}\n"
        f"Nullable: {nullable}\n"
        f"Identity column: {identity}\n"
        f"Column description: {row['ColumnDescription']}"
    )


def build_chunks(df: pd.DataFrame) -> list[dict]:
    chunks = []
    for _, row in df.iterrows():
        chunks.append({
            "text": row_to_text(row),
            "payload": {
                "database_name":      row["DatabaseName"],
                "schema_name":        row["SchemaName"],
                "object_name":        row["ObjectName"],
                "object_type":        row["ObjectTypeDesc"],
                "column_name":        row["ColumnName"],
                "column_order":       int(row["ColumnOrder"]),
                "data_type":          row["DataType"],
                "max_length":         int(row["max_length"]),
                "precision":          int(row["precision"]),
                "scale":              int(row["scale"]),
                "is_nullable":        bool(row["is_nullable"]),
                "is_identity":        bool(row["is_identity"]),
                "object_description": row["ObjectDescription"],
                "column_description": row["ColumnDescription"],
                "clearance":          "general",  # schema metadata (audit #7)
            }
        })
    return chunks


# ── 3. Embed with Ollama ──────────────────────────────────────────────────────

def embed_texts(texts: list[str]) -> list[list[float]]:
    client = ollama.Client(host=config.OLLAMA_HOST)
    vectors = []
    for text in track(texts, description="Embedding chunks..."):
        resp = client.embeddings(model=config.EMBED_MODEL, prompt=text)
        vectors.append(resp["embedding"])
    return vectors


# ── 4. Upsert into Qdrant ────────────────────────────────────────────────────

def upsert_to_qdrant(chunks: list[dict], vectors: list[list[float]]) -> None:
    client = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)

    console.print("[bold cyan]Setting up Qdrant collection...[/bold cyan]")
    client.recreate_collection(
        collection_name=config.COLLECTION_NAME,
        vectors_config=VectorParams(
            size=config.VECTOR_SIZE,
            distance=Distance.COSINE,
        ),
    )

    points = [
        PointStruct(
            id=idx,
            vector=vector,
            payload=chunk["payload"] | {"text": chunk["text"]},
        )
        for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]

    client.upsert(collection_name=config.COLLECTION_NAME, points=points)
    console.print(
        f"[green]✓ Upserted {len(points):,} points into "
        f"'{config.COLLECTION_NAME}'[/green]"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    console.rule("[bold]DataDictionary → Qdrant Ingestion[/bold]")

    df     = load_data()
    chunks = build_chunks(df)

    console.print(f"[cyan]Built {len(chunks):,} chunks — starting embedding...[/cyan]")
    vectors = embed_texts([c["text"] for c in chunks])

    upsert_to_qdrant(chunks, vectors)

    console.rule("[bold green]Ingestion complete[/bold green]")


if __name__ == "__main__":
    main()
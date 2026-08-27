# ingest_docs.py
# Ingests PDF, Word, Excel, and Markdown files from the docs/ folder into Qdrant.
#
# Supports add, update (delete old + re-ingest), and delete.
# Run any time — only processes changed or new files.
#
# Usage:
#   python ingest_docs.py            # sync all changes
#   python ingest_docs.py --force    # re-ingest everything
#   python ingest_docs.py --delete path/to/file.pdf  # remove a specific file

import argparse
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from rich.console import Console
from rich.progress import track

import config
import doc_registry as registry
import llm
from chunker import chunk_file

console = Console()
_qdrant  = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)


# ── Qdrant collection setup ───────────────────────────────────────────────────

def ensure_collection() -> None:
    existing = [c.name for c in _qdrant.get_collections().collections]
    if config.DOCS_COLLECTION_NAME not in existing:
        _qdrant.create_collection(
            collection_name=config.DOCS_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=config.VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )
        console.print(
            f"[green]✓ Created collection '{config.DOCS_COLLECTION_NAME}'[/green]"
        )
    else:
        console.print(
            f"[dim]Collection '{config.DOCS_COLLECTION_NAME}' already exists[/dim]"
        )


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed(text: str) -> list[float]:
    return llm.embed(text)


# ── Ingest one file ───────────────────────────────────────────────────────────

def ingest_file(
    file_path: str,
    doc_id: str,
    reg: dict,
) -> int:
    """
    Chunks, embeds, and upserts one file into Qdrant.
    Returns the number of chunks ingested.
    """
    p = Path(file_path)
    file_name = p.name
    file_type = p.suffix.lower().lstrip(".")

    # Access control (audit #7): auto-classify by filename. Files matching a
    # restricted pattern (HR data, leave policy, ...) are tagged 'restricted' so
    # deny-by-default retrieval hides them from callers without that clearance.
    clearance = (
        "restricted"
        if any(pat in file_name.lower() for pat in config.RESTRICTED_DOC_PATTERNS)
        else "general"
    )
    console.print(f"  [cyan]Chunking {file_name}...[/cyan] [dim](clearance={clearance})[/dim]")
    chunks = chunk_file(file_path)

    if not chunks:
        console.print(f"  [yellow]⚠ No chunks extracted from {file_name}[/yellow]")
        return 0

    console.print(f"  [dim]{len(chunks)} chunks — embedding...[/dim]")

    points = []
    for chunk in track(chunks, description=f"  Embedding {file_name}"):
        vector = embed(chunk["text"])
        point_id = str(uuid.uuid4())
        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "doc_id":       doc_id,
                    "file_name":    file_name,
                    "file_type":    file_type,
                    "file_path":    file_path,
                    "chunk_index":  chunk["chunk_index"],
                    "chunk_total":  chunk["chunk_total"],
                    "page":         chunk.get("page"),
                    "heading":      chunk.get("heading"),
                    "sheet":        chunk.get("sheet"),
                    "row_start":    chunk.get("row_start"),
                    "row_end":      chunk.get("row_end"),
                    "clearance":    clearance,
                    "text":         chunk["text"],
                },
            )
        )

    _qdrant.upsert(collection_name=config.DOCS_COLLECTION_NAME, points=points)
    return len(points)


# ── Main sync logic ───────────────────────────────────────────────────────────

def sync(force: bool = False) -> None:
    console.rule("[bold]Document Sync[/bold]")
    ensure_collection()

    reg = registry.load_registry()
    changes = registry.detect_changes(reg)

    if force:
        # Mark everything as modified so it all re-ingests
        changes["modified"] += changes["unchanged"]
        changes["unchanged"] = []
        console.print("[yellow]--force: re-ingesting all files[/yellow]")

    total_new      = len(changes["new"])
    total_modified = len(changes["modified"])
    total_deleted  = len(changes["deleted"])
    total_unchanged= len(changes["unchanged"])

    console.print(
        f"New: [green]{total_new}[/green]  "
        f"Modified: [yellow]{total_modified}[/yellow]  "
        f"Deleted: [red]{total_deleted}[/red]  "
        f"Unchanged: [dim]{total_unchanged}[/dim]"
    )

    # ── Delete ────────────────────────────────────────────────────────────────
    for fp in changes["deleted"]:
        doc_id = reg[fp]["doc_id"]
        n = registry.delete_doc_from_qdrant(doc_id)
        registry.deregister_file(fp, reg)
        console.print(f"[red]✗ Deleted[/red] {Path(fp).name} ({n} chunks removed)")

    # ── Update (delete old + re-ingest) ───────────────────────────────────────
    for fp in changes["modified"]:
        doc_id = reg[fp]["doc_id"]
        n_del = registry.delete_doc_from_qdrant(doc_id)
        console.print(f"[yellow]↻ Updating[/yellow] {Path(fp).name} ({n_del} old chunks removed)")
        new_doc_id = registry.new_doc_id()
        n_new = ingest_file(fp, new_doc_id, reg)
        registry.register_file(fp, new_doc_id, n_new, reg)
        console.print(f"  [green]✓ Re-ingested {n_new} chunks[/green]")

    # ── Add new ───────────────────────────────────────────────────────────────
    for fp in changes["new"]:
        console.print(f"[green]+ Adding[/green] {Path(fp).name}")
        doc_id = registry.new_doc_id()
        n = ingest_file(fp, doc_id, reg)
        registry.register_file(fp, doc_id, n, reg)
        console.print(f"  [green]✓ Ingested {n} chunks[/green]")

    registry.save_registry(reg)
    console.rule("[bold green]Sync complete[/bold green]")


def delete_file(file_path: str) -> None:
    """Explicitly delete a file from Qdrant and the registry."""
    reg = registry.load_registry()
    fp  = str(Path(file_path).resolve())

    if fp not in reg:
        console.print(f"[yellow]'{fp}' not found in registry[/yellow]")
        return

    doc_id = reg[fp]["doc_id"]
    n = registry.delete_doc_from_qdrant(doc_id)
    registry.deregister_file(fp, reg)
    registry.save_registry(reg)
    console.print(f"[red]✗ Deleted[/red] {Path(fp).name} ({n} chunks removed from Qdrant)")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest documents into Qdrant")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-ingest all files regardless of modification time"
    )
    parser.add_argument(
        "--delete", metavar="FILE",
        help="Delete a specific file from Qdrant and the registry"
    )
    args = parser.parse_args()

    if args.delete:
        delete_file(args.delete)
    else:
        sync(force=args.force)
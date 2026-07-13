# doc_registry.py
# Tracks which files have been ingested and manages add/update/delete
# against the Qdrant documents collection.
#
# Registry is a JSON file on disk: docs/.registry.json
# Format:
#   {
#     "C:/rag/docs/policy.pdf": {
#       "doc_id":       "uuid4",
#       "file_name":    "policy.pdf",
#       "file_type":    "pdf",
#       "ingested_at":  "2026-06-25T...",
#       "mtime":        1719300000.0,
#       "chunk_count":  14
#     }
#   }

import json
import uuid
import os
from pathlib import Path
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

import config

REGISTRY_PATH = Path(config.DOCS_FOLDER) / ".registry.json"

_qdrant = QdrantClient(
    host=config.QDRANT_HOST, port=config.QDRANT_PORT,
    grpc_port=config.QDRANT_GRPC_PORT, prefer_grpc=True,
)


# ── Registry I/O ─────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {}


def save_registry(registry: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ── File state detection ──────────────────────────────────────────────────────

def file_state(file_path: str, registry: dict) -> str:
    """
    Returns:
      'new'      — file not in registry
      'modified' — file in registry but mtime changed
      'unchanged'— file in registry, mtime same
      'deleted'  — file in registry but no longer on disk
    """
    if file_path not in registry:
        return "new"
    if not Path(file_path).exists():
        return "deleted"
    current_mtime = Path(file_path).stat().st_mtime
    if abs(current_mtime - registry[file_path]["mtime"]) > 1.0:
        return "modified"
    return "unchanged"


def scan_docs_folder() -> list[str]:
    """Returns all supported files in DOCS_FOLDER (recursive)."""
    folder = Path(config.DOCS_FOLDER)
    if not folder.exists():
        folder.mkdir(parents=True)
    extensions = {".pdf", ".docx", ".xlsx", ".md"}
    return [
        str(p.resolve())
        for p in folder.rglob("*")
        if p.suffix.lower() in extensions and not p.name.startswith(".")
    ]


# ── Qdrant operations ─────────────────────────────────────────────────────────

def delete_doc_from_qdrant(doc_id: str) -> int:
    """
    Deletes all Qdrant points for a given doc_id.
    Returns number of points deleted.
    """
    # Scroll to find all point IDs for this doc
    points, _ = _qdrant.scroll(
        collection_name=config.DOCS_COLLECTION_NAME,
        scroll_filter=Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
        ),
        limit=1000,
        with_payload=False,
        with_vectors=False,
    )
    if not points:
        return 0

    point_ids = [p.id for p in points]
    _qdrant.delete(
        collection_name=config.DOCS_COLLECTION_NAME,
        points_selector=point_ids,
    )
    return len(point_ids)


# ── Registry entries ──────────────────────────────────────────────────────────

def register_file(
    file_path: str,
    doc_id: str,
    chunk_count: int,
    registry: dict,
) -> None:
    """Adds or updates a file entry in the registry."""
    p = Path(file_path)
    registry[file_path] = {
        "doc_id":       doc_id,
        "file_name":    p.name,
        "file_type":    p.suffix.lower().lstrip("."),
        "ingested_at":  datetime.now(timezone.utc).isoformat(),
        "mtime":        p.stat().st_mtime,
        "chunk_count":  chunk_count,
    }


def deregister_file(file_path: str, registry: dict) -> None:
    """Removes a file entry from the registry."""
    registry.pop(file_path, None)


def new_doc_id() -> str:
    return str(uuid.uuid4())


# ── Sync: detect and report changes ──────────────────────────────────────────

def detect_changes(registry: dict) -> dict:
    """
    Scans the docs folder and compares against the registry.
    Returns:
      {
        "new":      [file_path, ...],
        "modified": [file_path, ...],
        "deleted":  [file_path, ...],
        "unchanged":[file_path, ...],
      }
    """
    on_disk = set(scan_docs_folder())
    in_registry = set(registry.keys())

    changes = {"new": [], "modified": [], "deleted": [], "unchanged": []}

    for fp in on_disk:
        state = file_state(fp, registry)
        changes[state].append(fp)

    # Files in registry but not on disk
    for fp in in_registry - on_disk:
        changes["deleted"].append(fp)

    return changes
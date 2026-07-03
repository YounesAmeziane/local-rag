# chunker.py
# Extracts and chunks text from PDF, Word (.docx), and Excel (.xlsx) files.
#
# Returns a list of chunk dicts, each with:
#   text, chunk_index, chunk_total, and file-type-specific metadata
#   (page, heading, sheet, row_start, row_end)

import re
from pathlib import Path
import tiktoken

import config

# Use cl100k_base — close enough to llama tokenization for chunk sizing
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def split_with_overlap(text: str, max_tokens: int, overlap: int) -> list[str]:
    """
    Splits text into chunks of at most max_tokens tokens,
    with overlap tokens of context carried forward from the previous chunk.
    """
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        # Accumulate words until we hit the token limit
        end = start
        current_tokens = 0
        while end < len(words):
            token_count = count_tokens(" ".join(words[start:end + 1]))
            if token_count > max_tokens:
                break
            current_tokens = token_count
            end += 1

        if end == start:
            end = start + 1  # always advance at least one word

        chunk_text = " ".join(words[start:end]).strip()
        if chunk_text:
            chunks.append(chunk_text)

        # Move start back by overlap tokens worth of words
        overlap_words = 0
        overlap_tokens = 0
        for i in range(end - 1, start - 1, -1):
            overlap_tokens += count_tokens(words[i])
            if overlap_tokens >= overlap:
                break
            overlap_words += 1

        start = end - overlap_words if overlap_words > 0 else end

    return chunks


# ── PDF ───────────────────────────────────────────────────────────────────────

def chunk_pdf(file_path: str) -> list[dict]:
    import pdfplumber

    chunks = []
    raw_chunks = []  # (text, page_num, heading)

    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                continue

            # Try to detect a heading (first non-empty line, short, no period)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            heading = ""
            if lines:
                first = lines[0]
                if len(first) < 120 and not first.endswith("."):
                    heading = first

            raw_chunks.append((text.strip(), page_num, heading))

    # Split oversized page chunks and assign indices
    all_splits = []
    for text, page_num, heading in raw_chunks:
        if count_tokens(text) <= config.CHUNK_MAX_TOKENS:
            all_splits.append({
                "text":    text,
                "page":    page_num,
                "heading": heading,
            })
        else:
            sub_chunks = split_with_overlap(
                text, config.CHUNK_MAX_TOKENS, config.CHUNK_OVERLAP_TOKENS
            )
            for sub in sub_chunks:
                all_splits.append({
                    "text":    sub,
                    "page":    page_num,
                    "heading": heading,
                })

    total = len(all_splits)
    for i, chunk in enumerate(all_splits):
        chunks.append({
            "text":        chunk["text"],
            "chunk_index": i,
            "chunk_total": total,
            "page":        chunk["page"],
            "heading":     chunk["heading"],
            "sheet":       None,
            "row_start":   None,
            "row_end":     None,
        })

    return chunks


# ── Word (.docx) ──────────────────────────────────────────────────────────────

def chunk_docx(file_path: str) -> list[dict]:
    from docx import Document

    doc = Document(file_path)
    raw_chunks = []  # (text, heading)

    current_heading = ""
    current_text = []

    for para in doc.paragraphs:
        style = para.style.name.lower() if para.style else ""
        text = para.text.strip()
        if not text:
            continue

        if "heading" in style:
            # Flush current section
            if current_text:
                raw_chunks.append((" ".join(current_text), current_heading))
            current_heading = text
            current_text = []
        else:
            current_text.append(text)

    # Flush last section
    if current_text:
        raw_chunks.append((" ".join(current_text), current_heading))

    # Handle documents with no headings — treat as one block
    if not raw_chunks:
        full_text = " ".join(
            p.text.strip() for p in doc.paragraphs if p.text.strip()
        )
        raw_chunks = [(full_text, "")]

    # Split oversized sections
    all_splits = []
    for text, heading in raw_chunks:
        if not text.strip():
            continue
        if count_tokens(text) <= config.CHUNK_MAX_TOKENS:
            all_splits.append({"text": text, "heading": heading})
        else:
            for sub in split_with_overlap(
                text, config.CHUNK_MAX_TOKENS, config.CHUNK_OVERLAP_TOKENS
            ):
                all_splits.append({"text": sub, "heading": heading})

    total = len(all_splits)
    return [
        {
            "text":        c["text"],
            "chunk_index": i,
            "chunk_total": total,
            "page":        None,
            "heading":     c["heading"],
            "sheet":       None,
            "row_start":   None,
            "row_end":     None,
        }
        for i, c in enumerate(all_splits)
    ]


# ── Excel (.xlsx) ─────────────────────────────────────────────────────────────

def _is_prose_sheet(sheet) -> bool:
    """
    Detects whether a sheet contains mostly prose (long text cells)
    vs tabular data (short values).
    """
    total = 0
    long_text = 0
    for row in sheet.iter_rows(values_only=True):
        for cell in row:
            if cell is not None:
                total += 1
                if isinstance(cell, str) and len(cell) >= config.XLSX_PROSE_MIN_CHARS:
                    long_text += 1
    if total == 0:
        return False
    return (long_text / total) >= config.XLSX_PROSE_THRESHOLD


def _chunk_tabular_sheet(sheet, sheet_name: str) -> list[dict]:
    """Chunk a tabular sheet: header row + XLSX_ROWS_PER_CHUNK data rows."""
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []

    header = [str(c) if c is not None else "" for c in rows[0]]
    data_rows = rows[1:]
    n = config.XLSX_ROWS_PER_CHUNK
    chunks = []

    for batch_start in range(0, len(data_rows), n):
        batch = data_rows[batch_start: batch_start + n]
        lines = ["  |  ".join(header)]
        for row in batch:
            lines.append("  |  ".join(str(c) if c is not None else "" for c in row))
        text = "\n".join(lines)
        row_start = batch_start + 2        # 1-indexed, account for header
        row_end   = row_start + len(batch) - 1
        chunks.append({
            "text":      text,
            "sheet":     sheet_name,
            "row_start": row_start,
            "row_end":   row_end,
            "heading":   sheet_name,
            "page":      None,
        })

    return chunks


def _chunk_prose_sheet(sheet, sheet_name: str) -> list[dict]:
    """Chunk a prose sheet: concatenate cells, split by token limit."""
    texts = []
    for row in sheet.iter_rows(values_only=True):
        for cell in row:
            if cell is not None and str(cell).strip():
                texts.append(str(cell).strip())

    full_text = " ".join(texts)
    if not full_text.strip():
        return []

    splits = (
        [full_text]
        if count_tokens(full_text) <= config.CHUNK_MAX_TOKENS
        else split_with_overlap(
            full_text, config.CHUNK_MAX_TOKENS, config.CHUNK_OVERLAP_TOKENS
        )
    )

    return [
        {
            "text":      s,
            "sheet":     sheet_name,
            "row_start": None,
            "row_end":   None,
            "heading":   sheet_name,
            "page":      None,
        }
        for s in splits
    ]


def chunk_xlsx(file_path: str) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    all_chunks = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if _is_prose_sheet(ws):
            sheet_chunks = _chunk_prose_sheet(ws, sheet_name)
        else:
            sheet_chunks = _chunk_tabular_sheet(ws, sheet_name)
        all_chunks.extend(sheet_chunks)

    wb.close()

    total = len(all_chunks)
    return [
        {
            "text":        c["text"],
            "chunk_index": i,
            "chunk_total": total,
            "page":        c["page"],
            "heading":     c["heading"],
            "sheet":       c["sheet"],
            "row_start":   c["row_start"],
            "row_end":     c["row_end"],
        }
        for i, c in enumerate(all_chunks)
    ]


# ── Markdown (.md) ────────────────────────────────────────────────────────────

def chunk_md(file_path: str) -> list[dict]:
    """
    Chunks a Markdown file by heading (# / ## / ###).
    Each heading + its content = one logical chunk.
    Oversized sections are split with overlap.
    Falls back to full-text chunking for files with no headings.
    """
    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    raw_chunks = []       # (text, heading)
    current_heading = ""
    current_lines = []

    for line in lines:
        if line.startswith("#"):
            # Flush previous section
            if current_lines:
                raw_chunks.append((" ".join(current_lines).strip(), current_heading))
            current_heading = line.lstrip("#").strip()
            current_lines = []
        else:
            if line.strip():
                current_lines.append(line.strip())

    # Flush last section
    if current_lines:
        raw_chunks.append((" ".join(current_lines).strip(), current_heading))

    # No headings — treat as one block
    if not raw_chunks:
        raw_chunks = [(text.strip(), "")]

    # Split oversized sections
    all_splits = []
    for body, heading in raw_chunks:
        if not body.strip():
            continue
        if count_tokens(body) <= config.CHUNK_MAX_TOKENS:
            all_splits.append({"text": body, "heading": heading})
        else:
            for sub in split_with_overlap(body, config.CHUNK_MAX_TOKENS, config.CHUNK_OVERLAP_TOKENS):
                all_splits.append({"text": sub, "heading": heading})

    total = len(all_splits)
    return [
        {
            "text":        c["text"],
            "chunk_index": i,
            "chunk_total": total,
            "page":        None,
            "heading":     c["heading"],
            "sheet":       None,
            "row_start":   None,
            "row_end":     None,
        }
        for i, c in enumerate(all_splits)
    ]


# ── Dispatcher ────────────────────────────────────────────────────────────────

def chunk_file(file_path: str) -> list[dict]:
    """
    Routes to the correct chunker based on file extension.
    Returns a list of chunk dicts ready for embedding and upsert.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return chunk_pdf(file_path)
    elif ext == ".docx":
        return chunk_docx(file_path)
    elif ext == ".xlsx":
        return chunk_xlsx(file_path)
    elif ext == ".md":
        return chunk_md(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")
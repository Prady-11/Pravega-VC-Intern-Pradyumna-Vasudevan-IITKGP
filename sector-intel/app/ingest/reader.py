"""Reader — parses raw HTML/PDF into clean text chunks.

Reads FETCHED rows from the documents table, parses the file at raw_path into
prose, chunks it via tiktoken (~3000 tokens with 200 overlap), saves the chunks
as JSON to data/parsed/, and flips parse_status to PARSED. On failure: PARSE_FAILED.

Implementation notes:
  * HTML: BeautifulSoup with aggressive tag stripping (script, style, nav, footer).
  * PDF:  pdfplumber, page-by-page extraction.
  * Chunking: tiktoken-exact token counts, recursive split on paragraphs first
    then sentences if a paragraph alone exceeds the chunk budget.
  * Documents shorter than MIN_USEFUL_TOKENS are marked PARSE_FAILED — they're
    almost always cover pages or scanned images we can't read.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pdfplumber
import tiktoken
from bs4 import BeautifulSoup

from app.config import settings
from app.db import Document, ParseStatus, get_session

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_CHUNK = 3000
CHUNK_OVERLAP_TOKENS = 200
MIN_USEFUL_TOKENS = 500

TOKENIZER = tiktoken.get_encoding("cl100k_base")

HTML_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form")


def _parse_html(raw_path: Path) -> str:
    """Extract prose from an HTML file. Strips scripts, styles, nav, footers."""
    with raw_path.open("r", encoding="utf-8", errors="replace") as f:
        html = f.read()

    soup = BeautifulSoup(html, "lxml")
    for tag_name in HTML_NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    return text


def _parse_pdf(raw_path: Path) -> str:
    """Extract prose from a PDF, page by page."""
    pages_text: list[str] = []
    with pdfplumber.open(raw_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages_text.append(page_text)
    return "\n\n".join(pages_text)


def _parse_document(raw_path: Path, doc_type: str) -> str:
    """Dispatch to the right parser based on file extension."""
    suffix = raw_path.suffix.lower()
    if suffix in (".htm", ".html"):
        return _parse_html(raw_path)
    if suffix == ".pdf":
        return _parse_pdf(raw_path)
    raise ValueError(f"Unsupported file type {suffix} for {raw_path}")


def _count_tokens(text: str) -> int:
    return len(TOKENIZER.encode(text))


def _tail_tokens(text: str, n_tokens: int) -> str:
    """Return the last n_tokens worth of text (decoded back to a string)."""
    tokens = TOKENIZER.encode(text)
    if len(tokens) <= n_tokens:
        return text
    return TOKENIZER.decode(tokens[-n_tokens:])


def _split_long_paragraph(para: str, max_tokens: int) -> list[str]:
    """Fallback when one paragraph is bigger than the chunk budget. Split on '. '."""
    sentences = [s.strip() + "." for s in para.split(". ") if s.strip()]
    out: list[str] = []
    buf = ""
    buf_tokens = 0
    for s in sentences:
        s_tokens = _count_tokens(s)
        if buf_tokens + s_tokens > max_tokens and buf:
            out.append(buf.strip())
            buf = s
            buf_tokens = s_tokens
        else:
            buf = (buf + " " + s).strip() if buf else s
            buf_tokens += s_tokens
    if buf.strip():
        out.append(buf.strip())
    return out


def chunk_text(
    text: str,
    max_tokens: int = MAX_TOKENS_PER_CHUNK,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[dict]:
    """Split text into overlapping token-bounded chunks.

    Returns a list of {"text": str, "tokens": int} dicts.
    """
    if not text.strip():
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[dict] = []
    current_text = ""
    current_tokens = 0

    for para in paragraphs:
        para_tokens = _count_tokens(para)

        if para_tokens > max_tokens:
            if current_text:
                chunks.append({"text": current_text.strip(), "tokens": current_tokens})
                current_text = ""
                current_tokens = 0
            for sub in _split_long_paragraph(para, max_tokens):
                chunks.append({"text": sub, "tokens": _count_tokens(sub)})
            continue

        if current_tokens + para_tokens > max_tokens:
            chunks.append({"text": current_text.strip(), "tokens": current_tokens})
            overlap = _tail_tokens(current_text, overlap_tokens)
            current_text = overlap + " " + para
            current_tokens = _count_tokens(current_text)
        else:
            current_text = (current_text + " " + para).strip() if current_text else para
            current_tokens += para_tokens

    if current_text.strip():
        chunks.append({"text": current_text.strip(), "tokens": current_tokens})

    return chunks


def _process_one_document(doc_id: int) -> tuple[bool, str | None]:
    """Parse one document. Returns (success, error_message_or_None)."""
    with get_session() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            return False, f"Document {doc_id} not found"
        if doc.parse_status != ParseStatus.FETCHED:
            return True, None
        if not doc.raw_path:
            doc.parse_status = ParseStatus.PARSE_FAILED
            doc.error_message = "raw_path is empty"
            return False, "raw_path is empty"

        raw_path = Path(doc.raw_path)
        if not raw_path.exists():
            doc.parse_status = ParseStatus.PARSE_FAILED
            doc.error_message = f"raw file missing: {raw_path}"
            return False, "raw file missing"

        try:
            text = _parse_document(raw_path, doc.document_type)
        except Exception as exc:
            doc.parse_status = ParseStatus.PARSE_FAILED
            doc.error_message = f"parse error: {exc}"[:500]
            logger.warning("PARSE_FAILED %s: %s", raw_path, exc)
            return False, str(exc)

        chunks = chunk_text(text)
        total_tokens = sum(c["tokens"] for c in chunks)

        if total_tokens < MIN_USEFUL_TOKENS:
            doc.parse_status = ParseStatus.PARSE_FAILED
            doc.error_message = f"only {total_tokens} tokens extracted; document is unusable"
            logger.warning(
                "PARSE_FAILED %s: only %d tokens extracted", raw_path, total_tokens
            )
            return False, "document too short"

        parsed_filename = raw_path.stem + ".json"
        parsed_path = settings.parsed_docs_dir / parsed_filename
        payload = {
            "source_doc_id": doc_id,
            "company_id": doc.company_id,
            "period": doc.period,
            "total_tokens": total_tokens,
            "chunk_count": len(chunks),
            "chunks": chunks,
        }
        parsed_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        doc.parsed_path = str(parsed_path)
        doc.parse_status = ParseStatus.PARSED

    logger.info(
        "PARSED doc %d: %d tokens across %d chunks", doc_id, total_tokens, len(chunks)
    )
    return True, None


def run_reader() -> dict[str, int]:
    """Parse every FETCHED document. Returns a summary {key: count}."""
    settings.ensure_dirs()

    with get_session() as s:
        fetched_ids = [
            row[0]
            for row in s.query(Document.id)
            .filter(Document.parse_status == ParseStatus.FETCHED)
            .all()
        ]

    if not fetched_ids:
        logger.info("Reader: no FETCHED documents to parse.")
        return {"fetched": 0, "parsed": 0, "failed": 0}

    logger.info("Reader: %d FETCHED documents to parse", len(fetched_ids))

    parsed = 0
    failed = 0
    for doc_id in fetched_ids:
        success, _err = _process_one_document(doc_id)
        if success:
            parsed += 1
        else:
            failed += 1

    summary = {"fetched": len(fetched_ids), "parsed": parsed, "failed": failed}
    logger.info("Reader complete: %s", summary)
    return summary
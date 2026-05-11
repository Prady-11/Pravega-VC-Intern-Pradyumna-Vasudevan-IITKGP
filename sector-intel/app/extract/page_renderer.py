"""Page-level PDF parsing primitives for Indian Fintech extraction.

All functions take a PDF path. PyMuPDF (fitz) handles both text scanning and
image rendering in a single dependency.
"""
from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


_KPI_WEIGHTS: dict[str, int] = {
    "aum": 3, "loan book": 3, "receivables": 2, "advances": 1,
    "gnpa": 4, "gross npa": 4, "nnpa": 4, "net npa": 4,
    "nim": 3, "net interest margin": 3,
    "credit cost": 4, "cost of funds": 3, "cof": 2,
    "customer franchise": 4, "customer count": 3, "customers": 1,
    "active users": 3, "mau": 2, "active transacting": 3,
    "digital transactions": 4, "transaction volume": 3,
    "insurance premium": 4, "total insurance premium": 5, "gwp": 3,
    "saaum": 5, "serviced aum": 4, "mycams": 3,
    "demat accounts": 5, "avc": 4, "active value counts": 5,
}

_PER_COMPANY_BOOSTS: dict[str, set[str]] = {
    "ZAGGLE.NS":     {"customer count", "user count", "customers", "active users"},
    "CDSL.NS":       {"demat accounts", "avc", "active value counts"},
    "CAMS.NS":       {"saaum", "serviced aum", "mycams", "transaction volume"},
    "POLICYBZR.NS":  {"insurance premium", "total insurance premium", "gwp"},
    "BAJFINANCE.NS": {"customer franchise", "digital transactions"},
}


def _open(pdf_path: str | Path) -> fitz.Document | None:
    try:
        doc = fitz.open(pdf_path)
        if doc.is_encrypted and not doc.authenticate(""):
            logger.warning("Encrypted PDF, skipping: %s", pdf_path)
            doc.close()
            return None
        return doc
    except Exception as exc:
        logger.warning("Could not open %s: %s", pdf_path, exc)
        return None


def _page_text_lower(page: fitz.Page) -> str:
    try:
        raw = page.get_text("text") or ""
    except Exception:
        return ""
    return " ".join(raw.lower().split())


def _matches_any_anchor(text_lower: str, anchors: list[str]) -> bool:
    return any(a.lower() in text_lower for a in anchors)


def find_pages_by_anchors(
    pdf_path: str | Path,
    anchors: list[str],
    max_pages: int = 2,
) -> list[int]:
    """Return up to `max_pages` 0-indexed pages whose text contains any anchor."""
    if not anchors:
        return []
    doc = _open(pdf_path)
    if doc is None:
        return []
    hits: list[int] = []
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            if _matches_any_anchor(_page_text_lower(page), anchors):
                hits.append(i)
                if len(hits) >= max_pages:
                    break
    finally:
        doc.close()
    return hits


def find_pages_by_kpi_density(
    pdf_path: str | Path,
    max_pages: int = 2,
    ticker: str | None = None,
) -> list[int]:
    doc = _open(pdf_path)
    if doc is None:
        return []
    boost = _PER_COMPANY_BOOSTS.get(ticker or "", set())
    scores: list[tuple[int, int]] = []
    try:
        for i in range(doc.page_count):
            text = _page_text_lower(doc.load_page(i))
            if not text:
                continue
            score = 0
            for term, weight in _KPI_WEIGHTS.items():
                if term in text:
                    score += int(weight * (1.5 if term in boost else 1.0))
            if score > 0:
                scores.append((score, i))
    finally:
        doc.close()
    scores.sort(key=lambda x: (-x[0], x[1]))
    return [idx for _, idx in scores[:max_pages]]


def extract_text_block(
    pdf_path: str | Path,
    anchors: list[str],
    max_chars: int = 4000,
) -> str:
    """Pull full page text from pages matching any anchor. For Bajaj's KFI prose."""
    if not anchors:
        return ""
    doc = _open(pdf_path)
    if doc is None:
        return ""
    chunks: list[str] = []
    used = 0
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            text_low = _page_text_lower(page)
            if not _matches_any_anchor(text_low, anchors):
                continue
            try:
                raw = page.get_text("text") or ""
            except Exception:
                continue
            chunk = raw.strip()
            if not chunk:
                continue
            chunks.append(f"--- page {i+1} ---\n{chunk}")
            used += len(chunk)
            if used >= max_chars:
                break
    finally:
        doc.close()
    return ("\n\n".join(chunks))[:max_chars]


def render_page_png(
    pdf_path: str | Path,
    page_index: int,
    dpi: int = 150,
) -> bytes | None:
    doc = _open(pdf_path)
    if doc is None:
        return None
    try:
        if page_index < 0 or page_index >= doc.page_count:
            return None
        page = doc.load_page(page_index)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    except Exception as exc:
        logger.warning("Render failed %s page %d: %s", pdf_path, page_index, exc)
        return None
    finally:
        doc.close()


def render_kpi_pages(
    pdf_path: str | Path,
    anchors: list[str],
    max_pages: int = 2,
    dpi: int = 150,
    ticker: str | None = None,
) -> list[tuple[int, bytes]]:
    """Anchor-first, density-fallback page selection + PNG rendering."""
    pages = find_pages_by_anchors(pdf_path, anchors, max_pages=max_pages)
    if not pages:
        pages = find_pages_by_kpi_density(pdf_path, max_pages=max_pages, ticker=ticker)
        if pages:
            logger.info("Anchor miss → density fallback chose pages %s for %s",
                        pages, Path(pdf_path).name)
    out: list[tuple[int, bytes]] = []
    for idx in pages:
        img = render_page_png(pdf_path, idx, dpi=dpi)
        if img:
            out.append((idx, img))
    return out
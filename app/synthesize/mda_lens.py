"""MD&A Investing Lens engine.

Reads annual report documents already processed by Reader (parse_status=PARSED),
extracts the MD&A section from the pre-cleaned chunks, and generates the
four-question investing lens via LangChain + Claude.

Pipeline position:
    hunter → postman → reader → [THIS FILE] → synthesis table

Reader already did the hard work (HTML stripping, PDF extraction, chunking).
This file just:
  1. Joins the chunks back into full text
  2. Slices out the MD&A section
  3. Sends MD&A + structured metrics to the LLM
  4. Writes the result into Synthesis.investing_lens_text
  5. Marks each document EXTRACTED

Run:
    python -m app.synthesize.mda_lens                     # all sectors, FY25
    python -m app.synthesize.mda_lens --sector indian_fintech --fy 25
    python -m app.synthesize.mda_lens --sector us_biotech --fy 24
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.config import settings
from app.db import (
    Company, Document, DocumentType,
    Metric, ParseStatus, Sector, Synthesis, get_session,
)
from app.synthesize.synthesizer import (
    METRIC_LABEL, _fmt_value, _possible_period_formats,
)

logger = logging.getLogger(__name__)

MAX_MDA_CHARS_PER_COMPANY = 50_000   # ~12k tokens; keeps total prompt manageable

# ── MD&A boundary patterns ────────────────────────────────────────────────────

_MDA_START = [
    r"management.s\s+discussion\s+and\s+analysis",
    r"\bMD&A\b",
    r"management.s\s+discussion",
    r"review\s+of\s+operations",
    r"business\s+overview\s+and\s+financial\s+review",
    r"Item\s+7[\.\s]+Management.s\s+Discussion",
]

_MDA_END = [
    r"Item\s+7A[\.\s]+Quantitative",
    r"quantitative\s+and\s+qualitative\s+disclosures\s+about\s+market\s+risk",
    r"standalone\s+financial\s+statements",
    r"consolidated\s+financial\s+statements",
    r"independent\s+auditor",
    r"corporate\s+governance\s+report",
    r"board\s+of\s+directors\s+report",
    r"Item\s+8[\.\s]+Financial\s+Statements",
]


# ════════════════════════════════════════════════════════════════════════════
# SECTION 1: READ PARSED CHUNKS + SLICE MD&A
# ════════════════════════════════════════════════════════════════════════════

def _load_chunks(parsed_path: str) -> list[dict]:
    """Load the JSON produced by reader.py. Returns list of chunk dicts."""
    path = Path(parsed_path)
    if not path.exists():
        raise FileNotFoundError(f"Parsed file missing: {parsed_path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("chunks", [])


def _join_chunks(chunks: list[dict]) -> str:
    """Reconstruct full document text from reader.py chunks."""
    return "\n\n".join(c["text"] for c in chunks if c.get("text"))


def _slice_mda(text: str) -> Optional[str]:
    """Locate and return the MD&A section, or None if not found."""
    start_idx = None
    for pat in _MDA_START:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            start_idx = m.start()
            break

    if start_idx is None:
        return None

    search_from = start_idx + 200
    end_idx     = len(text)
    for pat in _MDA_END:
        m = re.search(pat, text[search_from:], re.IGNORECASE)
        if m:
            candidate = search_from + m.start()
            if candidate < end_idx:
                end_idx = candidate

    mda = text[start_idx:end_idx].strip()
    return mda if len(mda) > 500 else None


def extract_mda_from_parsed(doc: Document) -> Optional[str]:
    """
    Load reader.py's parsed JSON, join chunks, slice MD&A.
    Marks the document EXTRACTED on success, PARSE_FAILED if MD&A not found.
    Returns the MD&A text or None.
    """
    if not doc.parsed_path:
        logger.warning("Doc %d has no parsed_path — run reader.py first.", doc.id)
        return None

    try:
        chunks = _load_chunks(doc.parsed_path)
    except Exception as exc:
        logger.error("Doc %d: could not load parsed JSON: %s", doc.id, exc)
        return None

    if not chunks:
        logger.warning("Doc %d: parsed JSON has no chunks.", doc.id)
        return None

    full_text = _join_chunks(chunks)
    mda       = _slice_mda(full_text)

    if not mda:
        logger.warning("Doc %d: MD&A section not found in %s", doc.id, doc.parsed_path)
        _set_status(doc.id, ParseStatus.PARSE_FAILED, "MD&A section not detected")
        return None

    logger.info("Doc %d: MD&A extracted (%d chars)", doc.id, len(mda))
    _set_status(doc.id, ParseStatus.EXTRACTED)
    return mda


def _set_status(doc_id: int, status: ParseStatus, error: str = None) -> None:
    with get_session() as s:
        doc = s.get(Document, doc_id)
        if doc:
            doc.parse_status = status
            if error:
                doc.error_message = error


# ════════════════════════════════════════════════════════════════════════════
# SECTION 2: STRUCTURED METRICS (mirrors synthesizer.py pivot)
# ════════════════════════════════════════════════════════════════════════════

def _format_metrics_block(sector: Sector, fy: int) -> str:
    quarters    = _possible_period_formats(fy)
    pivot: dict = defaultdict(lambda: defaultdict(dict))

    with get_session() as s:
        companies    = s.query(Company).filter_by(sector=sector).all()
        company_ids  = [c.id for c in companies]
        id_to_ticker = {c.id: c.ticker for c in companies}

        rows = (
            s.query(
                Metric.company_id, Metric.period,
                Metric.metric_name, Metric.metric_value,
            )
            .filter(
                Metric.company_id.in_(company_ids),
                Metric.period.in_(quarters),
                Metric.metric_value.isnot(None),
            )
            .all()
        )

    for cid, period, mname, mval in rows:
        try:
            formatted = _fmt_value(mname, float(mval))
        except (TypeError, ValueError):
            continue
        ticker = id_to_ticker.get(cid)
        if ticker:
            pivot[mname][period][ticker] = formatted

    if not pivot:
        return "(no metrics found for this sector / period)"

    blocks: list[str] = []
    for mname in sorted(pivot.keys()):
        label = METRIC_LABEL.get(mname, mname)
        blocks.append(f"\n## {label}  [{mname}]")
        for q in quarters:
            company_vals = pivot[mname].get(q, {})
            if not company_vals:
                continue
            row = ", ".join(f"{t}={v}" for t, v in sorted(company_vals.items()))
            blocks.append(f"  {q}: {row}")

    return "\n".join(blocks)


# ════════════════════════════════════════════════════════════════════════════
# SECTION 3: LANGCHAIN INVESTING LENS
# ════════════════════════════════════════════════════════════════════════════

_LENS_SYSTEM = """You are a senior analyst at a venture fund studying public
incumbent behaviour to identify early-stage startup opportunities.

Rules:
  1. Answer each question with specific evidence — company names, exact
     language from the MD&A, or specific metric movements.
  2. Never give generic statements. "Companies are investing in AI" is not
     acceptable. "CreditAccess mentioned in its FY25 MD&A that it is
     deploying ML-based early-warning systems for NPA prediction across
     40% of its book" is.
  3. For each question, 2-4 short paragraphs. No bullet lists.
  4. If the data is insufficient to answer a question, say so briefly and
     move on — do not pad with filler.
"""

_LENS_USER = """Sector: {sector_label}
Period: FY{fy:02d}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MD&A EXCERPTS  (from each company's annual report)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{mda_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURED METRICS  (last 4 quarters)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{metrics_block}

Answer each question, citing MD&A language or metric movements as evidence.

QUESTION 1 — Where are incumbents investing that validates a startup market?
QUESTION 2 — Where are they struggling that creates a white space?
QUESTION 3 — What are they buying or partnering rather than building?
QUESTION 4 — What metric benchmarks should inform early-stage evaluation here?
"""


def _build_chain():
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment / .env")
    llm = ChatAnthropic(
        model      = settings.synthesis_model or settings.extraction_model,
        api_key    = settings.anthropic_api_key,
        max_tokens = 2048,
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", _LENS_SYSTEM),
        ("user",   _LENS_USER),
    ])
    return prompt | llm | StrOutputParser()


def _build_mda_block(segments: list[dict]) -> str:
    """segments: [{"company": str, "mda": str}, ...]"""
    parts: list[str] = []
    for seg in segments:
        mda = seg["mda"]
        if len(mda) > MAX_MDA_CHARS_PER_COMPANY:
            mda = mda[:MAX_MDA_CHARS_PER_COMPANY] + "\n\n[... truncated ...]"
        parts.append(f"### {seg['company']}\n{mda}")
    return ("\n\n" + "─" * 60 + "\n\n").join(parts)


# ════════════════════════════════════════════════════════════════════════════
# SECTION 4: PERSIST
# ════════════════════════════════════════════════════════════════════════════

def _persist_lens(sector: Sector, period: str, lens_text: str) -> int:
    """
    Update investing_lens_text on the Synthesis row created by synthesizer.py.
    Creates a stub row if synthesizer.py hasn't run yet for this period.
    """
    with get_session() as s:
        row = (
            s.query(Synthesis)
            .filter_by(sector=sector, period=period)
            .order_by(Synthesis.generated_at.desc())
            .first()
        )
        if row:
            row.investing_lens_text = lens_text
            s.flush()
            logger.info("Updated investing_lens_text on synthesis id=%d (%d chars)",
                        row.id, len(lens_text))
            return row.id

        logger.warning(
            "No synthesis row for sector=%s period=%s — creating stub.",
            sector.value, period,
        )
        stub = Synthesis(
            sector              = sector,
            period              = period,
            synthesis_text      = "",
            investing_lens_text = lens_text,
        )
        s.add(stub)
        s.flush()
        return stub.id


# ════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════

_SECTOR_FY = {
    Sector.INDIAN_FINTECH: 25,
    Sector.INDIAN_DEFENCE: 25,
    Sector.US_BIOTECH:     25,
}


def process_sector(sector: Sector, fy: int, chain) -> dict:
    period = f"FY{fy:02d}"
    logger.info("=== Investing lens: sector=%s %s ===", sector.value, period)

    # ── Fetch all PARSED annual report documents for this sector ───────
    with get_session() as s:
        companies    = s.query(Company).filter_by(sector=sector).all()
        comp_ids     = {c.id for c in companies}
        id_to_name   = {c.id: c.name for c in companies}

        docs = (
            s.query(Document)
            .filter(
                Document.company_id.in_(comp_ids),
                Document.document_type.in_([
                    DocumentType.ANNUAL_REPORT,
                    DocumentType.SEC_10K,
                ]),
                Document.parse_status == ParseStatus.PARSED,
            )
            .all()
        )

    if not docs:
        msg = (
            f"No PARSED annual report documents for sector={sector.value}. "
            "Run hunter → postman → reader first."
        )
        logger.warning(msg)
        return {"sector": sector.value, "status": "no_documents", "message": msg}

    # ── Extract MD&A from each document's parsed JSON ─────────────────
    segments: list[dict] = []
    for doc in docs:
        name = id_to_name.get(doc.company_id, f"company_{doc.company_id}")
        logger.info("  [%s] loading parsed chunks …", name)
        mda = extract_mda_from_parsed(doc)
        if mda:
            segments.append({"company": name, "mda": mda})

    if not segments:
        msg = f"MD&A extraction failed for all documents in sector={sector.value}."
        logger.error(msg)
        return {"sector": sector.value, "status": "extraction_failed", "message": msg}

    logger.info("%d/%d companies have usable MD&A", len(segments), len(docs))

    # ── Build prompt and call LLM ──────────────────────────────────────
    mda_block     = _build_mda_block(segments)
    metrics_block = _format_metrics_block(sector, fy)

    logger.info("Sending to LLM (~%d chars) …", len(mda_block) + len(metrics_block))
    try:
        lens_text = chain.invoke({
            "sector_label":  sector.value.replace("_", " ").title(),
            "fy":            fy,
            "mda_block":     mda_block,
            "metrics_block": metrics_block,
        })
    except Exception as exc:
        logger.exception("LLM call failed: %s", exc)
        return {"sector": sector.value, "status": "llm_failed", "error": str(exc)}

    # ── Persist ────────────────────────────────────────────────────────
    row_id = _persist_lens(sector, period, lens_text)
    return {
        "status":       "ok",
        "sector":       sector.value,
        "period":       period,
        "synthesis_id": row_id,
        "companies":    len(segments),
        "lens_chars":   len(lens_text),
    }


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def _sector_from_str(s: str) -> Sector:
    try:
        return Sector(s)
    except ValueError:
        raise SystemExit(
            f"Unknown sector '{s}'. Valid: {', '.join(sv.value for sv in Sector)}"
        )


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", default="all")
    parser.add_argument("--fy", type=int, default=25)
    args = parser.parse_args()

    chain = _build_chain()

    if args.sector.lower() == "all":
        print(f"\n{'=' * 60}\nMD&A INVESTING LENS — ALL SECTORS | FY{args.fy:02d}\n{'=' * 60}")
        results = []
        for sec in Sector:
            fy = _SECTOR_FY.get(sec, args.fy)
            print(f"\n--- {sec.value} ---")
            r = process_sector(sec, fy, chain)
            results.append(r)
            print(f"  {r['status']}", end="")
            if r.get("companies"):
                print(f"  |  {r['companies']} companies  |  {r.get('lens_chars', 0):,} chars", end="")
            print()
        print(f"\n{'=' * 60}\nSUMMARY\n{'=' * 60}")
        for r in results:
            print(f"  {r.get('sector', ''):<30} {r.get('status')}")
    else:
        r = process_sector(_sector_from_str(args.sector), args.fy, chain)
        for k, v in r.items():
            print(f"  {k:<18} {v}")
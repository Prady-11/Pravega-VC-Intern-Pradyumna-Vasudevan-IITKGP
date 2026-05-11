"""Per-company time-series extraction for Indian Fintech.

Strategy-driven: each ticker has its own recipe in `strategies.py` declaring
which modalities (text / images / both) and which schema fields to populate.

Pipeline per company:
  1. Walk data/raw/ for files matching <TICKER>_*.pdf
  2. Parse period from canonical filename: TICKER_Qn_YEAR_doctype_hash8.ext
  3. Apply company's strategy:
       - text mode:   extract_text_block from each PDF, group by quarter
       - image mode:  render KPI pages, batch as multi-modal images
       - hybrid:      both
  4. ONE multi-modal LLM call per company.
  5. yfinance overrides revenue_inr_cr.
  6. Persist Metric rows (one row per (company, period, metric_name)).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel

from app.config import settings
from app.db import (
    Company, Document, DocumentType, Metric, ParseStatus, Sector, get_session,
)
from app.extract.page_renderer import (
    extract_text_block, render_kpi_pages,
)
from app.extract.schemas import IndianFintechTimeSeriesMetrics
from app.extract.strategies import get_strategy
from app.ingest.yfinance_client import fetch_yfinance_quarterly

logger = logging.getLogger(__name__)


FINTECH_COMPANIES: list[tuple[str, str, str]] = [
    ("Bajaj Finance",                       "BAJFINANCE.NS", "NSE"),
    ("SBI Cards & Payment Services",        "SBICARD.NS",    "NSE"),
    ("PB Fintech (Policybazaar)",           "POLICYBZR.NS",  "NSE"),
    ("Computer Age Management Services",    "CAMS.NS",       "NSE"),
    ("Central Depository Services",         "CDSL.NS",       "NSE"),
    ("Zaggle Prepaid",                      "ZAGGLE.NS",     "NSE"),
    ("CreditAccess Grameen",                "CREDITACC.NS",  "NSE"),
    ("Five Star Business Finance",          "FIVESTAR.NS",   "NSE"),
]

DATA_RAW = Path("data/raw")
MAX_IMAGES_PER_CALL = 16
DPI = 150


SYSTEM_PROMPT = """You extract structured fintech KPIs from Indian investor presentations and earnings text.

You receive:
  (a) optional text blocks from the deck (one per quarter)
  (b) optional slide images (each labeled with its quarter)

Read values DIRECTLY off the source and write them into the appropriate structured fields.

CRITICAL RULES:

1. NUMERIC VALUES GO INTO STRUCTURED FIELDS, NOT INTO `notes`.
   - WRONG: notes = "Net NPA at 60 bps; AUM 462,261 Cr"
   - RIGHT: net_npa_pct = 0.60, aum_inr_cr = 462261

2. Source-label → schema-field map:
   - "GNPA" / "Gross NPA"          → gross_npa_pct       (e.g. 1.24% → 1.24)
   - "NNPA" / "Net NPA"             → net_npa_pct         (60 bps → 0.60)
   - "AUM" / "Loan book" / "Receivables" / "Advances" → aum_inr_cr (in crore)
   - "NIM" / "Net Interest Margin"  → nim_pct
   - "COF" / "Cost of funds"        → cost_of_funds_pct
   - "Credit cost"                  → credit_cost_pct (bps→%: 200 bps = 2.00)
   - "Customer franchise" / "Customers" → customer_count_mn (in millions)
   - "Active users" / "MAU"         → active_users_mn
   - "Demat accounts"               → demat_accounts_mn
   - "AVC" / "Active Value Counts"  → avc_count_mn
   - "Insurance premium" / "GWP"    → insurance_premium_inr_cr
   - "SaAUM" / "Serviced AUM"       → saaum_inr_cr
   - "Digital transactions"         → digital_transactions_count_mn

3. Use null for fields not shown for that quarter. Do not guess.

4. Return one QuarterlyFintechMetrics object per quarter visible. Use the
   period label EXACTLY as given (e.g. "Q3 FY25").

5. yfinance revenue values are authoritative — copy `revenue_inr_cr` exactly.

6. `notes` is for ONE-LINE context only. Numeric KPIs go in their fields.

7. trend_notes (top-level): 2-3 sentences on cross-quarter trajectory.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Filename → period:  TICKER_Q3_2024_investor_presentation_a3f8c102.pdf → "Q3 FY25"
# Year in filename is FY-start-year → FY label is start+1 (two-digit).
# ─────────────────────────────────────────────────────────────────────────────
_FNAME_RE = re.compile(
    r"^(?P<ticker>[A-Z]+\.NS)_"
    r"(?P<q>Q[1-4])_"
    r"(?P<year>\d{4})_"
    r"(?P<doc>[a-z_]+)_"
    r"(?P<hash>[0-9a-f]{8})\.(?:pdf|xls|xlsx)$",
    re.IGNORECASE,
)


def parse_canonical_filename(name: str) -> tuple[str, str, str, str] | None:
    m = _FNAME_RE.match(name)
    if not m:
        return None
    ticker = m.group("ticker").upper()
    q = m.group("q").upper()
    start_year = int(m.group("year"))
    fy_label = f"FY{(start_year + 1) % 100:02d}"
    return ticker, f"{q} {fy_label}", m.group("doc").lower(), m.group("hash")


# ─────────────────────────────────────────────────────────────────────────────
# Seed companies + register data/raw files as PARSED Documents
# ─────────────────────────────────────────────────────────────────────────────
def _sha256_file(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


_DOC_TYPE_MAP = {
    "investor_presentation": DocumentType.INVESTOR_PRESENTATION,
    "earnings_call":         DocumentType.EARNINGS_CALL,
    "press_release":         DocumentType.PRESS_RELEASE,
    "financial_statement":   DocumentType.FINANCIAL_STATEMENT,
    "annual_report":         DocumentType.ANNUAL_REPORT,
    "award_of_orders":       DocumentType.AWARD_OF_ORDERS,
}


def seed_companies_and_register_files() -> None:
    new_docs = skipped = 0
    fintech_tickers = {t for _, t, _ in FINTECH_COMPANIES}

    with get_session() as s:
        for name, ticker, exchange in FINTECH_COMPANIES:
            existing = s.query(Company).filter_by(
                ticker=ticker, exchange=exchange,
            ).one_or_none()
            if existing is None:
                s.add(Company(
                    name=name, sector=Sector.INDIAN_FINTECH,
                    ticker=ticker, exchange=exchange,
                ))
                logger.info("Seeded company %s (%s)", name, ticker)

        s.flush()
        ticker_to_id = {
            c.ticker: c.id
            for c in s.query(Company)
            .filter(Company.sector == Sector.INDIAN_FINTECH).all()
        }

        if not DATA_RAW.exists():
            logger.error("Missing %s — run migration first", DATA_RAW.resolve())
            return

        for fp in sorted(DATA_RAW.iterdir()):
            if not fp.is_file():
                continue
            parsed = parse_canonical_filename(fp.name)
            if not parsed:
                continue
            ticker, period, doc_type_str, _ = parsed
            if ticker not in fintech_tickers:
                continue
            company_id = ticker_to_id.get(ticker)
            if company_id is None:
                continue

            file_hash = _sha256_file(fp)
            existing = s.query(Document).filter_by(
                company_id=company_id, content_hash=file_hash,
            ).one_or_none()
            if existing:
                if existing.raw_path != str(fp.resolve()):
                    existing.raw_path = str(fp.resolve())
                skipped += 1
                continue

            doc_type = _DOC_TYPE_MAP.get(doc_type_str, DocumentType.OTHER)
            s.add(Document(
                company_id=company_id,
                source_url=f"file://{fp.resolve()}",
                document_type=doc_type,
                period=period,
                parse_status=ParseStatus.PARSED,
                fetched_at=datetime.utcnow(),
                content_hash=file_hash,
                raw_path=str(fp.resolve()),
            ))
            new_docs += 1

    logger.info("Ingest: %d new Documents, %d already present", new_docs, skipped)


# ─────────────────────────────────────────────────────────────────────────────
# LLM helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_llm():
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return ChatAnthropic(
        model=settings.extraction_model,
        api_key=settings.anthropic_api_key,
        temperature=0.0,
        max_tokens=8192,
    )


def _build_focused_system_prompt(target_fields: list[str]) -> str:
    return SYSTEM_PROMPT + (
        f"\n\nFor this company, populate ONLY these fields (others stay null):\n"
        f"  {', '.join(target_fields)}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Persist
# ─────────────────────────────────────────────────────────────────────────────
def _flatten_quarter(q: BaseModel, company_id: int) -> list[dict]:
    rows = []
    period = q.period
    for key, value in q.model_dump().items():
        if key == "period" or value is None:
            continue
        if isinstance(value, (int, float)):
            rows.append({
                "company_id": company_id, "period": period,
                "metric_name": key, "metric_value": float(value),
            })
        elif isinstance(value, str):
            rows.append({
                "company_id": company_id, "period": period,
                "metric_name": key, "metric_value_text": value,
            })
    return rows


def _persist_rows(rows, period_to_doc_id):
    if not rows:
        return 0
    written = 0
    with get_session() as s:
        for row in rows:
            doc_id = period_to_doc_id.get(row["period"])
            if doc_id is None:
                continue
            row["source_document_id"] = doc_id
            existing = s.query(Metric).filter_by(
                company_id=row["company_id"],
                period=row["period"],
                metric_name=row["metric_name"],
            ).one_or_none()
            if existing is None:
                s.add(Metric(**row))
            else:
                existing.metric_value = row.get("metric_value")
                existing.metric_value_text = row.get("metric_value_text")
                existing.source_document_id = row["source_document_id"]
            written += 1
    return written


def _tier5_yfinance_overrides(extracted, yf):
    out = []
    for q in extracted.quarters:
        rev = (yf.get(q.period) or {}).get("revenue_inr_cr")
        if rev is not None and hasattr(q, "revenue_inr_cr"):
            out.append(q.model_copy(update={"revenue_inr_cr": rev}))
            logger.info("yfinance override %s.revenue_inr_cr -> %s", q.period, rev)
        else:
            out.append(q)
    return extracted.model_copy(update={"quarters": out})


def _format_yf_block(yf):
    if not yf:
        return "(no yfinance data)"
    lines = []
    for period in sorted(yf.keys()):
        parts = [f"{k}={v}" for k, v in yf[period].items() if k.startswith("revenue")]
        if parts:
            lines.append(f"  {period}: {', '.join(parts)}")
    return "\n".join(lines) if lines else "(no revenue data)"


# ─────────────────────────────────────────────────────────────────────────────
# Per-company extraction
# ─────────────────────────────────────────────────────────────────────────────
def _extract_one_fintech_company(company_id: int) -> tuple[bool, str | None]:
    with get_session() as s:
        company = s.get(Company, company_id)
        if company is None or company.sector != Sector.INDIAN_FINTECH:
            return True, None
        ticker = company.ticker
        company_name = company.name
        docs = (
            s.query(Document)
            .filter(
                Document.company_id == company_id,
                Document.parse_status == ParseStatus.PARSED,
                Document.document_type == DocumentType.INVESTOR_PRESENTATION,
            )
            .order_by(Document.period.desc())
            .all()
        )
        doc_records = [(d.id, d.period, d.raw_path) for d in docs]

    if not doc_records:
        logger.info("%s: no PARSED investor presentations", ticker)
        return True, None

    strategy = get_strategy(ticker)
    yf_data = fetch_yfinance_quarterly(ticker)

    period_to_doc_id: dict[str, int] = {}
    text_blocks: list[tuple[str, str]] = []
    images_with_labels: list[tuple[str, bytes]] = []

    for doc_id, period, raw_path in doc_records:
        period_to_doc_id.setdefault(period, doc_id)
        if not raw_path:
            continue

        if strategy["mode"] in ("text", "hybrid") and strategy["text_anchors"]:
            block = extract_text_block(
                raw_path, strategy["text_anchors"], max_chars=3000,
            )
            if block:
                text_blocks.append((period, block))
                logger.info("%s %s: extracted %d chars of text", ticker, period, len(block))

        if strategy["mode"] in ("image", "hybrid") and \
           len(images_with_labels) < MAX_IMAGES_PER_CALL:
            pages = render_kpi_pages(
                raw_path,
                anchors=strategy["image_anchors"],
                max_pages=strategy["max_pages"],
                dpi=DPI,
                ticker=ticker,
            )
            for page_idx, png in pages:
                images_with_labels.append((period, png))
                logger.info("%s %s: rendered page %d", ticker, period, page_idx + 1)
                if len(images_with_labels) >= MAX_IMAGES_PER_CALL:
                    break

    fallback_doc_id = doc_records[0][0]
    for p in yf_data.keys():
        period_to_doc_id.setdefault(p, fallback_doc_id)

    yf_block = _format_yf_block(yf_data)
    logger.info(
        "%s: strategy=%s | docs=%d | text_blocks=%d | images=%d",
        ticker, strategy["mode"], len(doc_records),
        len(text_blocks), len(images_with_labels),
    )

    if not text_blocks and not images_with_labels:
        logger.warning("%s: nothing extractable — skipping", ticker)
        return True, None

    content: list[dict] = [
        {"type": "text", "text": (
            f"Company: {company_name} ({ticker})\n\n"
            f"=== YFINANCE FINANCIALS [authoritative for revenue] ===\n{yf_block}\n\n"
        )},
    ]
    if text_blocks:
        content.append({"type": "text", "text": "=== DECK TEXT EXCERPTS ===\n"})
        for period, txt in text_blocks:
            content.append({"type": "text", "text": f"\n--- {period} ---\n{txt}\n"})
    if images_with_labels:
        content.append({"type": "text", "text": (
            f"\n=== INVESTOR PRESENTATION SLIDES ({len(images_with_labels)}) ===\n"
            f"Each slide is labeled with its quarter. Read KPIs from these tables.\n"
        )})
        for label, png in images_with_labels:
            b64 = base64.b64encode(png).decode()
            content.append({"type": "text", "text": f"\n--- {label} ---"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
    content.append({"type": "text", "text": "\nReturn IndianFintechTimeSeriesMetrics now."})

    llm = _get_llm()
    structured = llm.with_structured_output(IndianFintechTimeSeriesMetrics)
    focused_prompt = _build_focused_system_prompt(strategy["target_fields"])

    try:
        result = structured.invoke([
            {"role": "system", "content": focused_prompt},
            {"role": "user",   "content": content},
        ])
    except Exception as exc:
        logger.exception("%s LLM call failed: %s", ticker, exc)
        with get_session() as s:
            for doc_id, *_ in doc_records:
                doc = s.get(Document, doc_id)
                doc.parse_status = ParseStatus.EXTRACTION_FAILED
                doc.error_message = f"LLM failed: {str(exc)[:300]}"
        return False, str(exc)

    result = _tier5_yfinance_overrides(result, yf_data)

    for q in result.quarters:
        period_to_doc_id.setdefault(q.period, fallback_doc_id)

    all_rows = []
    for q in result.quarters:
        all_rows.extend(_flatten_quarter(q, company_id))
    n_written = _persist_rows(all_rows, period_to_doc_id)

    if result.trend_notes:
        with get_session() as s:
            existing = s.query(Metric).filter_by(
                company_id=company_id, period="ALL", metric_name="trend_notes",
            ).one_or_none()
            if existing is None:
                s.add(Metric(
                    company_id=company_id, period="ALL",
                    metric_name="trend_notes",
                    metric_value_text=result.trend_notes,
                    source_document_id=fallback_doc_id,
                ))
            else:
                existing.metric_value_text = result.trend_notes

    with get_session() as s:
        for doc_id, *_ in doc_records:
            doc = s.get(Document, doc_id)
            doc.parse_status = ParseStatus.EXTRACTED
            doc.error_message = None

    logger.info("%s EXTRACTED: %d metric rows across %d quarters",
                ticker, n_written, len(result.quarters))
    return True, None


def run_extractor_indian_fintech() -> dict[str, int]:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    seed_companies_and_register_files()

    with get_session() as s:
        company_ids = [
            c.id for c in s.query(Company)
            .filter(Company.sector == Sector.INDIAN_FINTECH).all()
        ]

    logger.info("Indian Fintech extraction: %d companies", len(company_ids))
    extracted = failed = 0
    for cid in company_ids:
        ok, err = _extract_one_fintech_company(cid)
        if ok and err is None:
            extracted += 1
        else:
            failed += 1
    summary = {"companies": len(company_ids), "extracted": extracted, "failed": failed}
    logger.info("Indian Fintech complete: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )
    print("Starting Indian Fintech extraction…")
    summary = run_extractor_indian_fintech()
    print("\nDone:", summary)
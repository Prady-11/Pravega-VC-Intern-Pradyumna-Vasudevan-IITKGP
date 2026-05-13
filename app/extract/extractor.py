"""Per-company time-series extraction.

For each US Biotech company:
  1. Fetch all quarters' XBRL financials in one API call
  2. Per document: regex-extract MD&A + Business sections only
  3. Concatenate all docs' relevant sections into one payload
  4. Single LLM call returns USBiotechTimeSeriesMetrics
  5. XBRL values overwrite LLM output for financial fields (Tier 5)
  6. Flatten to metrics table rows (one row per company-period-field)

Total LLM calls for biotech backfill: 8 (one per company), not 89.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from app.config import settings
from app.db import Company, Document, Metric, ParseStatus, Sector, get_session
from app.extract.schemas import USBiotechTimeSeriesMetrics
from app.extract.section_router import extract_relevant_text
from app.extract.xbrl_client import (
    extract_cik_from_url,
    fetch_all_quarters_for_company,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You extract structured biotech metrics from SEC filings.

You receive:
1. Pre-fetched XBRL financial facts (auditor-certified — match exactly).
2. Concatenated MD&A and Business Overview sections from 12 quarterly filings.

Your job: produce one USBiotechTimeSeriesMetrics object containing one
QuarterlyBiotechMetrics entry per period found in the input.

Rules:
1. Use null for any field NOT explicitly stated. Do not infer.
2. XBRL values are authoritative — copy exactly.
3. For pipeline counts: count distinct programs at each phase as of that period.
4. For trial_readouts: only include events explicitly described.
5. For ai_ml_callouts: extract distinct mentions of AI/ML platforms or partnerships.
6. trend_notes: 2-4 sentences highlighting cross-quarter changes (cash trajectory,
   pipeline progression, new partnerships).
"""


_chain: Any = None
_IDENTITY_FIELDS    = {"period"}
_LIST_OBJECT_FIELDS = {"trial_readouts"}
_LIST_STR_FIELDS    = {"ai_ml_callouts"}


def _get_chain():
    """Lazy-init the LangChain Anthropic chain. with_structured_output enforces schema."""
    global _chain
    if _chain is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
        llm = ChatAnthropic(
            model=settings.extraction_model,
            api_key=settings.anthropic_api_key,
            temperature=0.0,
            max_tokens=8192,
        )
        structured = llm.with_structured_output(USBiotechTimeSeriesMetrics)
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human",
             "Company: {company_name}\n\n"
             "=== XBRL FINANCIAL FACTS [auditor-certified] ===\n{xbrl_block}\n\n"
             "=== FILING SECTIONS [MD&A + Business Overview, all quarters] ===\n{filings_block}\n\n"
             "Return USBiotechTimeSeriesMetrics now."),
        ])
        _chain = prompt | structured
    return _chain


def _format_xbrl_block(xbrl_data: dict[str, dict[str, float]]) -> str:
    """Format per-period XBRL dict into prompt text."""
    if not xbrl_data:
        return "(no XBRL data available)"
    lines = []
    for period in sorted(xbrl_data.keys()):
        facts = xbrl_data[period]
        parts = [f"{k}=${v:.1f}M" for k, v in facts.items()]
        lines.append(f"  {period}: {', '.join(parts)}")
    return "\n".join(lines)


def _format_filings_block(per_period_text: dict[str, str]) -> str:
    """Format per-period concatenated filing text into one prompt block."""
    if not per_period_text:
        return "(no filing sections found)"
    parts = []
    for period in sorted(per_period_text.keys()):
        text = per_period_text[period]
        parts.append(f"\n----- {period} -----\n{text}")
    return "\n".join(parts)


def _tier5_xbrl_overrides(
    extracted: USBiotechTimeSeriesMetrics,
    xbrl_data: dict[str, dict[str, float]],
) -> USBiotechTimeSeriesMetrics:
    """For each quarter: overwrite LLM financial fields with XBRL values."""
    overridden_quarters = []
    for q in extracted.quarters:
        period_xbrl = xbrl_data.get(q.period, {})
        if not period_xbrl:
            overridden_quarters.append(q)
            continue
        updates = {k: v for k, v in period_xbrl.items() if hasattr(q, k)}
        if updates:
            overridden_quarters.append(q.model_copy(update=updates))
            for k, v in updates.items():
                logger.info("Tier5 override %s.%s -> %.1f", q.period, k, v)
        else:
            overridden_quarters.append(q)
    return extracted.model_copy(update={"quarters": overridden_quarters})


def _flatten_quarter_to_rows(
    q: BaseModel,
    company_id: int,
) -> list[dict]:
    """Convert one QuarterlyBiotechMetrics into Metric rows."""
    rows: list[dict] = []
    period = q.period
    data = q.model_dump()

    for key, value in data.items():
        if key in _IDENTITY_FIELDS or value is None:
            continue
        if key in _LIST_OBJECT_FIELDS:
            for i, item in enumerate(value):
                rows.append({
                    "company_id": company_id, "period": period,
                    "metric_name": f"{key}_{i+1}",
                    "metric_value_text": json.dumps(item, sort_keys=True, default=str),
                })
        elif key in _LIST_STR_FIELDS:
            for i, item in enumerate(value):
                rows.append({
                    "company_id": company_id, "period": period,
                    "metric_name": f"{key}_{i+1}",
                    "metric_value_text": str(item),
                })
        elif isinstance(value, (int, float)):
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


def _persist_metric_rows(
    rows: list[dict],
    period_to_doc_id: dict[str, int],
) -> int:
    """Upsert metric rows. Resolves source_document_id from period mapping."""
    if not rows:
        return 0
    written = 0
    with get_session() as s:
        for row in rows:
            doc_id = period_to_doc_id.get(row["period"])
            if doc_id is None:
                # period in LLM output not in our doc list — skip (the LLM hallucinated a quarter)
                logger.warning("Skipping row for unknown period %s", row["period"])
                continue
            row["source_document_id"] = doc_id

            existing = (
                s.query(Metric)
                .filter_by(
                    company_id=row["company_id"],
                    period=row["period"],
                    metric_name=row["metric_name"],
                )
                .one_or_none()
            )
            if existing is None:
                s.add(Metric(**row))
            else:
                existing.metric_value      = row.get("metric_value")
                existing.metric_value_text = row.get("metric_value_text")
                existing.source_document_id = row["source_document_id"]
            written += 1
    return written


def _extract_one_company(company_id: int) -> tuple[bool, str | None]:
    """One LLM call covering all of a company's parsed documents."""
    # Step 1: gather company info and all parsed docs
    with get_session() as s:
        company = s.get(Company, company_id)
        if company is None:
            return False, f"company {company_id} not found"
        if company.sector != Sector.US_BIOTECH:
            return True, None  # skip non-biotech for now

        company_name = company.name
        ticker = company.ticker

        docs = (
            s.query(Document)
            .filter(
                Document.company_id == company_id,
                Document.parse_status == ParseStatus.PARSED,
            )
            .order_by(Document.id.desc())
            .limit(4)
            .all()
        )
        doc_records = [
            (d.id, d.period, d.parsed_path, d.source_url) for d in docs
        ]

    if not doc_records:
        logger.info("Company %s: no PARSED docs", ticker)
        return True, None

    # Step 2: XBRL fetch (once per company)
    cik = None
    for _, _, _, source_url in doc_records:
        cik = extract_cik_from_url(source_url)
        if cik:
            break
    xbrl_data = fetch_all_quarters_for_company(cik) if cik else {}
    # Step 3: per-document section extraction
    period_to_doc_id: dict[str, int] = {}
    per_period_text: dict[str, str] = {}
    for doc_id, period, parsed_path, _ in doc_records:
        if not parsed_path or not Path(parsed_path).exists():
            continue
        period_to_doc_id[period] = doc_id
        try:
            payload = json.loads(Path(parsed_path).read_text(encoding="utf-8"))
            chunks = payload.get("chunks", [])
            relevant = extract_relevant_text(chunks, max_chars=6000)
            if relevant:
                per_period_text[period] = relevant
        except Exception as exc:
            logger.warning("Failed to load %s: %s", parsed_path, exc)
    xbrl_data = {p: v for p, v in xbrl_data.items() if p in period_to_doc_id}
    # Step 4: build prompt blocks
    xbrl_block    = _format_xbrl_block(xbrl_data)
    filings_block = _format_filings_block(per_period_text)
    estimated_tokens = (len(xbrl_block) + len(filings_block)) // 4
    logger.info(
        "%s: %d docs | XBRL periods: %d | filing periods: %d | est ~%d input tokens",
        ticker, len(doc_records), len(xbrl_data), len(per_period_text), estimated_tokens,
    )

    # Step 5: LLM call
    chain = _get_chain()
    try:
        result: USBiotechTimeSeriesMetrics = chain.invoke({
            "company_name": company_name,
            "xbrl_block": xbrl_block,
            "filings_block": filings_block,
        })
    except Exception as exc:
        logger.exception("LLM call failed for %s: %s", ticker, exc)
        with get_session() as s:
            for doc_id, _, _, _ in doc_records:
                doc = s.get(Document, doc_id)
                doc.parse_status = ParseStatus.EXTRACTION_FAILED
                doc.error_message = f"LLM call failed: {str(exc)[:300]}"
        return False, str(exc)

    # Step 6: Tier 5 overrides
    result = _tier5_xbrl_overrides(result, xbrl_data)

    # Step 7: flatten + persist
    all_rows: list[dict] = []
    for q in result.quarters:
        all_rows.extend(_flatten_quarter_to_rows(q, company_id))
    n_written = _persist_metric_rows(all_rows, period_to_doc_id)

    # Step 8: also persist trend_notes as a special metric
    if result.trend_notes:
        with get_session() as s:
            # Use a sentinel period "ALL" for company-level commentary
            existing = (
                s.query(Metric)
                .filter_by(
                    company_id=company_id,
                    period="ALL",
                    metric_name="trend_notes",
                )
                .one_or_none()
            )
            first_doc_id = doc_records[0][0]
            if existing is None:
                s.add(Metric(
                    company_id=company_id,
                    period="ALL",
                    metric_name="trend_notes",
                    metric_value_text=result.trend_notes,
                    source_document_id=first_doc_id,
                ))
            else:
                existing.metric_value_text = result.trend_notes

    # Step 9: flip all docs' status
    with get_session() as s:
        for doc_id, _, _, _ in doc_records:
            doc = s.get(Document, doc_id)
            doc.parse_status = ParseStatus.EXTRACTED
            doc.error_message = None

    logger.info("%s EXTRACTED: %d rows across %d quarters", ticker, n_written, len(result.quarters))
    return True, None


def run_extractor(tickers: list[str] | None = None) -> dict[str, int]:
    """Per-company extraction for all US Biotech companies with PARSED docs."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    with get_session() as s:
        company_ids = [
            c.id for c in s.query(Company).filter(Company.sector == Sector.US_BIOTECH).all()
        ]

    logger.info("Per-company extraction: %d biotech companies", len(company_ids))
    extracted = failed = skipped = 0

    for cid in company_ids:
        ok, err = _extract_one_company(cid)
        if err is None and ok:
            extracted += 1
        elif err is None:
            skipped += 1
        else:
            failed += 1

    summary = {
        "companies": len(company_ids),
        "extracted": extracted,
        "skipped": skipped,
        "failed": failed,
    }
    logger.info("Extractor complete: %s", summary)
    return summary
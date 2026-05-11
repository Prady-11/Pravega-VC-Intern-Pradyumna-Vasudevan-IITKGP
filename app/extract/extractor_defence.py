"""Per-company extraction for Indian Defence.

Pipeline:
  1. yfinance: revenue/EBITDA/margins (deterministic, no LLM)
  2. Press releases + Award of Orders: group by quarter, LLM extracts NewOrderWin objects
  3. Investor Presentations: regex-match 'order book' chunks, LLM extracts quarterly values
  4. ONE LLM call combines (2) + (3) into IndianDefenceTimeSeriesMetrics
  5. yfinance values overwrite LLM financials (Tier 5)
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from app.config import settings
from app.db import (
    Company, Document, DocumentType, Metric, ParseStatus, Sector, get_session,
)
from app.extract.schemas import IndianDefenceTimeSeriesMetrics
from app.ingest.yfinance_client import fetch_yfinance_quarterly

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You extract structured defence-sector metrics from BSE filings.

You receive:
1. Pre-computed yfinance financials per quarter (auditor-reported — match exactly).
2. Press releases / order announcements grouped by quarter.
3. Investor-presentation snippets containing 'order book' text, grouped by quarter.

Your job: produce one IndianDefenceTimeSeriesMetrics object containing one
QuarterlyDefenceMetrics entry per quarter present in the input.

Rules:
1. yfinance values are authoritative — copy exactly into revenue/EBITDA fields.
2. For new_order_wins: one NewOrderWin per distinct order announced in that quarter.
   - value_crore: order value in INR crore. Convert ₹X cr or ₹X crore directly.
   - geography: 'domestic' if Indian customer (MoD, IAF, Indian Navy, Indian Army),
     'export' if foreign, 'mixed' if both.
   - product_category: short phrase (e.g. 'radar systems', 'ammunition', 'aircraft engines').
3. For order book: extract the most recent figure per quarter from the snippets.
   Domestic/export % only if explicitly stated. If only domestic given, set export = 100 - domestic.
4. Use null for any field not stated. Do not infer.
5. trend_notes: 2-4 sentences on order book trajectory and major wins.
"""


_chain: Any = None

# Regex anchors for order book snippets
_ORDERBOOK_ANCHORS = [
    re.compile(r"order\s+book", re.IGNORECASE),
    re.compile(r"order\s+backlog", re.IGNORECASE),
    re.compile(r"outstanding\s+orders?", re.IGNORECASE),
    re.compile(r"unexecuted\s+order", re.IGNORECASE),
]


def _get_chain():
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
        structured = llm.with_structured_output(IndianDefenceTimeSeriesMetrics)
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human",
             "Company: {company_name}\n\n"
             "=== YFINANCE FINANCIALS [authoritative] ===\n{yf_block}\n\n"
             "=== ORDER ANNOUNCEMENTS [grouped by quarter] ===\n{orders_block}\n\n"
             "=== INVESTOR PRESENTATION SNIPPETS [order book mentions] ===\n{orderbook_block}\n\n"
             "Return IndianDefenceTimeSeriesMetrics now."),
        ])
        _chain = prompt | structured
    return _chain


def _format_yf_block(yf: dict[str, dict[str, float]]) -> str:
    if not yf:
        return "(no yfinance data)"
    lines = []
    for period in sorted(yf.keys()):
        parts = [f"{k}={v}" for k, v in yf[period].items()]
        lines.append(f"  {period}: {', '.join(parts)}")
    return "\n".join(lines)


def _extract_orderbook_snippets(parsed_path: str, window: int = 220) -> list[str]:
    """Return ±window-char snippets around order-book anchors in this PDF's chunks."""
    if not parsed_path or not Path(parsed_path).exists():
        return []
    try:
        payload = json.loads(Path(parsed_path).read_text(encoding="utf-8"))
    except Exception:
        return []
    snippets: list[str] = []
    for chunk in payload.get("chunks", []):
        text = chunk.get("text", "")
        for anchor in _ORDERBOOK_ANCHORS:
            for m in anchor.finditer(text):
                start = max(0, m.start() - window)
                end = min(len(text), m.end() + window)
                snippets.append(text[start:end].strip())
                if len(snippets) >= 3:  # cap per chunk
                    break
    return snippets[:5]


def _press_release_text(parsed_path: str, max_chars: int = 1500) -> str:
    """Get up to max_chars of press release / order announcement body."""
    if not parsed_path or not Path(parsed_path).exists():
        return ""
    try:
        payload = json.loads(Path(parsed_path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    parts: list[str] = []
    total = 0
    for chunk in payload.get("chunks", []):
        text = chunk.get("text", "")
        if total + len(text) > max_chars:
            text = text[: max_chars - total]
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return " ".join(parts).strip()


def _flatten_quarter_to_rows(q: BaseModel, company_id: int) -> list[dict]:
    rows = []
    period = q.period
    data = q.model_dump()
    for key, value in data.items():
        if key == "period" or value is None:
            continue
        if key == "new_order_wins":
            for i, win in enumerate(value):
                rows.append({
                    "company_id": company_id, "period": period,
                    "metric_name": f"new_order_win_{i+1}",
                    "metric_value_text": json.dumps(win, sort_keys=True, default=str),
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


def _persist_metric_rows(rows: list[dict], period_to_doc_id: dict[str, int]) -> int:
    if not rows:
        return 0
    written = 0
    with get_session() as s:
        for row in rows:
            doc_id = period_to_doc_id.get(row["period"])
            if doc_id is None:
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


def _tier5_yfinance_overrides(
    extracted: IndianDefenceTimeSeriesMetrics,
    yf: dict[str, dict[str, float]],
) -> IndianDefenceTimeSeriesMetrics:
    """yfinance values overwrite LLM output for revenue/EBITDA/growth fields."""
    overridden = []
    for q in extracted.quarters:
        period_yf = yf.get(q.period, {})
        if not period_yf:
            overridden.append(q)
            continue
        updates = {k: v for k, v in period_yf.items() if hasattr(q, k)}
        if updates:
            overridden.append(q.model_copy(update=updates))
            for k, v in updates.items():
                logger.info("Tier5 override %s.%s -> %s", q.period, k, v)
        else:
            overridden.append(q)
    return extracted.model_copy(update={"quarters": overridden})


def _extract_one_defence_company(company_id: int) -> tuple[bool, str | None]:
    with get_session() as s:
        company = s.get(Company, company_id)
        if company is None or company.sector != Sector.INDIAN_DEFENCE:
            return True, None

        ticker = company.ticker
        company_name = company.name

        docs = (
            s.query(Document)
            .filter(
                Document.company_id == company_id,
                Document.parse_status == ParseStatus.PARSED,
            )
            .all()
        )
        # Snapshot doc fields we need outside the session
        doc_records = [
            (d.id, d.period, d.parsed_path, d.document_type) for d in docs
        ]

    if not doc_records:
        logger.info("%s: no PARSED docs", ticker)
        return True, None

    # Step 1: yfinance
    yf_data = fetch_yfinance_quarterly(ticker)

    # Step 2 + 3: bucket docs by quarter and document_type
    period_to_doc_id: dict[str, int] = {}
    orders_by_period: dict[str, list[str]] = defaultdict(list)
    orderbook_by_period: dict[str, list[str]] = defaultdict(list)

    for doc_id, period, parsed_path, doctype in doc_records:
        if period not in period_to_doc_id:
            period_to_doc_id[period] = doc_id

        if doctype in (DocumentType.AWARD_OF_ORDERS, DocumentType.PRESS_RELEASE):
            text = _press_release_text(parsed_path)
            if text:
                orders_by_period[period].append(text)

        elif doctype == DocumentType.INVESTOR_PRESENTATION:
            snippets = _extract_orderbook_snippets(parsed_path)
            if snippets:
                orderbook_by_period[period].extend(snippets)

    # Ensure every yfinance period has a doc-id mapping (even via fallback)
    # so we can save yfinance-only metrics for periods without filings.
    if doc_records:
        fallback_doc_id = doc_records[0][0]
        for period in yf_data.keys():
            period_to_doc_id.setdefault(period, fallback_doc_id)

    # Format prompt blocks
    yf_block = _format_yf_block(yf_data)

    if orders_by_period:
        order_lines = []
        for period in sorted(orders_by_period.keys()):
            order_lines.append(f"\n----- {period} -----")
            for i, text in enumerate(orders_by_period[period]):
                order_lines.append(f"[announcement {i+1}]\n{text}")
        orders_block = "\n".join(order_lines)
    else:
        orders_block = "(no order announcements)"

    if orderbook_by_period:
        ob_lines = []
        for period in sorted(orderbook_by_period.keys()):
            ob_lines.append(f"\n----- {period} -----")
            for s_text in orderbook_by_period[period]:
                ob_lines.append(s_text)
        orderbook_block = "\n".join(ob_lines)
    else:
        orderbook_block = "(no order-book mentions found)"

    est_tokens = (len(yf_block) + len(orders_block) + len(orderbook_block)) // 4
    logger.info(
        "%s: %d docs | yf periods: %d | order periods: %d | orderbook periods: %d | est ~%d tokens",
        ticker, len(doc_records), len(yf_data),
        len(orders_by_period), len(orderbook_by_period), est_tokens,
    )

    # Step 4: ONE LLM call
    chain = _get_chain()
    try:
        result = chain.invoke({
            "company_name": company_name,
            "yf_block": yf_block,
            "orders_block": orders_block,
            "orderbook_block": orderbook_block,
        })
    except Exception as exc:
        logger.exception("%s LLM call failed: %s", ticker, exc)
        with get_session() as s:
            for doc_id, *_ in doc_records:
                doc = s.get(Document, doc_id)
                doc.parse_status = ParseStatus.EXTRACTION_FAILED
                doc.error_message = f"LLM call failed: {str(exc)[:300]}"
        return False, str(exc)

    # Step 5: yfinance overrides
    result = _tier5_yfinance_overrides(result, yf_data)

    # Step 6: persist
    all_rows: list[dict] = []
    for q in result.quarters:
        all_rows.extend(_flatten_quarter_to_rows(q, company_id))
    n_written = _persist_metric_rows(all_rows, period_to_doc_id)

    # Trend notes
    if result.trend_notes:
        with get_session() as s:
            existing = (
                s.query(Metric)
                .filter_by(company_id=company_id, period="ALL", metric_name="trend_notes")
                .one_or_none()
            )
            if existing is None:
                s.add(Metric(
                    company_id=company_id, period="ALL",
                    metric_name="trend_notes",
                    metric_value_text=result.trend_notes,
                    source_document_id=doc_records[0][0],
                ))
            else:
                existing.metric_value_text = result.trend_notes

    with get_session() as s:
        for doc_id, *_ in doc_records:
            doc = s.get(Document, doc_id)
            doc.parse_status = ParseStatus.EXTRACTED
            doc.error_message = None

    logger.info("%s EXTRACTED: %d rows across %d quarters", ticker, n_written, len(result.quarters))
    return True, None


def run_extractor_indian_defence() -> dict[str, int]:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")

    with get_session() as s:
        company_ids = [
            c.id for c in s.query(Company)
            .filter(Company.sector == Sector.INDIAN_DEFENCE)
            .all()
        ]

    logger.info("Indian Defence extraction: %d companies", len(company_ids))
    extracted = failed = 0
    for cid in company_ids:
        ok, err = _extract_one_defence_company(cid)
        if ok and err is None:
            extracted += 1
        else:
            failed += 1

    summary = {"companies": len(company_ids), "extracted": extracted, "failed": failed}
    logger.info("Indian Defence extractor complete: %s", summary)
    return summary
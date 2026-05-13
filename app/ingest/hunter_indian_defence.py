"""Hunter for Indian Defence companies via BSE.

For each company:
  1. Fetch BSE announcements over the past 3 years
  2. Filter to: award_of_orders, press_release, investor_presentation
  3. Create Document records — Postman/Reader handle download + parse
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from app.db import (
    Company, Document, DocumentType, ParseStatus, Sector, get_session
)
from app.ingest.bse_client import (
    fetch_announcements, filter_by_category, date_to_period,
)

logger = logging.getLogger(__name__)


# Verify scrip codes against bseindia.com before running.
INDIAN_DEFENCE_COMPANIES = [
    {"name": "Hindustan Aeronautics",  "ticker": "HAL.NS",          "scrip": "541154"},
    {"name": "Bharat Electronics",     "ticker": "BEL.NS",          "scrip": "500049"},
    {"name": "MTAR Technologies",      "ticker": "MTARTECH.NS",     "scrip": "543270"},
    {"name": "Paras Defence",          "ticker": "PARAS.NS",        "scrip": "543282"},
    {"name": "Astra Microwave",        "ticker": "ASTRAMICRO.NS",   "scrip": "532493"},
    {"name": "Data Patterns",          "ticker": "DATAPATTNS.NS",   "scrip": "543428"},
    {"name": "Zen Technologies",       "ticker": "ZENTEC.NS",       "scrip": "533339"},
    {"name": "Bharat Forge",           "ticker": "BHARATFORG.NS",   "scrip": "500493"}
]


# Map our category keys to DocumentType
CATEGORY_TO_DOCTYPE = {
    "award_of_orders":         DocumentType.AWARD_OF_ORDERS,
    "press_release":           DocumentType.PRESS_RELEASE,
    "investor_presentation":   DocumentType.INVESTOR_PRESENTATION,
    "annual_report":           DocumentType.ANNUAL_REPORT,
}


def seed_indian_defence_companies() -> int:
    """Insert company rows. Idempotent."""
    added = 0
    with get_session() as s:
        for c in INDIAN_DEFENCE_COMPANIES:
            existing = (
                s.query(Company)
                .filter_by(ticker=c["ticker"], exchange="NSE")
                .one_or_none()
            )
            if existing is None:
                s.add(Company(
                    name=c["name"],
                    sector=Sector.INDIAN_DEFENCE,
                    ticker=c["ticker"],
                    exchange="NSE",
                ))
                added += 1
    logger.info("Seeded %d Indian Defence companies", added)
    return added


def run_hunter_indian_defence(years_back: int = 3) -> dict[str, int]:
    """For each Indian Defence company, find relevant BSE filings."""
    seed_indian_defence_companies()

    to_date = date.today()
    from_date = to_date - timedelta(days=365 * years_back)

    found = 0
    skipped = 0

    with get_session() as s:
        companies = (
            s.query(Company)
            .filter(Company.sector == Sector.INDIAN_DEFENCE)
            .all()
        )
        company_ticker_to_id = {c.ticker: c.id for c in companies}

    for cfg in INDIAN_DEFENCE_COMPANIES:
        ticker = cfg["ticker"]
        scrip = cfg["scrip"]
        company_id = company_ticker_to_id.get(ticker)
        if company_id is None:
            continue

        try:
            announcements = fetch_announcements(scrip, from_date, to_date)
        except Exception as exc:
            logger.warning("BSE fetch failed for %s: %s", ticker, exc)
            continue

        for cat_key, doctype in CATEGORY_TO_DOCTYPE.items():
            relevant = filter_by_category(announcements, [cat_key])
            for ann in relevant:
                url = ann.get("attachment_url")
                if not url:
                    continue
                period = date_to_period(ann["date"])
                if period is None:
                    continue

                with get_session() as s:
                    existing = s.query(Document).filter_by(source_url=url).one_or_none()
                    if existing is not None:
                        skipped += 1
                        continue
                    s.add(Document(
                        company_id=company_id,
                        source_url=url,
                        document_type=doctype,
                        period=period,
                        parse_status=ParseStatus.PENDING,
                    ))
                    found += 1

    summary = {"new_documents": found, "already_present": skipped}
    logger.info("Hunter Indian Defence complete: %s", summary)
    return summary
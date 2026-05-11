"""Hunter for Indian Fintech via BSE — mirrors hunter_indian_defence."""
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


INDIAN_FINTECH_COMPANIES = [
    {"name": "Bajaj Finance",          "ticker": "BAJFINANCE.NS",  "scrip": "500034"},
    {"name": "SBI Cards",              "ticker": "SBICARD.NS",     "scrip": "543066"},
    {"name": "PB Fintech",             "ticker": "POLICYBZR.NS",   "scrip": "543390"},
    {"name": "CAMS",                   "ticker": "CAMS.NS",        "scrip": "543232"},
    {"name": "CDSL",                   "ticker": "CDSL.NS",        "scrip": "543287"},
    {"name": "Zaggle",                 "ticker": "ZAGGLE.NS",      "scrip": "543987"},
    {"name": "CreditAccess Grameen",   "ticker": "CREDITACC.NS",   "scrip": "541770"},
    {"name": "Five Star Business Fin", "ticker": "FIVESTAR.NS",    "scrip": "543663"},
]

# Investor presentations only — no concall transcripts, no press releases
CATEGORY_TO_DOCTYPE = {
    "investor_presentation": DocumentType.INVESTOR_PRESENTATION,
}


def seed_indian_fintech_companies() -> int:
    added = 0
    with get_session() as s:
        for c in INDIAN_FINTECH_COMPANIES:
            existing = (
                s.query(Company)
                .filter_by(ticker=c["ticker"], exchange="NSE")
                .one_or_none()
            )
            if existing is None:
                s.add(Company(
                    name=c["name"],
                    sector=Sector.INDIAN_FINTECH,
                    ticker=c["ticker"],
                    exchange="NSE",
                ))
                added += 1
    logger.info("Seeded %d Indian Fintech companies", added)
    return added


def run_hunter_indian_fintech(years_back: int = 3) -> dict[str, int]:
    seed_indian_fintech_companies()

    to_date = date.today()
    from_date = to_date - timedelta(days=365 * years_back)

    found = 0
    skipped = 0

    with get_session() as s:
        companies = (
            s.query(Company)
            .filter(Company.sector == Sector.INDIAN_FINTECH)
            .all()
        )
        ticker_to_id = {c.ticker: c.id for c in companies}

    for cfg in INDIAN_FINTECH_COMPANIES:
        company_id = ticker_to_id.get(cfg["ticker"])
        if company_id is None:
            continue
        try:
            anns = fetch_announcements(cfg["scrip"], from_date, to_date)
        except Exception as exc:
            logger.warning("BSE fetch failed %s: %s", cfg["ticker"], exc)
            continue

        for cat_key, doctype in CATEGORY_TO_DOCTYPE.items():
            relevant = filter_by_category(anns, [cat_key])
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
    logger.info("Hunter Indian Fintech complete: %s", summary)
    return summary
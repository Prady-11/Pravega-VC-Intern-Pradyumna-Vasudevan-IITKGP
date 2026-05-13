"""Hunter for Annual Reports — Indian Fintech + Indian Defence via BSE.

Scoped to the last 1 year only (annual reports filed within the past 12
months). Intentionally narrow: this hunter's only job is annual reports.
All other document types are handled by the sector-specific hunters.

Pipeline:
  1. Ensures both sectors' companies are seeded in the DB.
  2. For each company, fetches BSE announcements (1-year window).
  3. Filters to the "annual_report" category only.
  4. Creates Document rows with parse_status=PENDING.
  5. Postman + Reader pick up from there as normal.

Run:
    python -m app.ingest.hunter_annual_reports
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
from app.ingest.hunter_indian_fintech import (
    INDIAN_FINTECH_COMPANIES, seed_indian_fintech_companies,
)
from app.ingest.hunter_indian_defence import (
    INDIAN_DEFENCE_COMPANIES, seed_indian_defence_companies,
)

logger = logging.getLogger(__name__)

# All companies across both sectors in one flat list.
# Each entry carries its sector so we can look up the right company_id.
_ALL_COMPANIES: list[dict] = [
    {**c, "sector": Sector.INDIAN_FINTECH} for c in INDIAN_FINTECH_COMPANIES
] + [
    {**c, "sector": Sector.INDIAN_DEFENCE} for c in INDIAN_DEFENCE_COMPANIES
]


def _build_ticker_to_id() -> dict[str, int]:
    """Return a {ticker: company_id} map for all fintech + defence companies."""
    sectors = [Sector.INDIAN_FINTECH, Sector.INDIAN_DEFENCE]
    with get_session() as s:
        companies = (
            s.query(Company)
            .filter(Company.sector.in_(sectors))
            .all()
        )
        return {c.ticker: c.id for c in companies}


def run_hunter_annual_reports(years_back: int = 1) -> dict[str, int]:
    """
    Discover annual report filings on BSE for all fintech + defence
    companies filed within the last `years_back` years (default: 1).
    """
    # Ensure all companies exist in the DB before querying them
    seed_indian_fintech_companies()
    seed_indian_defence_companies()

    to_date   = date.today()
    from_date = to_date - timedelta(days=365 * years_back)

    ticker_to_id = _build_ticker_to_id()

    found   = 0
    skipped = 0

    for cfg in _ALL_COMPANIES:
        ticker     = cfg["ticker"]
        scrip      = cfg["scrip"]
        company_id = ticker_to_id.get(ticker)

        if company_id is None:
            logger.warning("Company not found in DB: %s — run seed first.", ticker)
            continue

        try:
            announcements = fetch_announcements(scrip, from_date, to_date)
        except Exception as exc:
            logger.warning("BSE fetch failed for %s: %s", ticker, exc)
            continue

        relevant = filter_by_category(announcements, ["annual_report"])

        logger.info(
            "[%s] %d announcements fetched, %d annual report(s) found",
            ticker, len(announcements), len(relevant),
        )

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
                    document_type=DocumentType.ANNUAL_REPORT,
                    period=period,
                    parse_status=ParseStatus.PENDING,
                ))
                found += 1

    summary = {"new_documents": found, "already_present": skipped}
    logger.info("Hunter Annual Reports complete: %s", summary)
    return summary


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )
    result = run_hunter_annual_reports(years_back=1)
    print(result)
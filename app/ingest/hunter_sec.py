"""Hunter for SEC EDGAR filings.

Discovers 10-Q and 10-K filings for US biotech companies in our database, going
back 3 years from today. Inserts one row per discovered filing into the
`documents` table with parse_status='pending'. Re-running is safe — the
unique constraint on documents.source_url prevents duplicates.

Implementation notes:
  * SEC requires a User-Agent header identifying the requester. We read it from
    settings.sec_user_agent and refuse to run if it's empty.
  * SEC's documented rate limit is 10 req/s; we sleep 0.1s between calls
    defensively. Total calls per run: ~1 (ticker map) + N companies.
  * Filings JSON has a parallel-array shape — we zip the columns into rows.
  * 10-K filings are annualized: we label them "FY {year}".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.db import Company, Document, DocumentType, ParseStatus, Sector, get_session

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_no_dashes}/{document}"
)

LOOKBACK_YEARS = 4
RELEVANT_FORMS = {"10-Q", "10-K"}
RATE_LIMIT_SLEEP_SECONDS = 0.1


@dataclass(frozen=True)
class DiscoveredFiling:
    """In-memory record of one filing returned by SEC, before DB insert."""

    cik: str
    ticker: str
    form: str
    period: str
    source_url: str
    accession_number: str
    report_date: date


def _build_archive_url(cik: str, accession_number: str, primary_document: str) -> str:
    """Translate SEC's parallel-array fields into a fetchable URL."""
    return SEC_ARCHIVE_URL.format(
        cik_no_zeros=str(int(cik)),
        accession_no_dashes=accession_number.replace("-", ""),
        document=primary_document,
    )


def _derive_period(report_date: date, form: str) -> str:
    """Format the period label from a date and form type.

    Quarterly:  '2024-09-30' + '10-Q' -> 'Q3 2024'
    Annual:     '2023-12-31' + '10-K' -> 'FY 2023'
    """
    if form == "10-K":
        return f"FY {report_date.year}"
    quarter = (report_date.month - 1) // 3 + 1
    return f"Q{quarter} {report_date.year}"


def _form_to_doc_type(form: str) -> DocumentType:
    return {"10-Q": DocumentType.SEC_10Q, "10-K": DocumentType.SEC_10K}[form]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _http_get_json(url: str, user_agent: str) -> dict:
    """GET a JSON URL with the SEC's required User-Agent. Retries 3x on failure."""
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def fetch_ticker_to_cik_map(user_agent: str) -> dict[str, str]:
    """Build a {ticker: zero-padded-cik} dict from SEC's public mapping file."""
    logger.info("Fetching SEC ticker -> CIK map")
    raw = _http_get_json(SEC_TICKERS_URL, user_agent)
    mapping: dict[str, str] = {}
    for entry in raw.values():
        ticker = entry["ticker"].upper()
        cik_padded = str(entry["cik_str"]).zfill(10)
        mapping[ticker] = cik_padded
    logger.info("Loaded %d ticker -> CIK mappings", len(mapping))
    return mapping


def fetch_filings_index(cik: str, ticker: str, user_agent: str) -> list[DiscoveredFiling]:
    """Return all 10-Q and 10-K filings for a CIK, going back LOOKBACK_YEARS."""
    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    logger.info("Fetching filings index for %s (CIK %s)", ticker, cik)
    raw = _http_get_json(url, user_agent)

    recent = raw.get("filings", {}).get("recent", {})
    forms: list[str] = recent.get("form", [])
    filing_dates: list[str] = recent.get("filingDate", [])
    accession_numbers: list[str] = recent.get("accessionNumber", [])
    primary_documents: list[str] = recent.get("primaryDocument", [])
    report_dates: list[str] = recent.get("reportDate", [])

    cutoff = date.today() - timedelta(days=365 * LOOKBACK_YEARS)
    discovered: list[DiscoveredFiling] = []

    for form, filing_date_str, acc_no, primary_doc, report_date_str in zip(
        forms, filing_dates, accession_numbers, primary_documents, report_dates, strict=True
    ):
        if form not in RELEVANT_FORMS:
            continue
        if not report_date_str:
            continue
        report_dt = date.fromisoformat(report_date_str)
        if report_dt < cutoff:
            continue

        discovered.append(
            DiscoveredFiling(
                cik=cik,
                ticker=ticker,
                form=form,
                period=_derive_period(report_dt, form),
                source_url=_build_archive_url(cik, acc_no, primary_doc),
                accession_number=acc_no,
                report_date=report_dt,
            )
        )

    logger.info("Found %d 10-Q/10-K filings for %s in lookback window", len(discovered), ticker)
    return discovered


def _persist_filings(filings: list[DiscoveredFiling], company: Company) -> int:
    """Insert new filings as documents rows. Skip URLs already present.

    Returns the number of new rows inserted.
    """
    if not filings:
        return 0

    inserted = 0
    with get_session() as s:
        existing_urls = {
            url for (url,) in s.query(Document.source_url).filter(
                Document.company_id == company.id
            ).all()
        }

        for filing in filings:
            if filing.source_url in existing_urls:
                continue
            s.add(
                Document(
                    company_id=company.id,
                    source_url=filing.source_url,
                    document_type=_form_to_doc_type(filing.form),
                    period=filing.period,
                    parse_status=ParseStatus.PENDING,
                )
            )
            inserted += 1

    return inserted


def run_hunter_for_us_biotech() -> dict[str, int]:
    """Discover new 10-Q/10-K filings for every US biotech company in the DB.

    Returns a dict {ticker: new_filings_inserted} for logging by the caller.
    """
    if not settings.sec_user_agent:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. Add it to .env in the form "
            "'Your Name your-email@example.com'. SEC blocks anonymous bots."
        )

    user_agent = settings.sec_user_agent

    with get_session() as s:
        biotech_companies = s.query(Company).filter(
            Company.sector == Sector.US_BIOTECH
        ).all()
        company_records = [
            (c.id, c.name, c.ticker) for c in biotech_companies
        ]

    if not company_records:
        logger.warning("No US biotech companies found in DB. Run seed_companies first.")
        return {}

    ticker_to_cik = fetch_ticker_to_cik_map(user_agent)
    results: dict[str, int] = {}

    for company_id, name, ticker in company_records:
        cik = ticker_to_cik.get(ticker)
        if not cik:
            logger.warning("No CIK found for ticker %s (%s) — skipping", ticker, name)
            results[ticker] = 0
            continue

        try:
            filings = fetch_filings_index(cik, ticker, user_agent)
        except Exception as exc:
            logger.exception("Filings fetch failed for %s — skipping: %s", ticker, exc)
            results[ticker] = 0
            continue

        with get_session() as s:
            company = s.get(Company, company_id)
            if company is None:
                logger.warning("Company %s vanished mid-run — skipping", ticker)
                results[ticker] = 0
                continue
            inserted = _persist_filings(filings, company)

        results[ticker] = inserted
        logger.info("%s: %d new filings inserted", ticker, inserted)
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)

    total_inserted = sum(results.values())
    logger.info("Hunter complete: %d new filings across %d companies", total_inserted, len(results))
    return results
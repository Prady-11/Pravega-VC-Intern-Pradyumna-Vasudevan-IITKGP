"""Track A: Deterministic financial facts via SEC XBRL API."""
from __future__ import annotations

import calendar
import logging
import re
from typing import NamedTuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger(__name__)

XBRL_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

CONCEPTS = {
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndCashEquivalentsAndRestrictedCash",
    ],
    "revenue_product": [
        "ProductRevenue",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "revenue_collaboration": [
        "RevenueFromCollaborativeArrangement",
        "CollaborativeArrangementRevenue",
        "LicenseAndCollaborationRevenue",
    ],
    "revenue_total": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ],
    "rd_expense": [
        "ResearchAndDevelopmentExpense",
    ],
}


class XbrlFacts(NamedTuple):
    cash_usd_mn: float | None
    revenue_product_usd_mn: float | None
    revenue_collaboration_usd_mn: float | None
    revenue_total_usd_mn: float | None
    rd_expense_usd_mn: float | None
    provenance: list[str]


def extract_cik_from_url(source_url: str) -> str | None:
    m = re.search(r"/Archives/edgar/data/(\d+)/", source_url)
    return str(int(m.group(1))).zfill(10) if m else None


def period_to_end_date(period: str) -> str | None:
    fy = re.match(r"FY\s*(\d{4})", period, re.IGNORECASE)
    if fy:
        return f"{fy.group(1)}-12-31"
    q = re.match(r"Q([1-4])\s*(\d{4})", period, re.IGNORECASE)
    if q:
        quarter, year = int(q.group(1)), int(q.group(2))
        end_month = quarter * 3
        last_day = calendar.monthrange(year, end_month)[1]
        return f"{year}-{end_month:02d}-{last_day:02d}"
    return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def _fetch_company_facts(cik: str) -> dict:
    url = XBRL_FACTS_URL.format(cik=cik)
    headers = {"User-Agent": settings.sec_user_agent, "Accept": "application/json"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def _lookup(
    usgaap: dict,
    concepts: list[str],
    end_date: str,
    form_prefix: str,
) -> tuple[float | None, str | None]:
    """First-hit search across concept names. form_prefix matches '10-Q' AND '10-Q/A'."""
    for concept in concepts:
        entries = usgaap.get(concept, {}).get("units", {}).get("USD", [])
        for entry in entries:
            form = entry.get("form", "")
            if entry.get("end") == end_date and form.startswith(form_prefix):
                return entry["val"] / 1_000_000, concept
    return None, None


def fetch_xbrl_facts(
    source_url: str,
    period: str,
    document_type_value: str,
) -> XbrlFacts:
    empty = XbrlFacts(None, None, None, None, None, [])

    cik = extract_cik_from_url(source_url)
    if not cik:
        return empty
    end_date = period_to_end_date(period)
    if not end_date:
        return empty
    if not settings.sec_user_agent:
        return empty

    form_prefix = "10-K" if "10k" in document_type_value.lower() else "10-Q"

    try:
        facts_json = _fetch_company_facts(cik)
    except Exception as exc:
        logger.warning("XBRL fetch failed CIK=%s period=%s: %s", cik, period, exc)
        return empty

    usgaap = facts_json.get("facts", {}).get("us-gaap", {})

    def g(key: str) -> tuple[float | None, str | None]:
        return _lookup(usgaap, CONCEPTS[key], end_date, form_prefix)

    cash, cash_c   = g("cash")
    rprod, rprod_c = g("revenue_product")
    rcol, rcol_c   = g("revenue_collaboration")
    rtot, rtot_c   = g("revenue_total")
    rd, rd_c       = g("rd_expense")

    provenance: list[str] = []
    if cash is not None:  provenance.append(f"[XBRL:{cash_c}] Cash & Equivalents: ${cash:.1f}M")
    if rprod is not None: provenance.append(f"[XBRL:{rprod_c}] Product Revenue: ${rprod:.1f}M")
    if rcol is not None:  provenance.append(f"[XBRL:{rcol_c}] Collaboration Revenue: ${rcol:.1f}M")
    if rtot is not None:  provenance.append(f"[XBRL:{rtot_c}] Total Revenue: ${rtot:.1f}M")
    if rd is not None:    provenance.append(f"[XBRL:{rd_c}] R&D Expense: ${rd:.1f}M")

    logger.info("XBRL: %d facts CIK=%s %s", len(provenance), cik, period)
    return XbrlFacts(cash, rprod, rcol, rtot, rd, provenance)

def fetch_all_quarters_for_company(cik: str) -> dict[str, dict[str, float]]:
    """Fetch every period's financials for one CIK in one API call.

    Returns: {period_string: {field_name: value_in_millions, ...}, ...}
    Example: {"Q3 2024": {"cash_usd_mn": 3200.0, "revenue_product_usd_mn": 1826.0}, ...}
    """
    if not settings.sec_user_agent:
        return {}

    try:
        facts_json = _fetch_company_facts(cik)
    except Exception as exc:
        logger.warning("XBRL fetch failed CIK=%s: %s", cik, exc)
        return {}

    usgaap = facts_json.get("facts", {}).get("us-gaap", {})
    by_period: dict[str, dict[str, float]] = {}

    field_map = {
        "cash_and_equiv_usd_mn": "cash",
        "revenue_product_usd_mn": "revenue_product",
        "revenue_collaboration_usd_mn": "revenue_collaboration",
        "revenue_total_usd_mn": "revenue_total",
        "rd_expense_usd_mn": "rd_expense",
    }

    for field_name, concept_key in field_map.items():
        for concept in CONCEPTS[concept_key]:
            entries = usgaap.get(concept, {}).get("units", {}).get("USD", [])
            for entry in entries:
                form = entry.get("form", "")
                if not (form.startswith("10-Q") or form.startswith("10-K")):
                    continue
                end_date = entry.get("end")
                if not end_date:
                    continue

                # Convert end_date "2024-09-30" → period "Q3 2024" or "FY 2024"
                period = _end_date_to_period(end_date, form)
                if period is None:
                    continue

                by_period.setdefault(period, {})[field_name] = entry["val"] / 1_000_000
            # First concept that yielded data wins for this field — break to next field
            if any(field_name in v for v in by_period.values()):
                break

    logger.info("XBRL: fetched %d periods for CIK=%s", len(by_period), cik)
    return by_period


def _end_date_to_period(end_date: str, form: str) -> str | None:
    """'2024-09-30' + '10-Q' → 'Q3 2024'.  '2024-12-31' + '10-K' → 'FY 2024'."""
    try:
        year, month, _ = end_date.split("-")
        year_i = int(year)
        month_i = int(month)
    except (ValueError, AttributeError):
        return None

    if form.startswith("10-K"):
        return f"FY {year_i}"
    quarter = (month_i - 1) // 3 + 1
    return f"Q{quarter} {year_i}"
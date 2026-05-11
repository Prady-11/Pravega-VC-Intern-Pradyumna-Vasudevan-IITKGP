"""Track C: ClinicalTrials.gov v2 API."""
from __future__ import annotations
from app.config import settings
import logging
from typing import NamedTuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.db import Sector

logger = logging.getLogger(__name__)

CT_API = "https://clinicaltrials.gov/api/v2/studies"


class TrialSummary(NamedTuple):
    indication: str
    phase: str
    status: str
    nct_id: str


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5), reraise=True)
def _fetch(sponsor: str) -> list[dict]:
    params = [
        ("query.spons", sponsor),
        ("filter.overallStatus", "RECRUITING"),
        ("filter.overallStatus", "ACTIVE_NOT_RECRUITING"),
        ("filter.overallStatus", "COMPLETED"),
        ("filter.overallStatus", "ENROLLING_BY_INVITATION"),
        ("pageSize", "100"),
        ("fields", "NCTId,BriefTitle,Phase,OverallStatus,Condition"),
    ]
    headers = {
        "User-Agent": settings.sec_user_agent or "sector-intel/1.0",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=20.0, headers=headers) as client:
        r = client.get(CT_API, params=params)
        r.raise_for_status()
        return r.json().get("studies", [])


def fetch_trials(company_name: str, sector: Sector) -> list[TrialSummary]:
    if sector != Sector.US_BIOTECH:
        return []

    try:
        studies = _fetch(company_name)
    except Exception as exc:
        logger.warning("ClinicalTrials fetch failed for %s: %s", company_name, exc)
        return []

    out: list[TrialSummary] = []
    for s in studies[:40]:
        proto = s.get("protocolSection", {})
        nct = proto.get("identificationModule", {}).get("nctId", "")
        phases = proto.get("designModule", {}).get("phases", [])
        status = proto.get("statusModule", {}).get("overallStatus", "")
        conditions = proto.get("conditionsModule", {}).get("conditions", [])
        indication = (
            conditions[0] if conditions
            else proto.get("identificationModule", {}).get("briefTitle", "Unknown")
        )
        out.append(TrialSummary(indication, phases[0] if phases else "N/A", status, nct))

    logger.info("ClinicalTrials: %d trials for %s", len(out), company_name)
    return out
"""Sector-specific keyword bags for chunk pre-filtering.

A parsed 10-Q has 20-30 chunks; only ~30% contain extractable metrics. The rest
are cover pages, ToCs, legal disclaimers, auditor signature blocks. Sending all
chunks to the LLM costs ~3× more and dilutes the model's focus.

Heuristic: a chunk passes the filter if it contains any keyword from the sector's
bag (case-insensitive substring match). We choose recall over precision — better
to send a few extra noise chunks than miss a real metric.

These bags are deliberately broad. Tighten them only if extraction cost becomes
a problem; loosening them is hard once you've left signal on the table.
"""
from __future__ import annotations

from app.db import Sector

# US Biotech: pipeline stages, trial language, cash/runway, revenue mix, AI/ML.
_US_BIOTECH_KEYWORDS: frozenset[str] = frozenset({
    # Pipeline & trials
    "phase 1", "phase 2", "phase 3", "phase i", "phase ii", "phase iii",
    "preclinical", "clinical trial", "clinical study", "trial",
    "indication", "candidate", "pipeline",
    "fda", "nda", "bla", "approval", "approved", "filing",
    "readout", "endpoint", "efficacy", "safety",
    # Cash position
    "cash and cash equivalents", "cash, cash equivalents",
    "marketable securities", "runway", "burn rate",
    # Revenue mix
    "product revenue", "royalty", "royalties",
    "collaboration revenue", "license revenue", "milestone payment",
    "total revenue", "revenue,",
    # AI/ML callouts
    "artificial intelligence", "machine learning", "ai/ml", "ai-driven",
    "ml platform", "deep learning",
})

# Indian Fintech: AUM, NPA, NIM, digital metrics, cost of funds.
_INDIAN_FINTECH_KEYWORDS: frozenset[str] = frozenset({
    # Loan book
    "aum", "assets under management", "loan book", "advances", "disbursement",
    # Asset quality
    "gnpa", "gross npa", "net npa", "stage 3", "credit cost", "provision",
    "slippage", "write-off", "write off",
    # Yields & margins
    "nim", "net interest margin", "yield on advances", "cost of funds",
    "spread", "interest income",
    # Digital metrics
    "monthly active users", "mau", "transactions", "transaction value",
    "digital", "upi", "payments volume", "gmv",
})

# Indian Defence: order book, revenue growth, EBITDA margin, R&D, new orders.
_INDIAN_DEFENCE_KEYWORDS: frozenset[str] = frozenset({
    # Order book
    "order book", "order backlog", "order inflow", "order intake",
    "domestic order", "export order",
    # Margins
    "ebitda", "ebitda margin", "operating margin", "revenue growth",
    "topline", "gross margin",
    # Investment
    "research and development", "r&d", "research & development",
    # Programs
    "indigenous", "make in india", "drdo", "moa", "mou", "contract awarded",
    "tejas", "lca", "akash", "brahmos",  # well-known program names
})

_SECTOR_KEYWORDS: dict[Sector, frozenset[str]] = {
    Sector.US_BIOTECH: _US_BIOTECH_KEYWORDS,
    Sector.INDIAN_FINTECH: _INDIAN_FINTECH_KEYWORDS,
    Sector.INDIAN_DEFENCE: _INDIAN_DEFENCE_KEYWORDS,
}


def keywords_for_sector(sector: Sector) -> frozenset[str]:
    """Return the lowercase keyword bag for a sector."""
    return _SECTOR_KEYWORDS[sector]


def chunk_is_relevant(chunk_text: str, sector: Sector) -> bool:
    """True if the chunk contains at least one sector keyword (case-insensitive)."""
    haystack = chunk_text.lower()
    return any(kw in haystack for kw in _SECTOR_KEYWORDS[sector])
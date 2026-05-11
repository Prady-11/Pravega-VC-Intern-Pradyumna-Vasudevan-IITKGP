"""Track B: Deterministic document mining via anchor phrases + regex."""
from __future__ import annotations

import logging
import re
from typing import NamedTuple

from app.db import Sector

logger = logging.getLogger(__name__)

BIOTECH_ANCHORS: list[tuple[str, str]] = [
    ("liquidity and capital resources", "Cash/Runway discussion"),
    ("cash runway", "Explicit runway"),
    ("sufficient to fund", "Runway language"),
    ("cash and cash equivalents were", "Cash position statement"),
    ("total pipeline", "Pipeline overview"),
    ("clinical pipeline", "Clinical pipeline"),
    ("phase 3 program", "Phase 3 programs"),
    ("artificial intelligence", "AI platform mention"),
    ("machine learning", "ML platform mention"),
    ("net loss was", "Net loss"),
]

FINTECH_ANCHORS: list[tuple[str, str]] = [
    ("assets under management", "AUM"),
    ("aum grew", "AUM growth"),
    ("gross npa", "Gross NPA"),
    ("net npa", "Net NPA"),
    ("net interest margin", "NIM"),
    ("cost of funds", "Cost of funds"),
    ("digital transactions", "Digital transaction volume"),
    ("monthly active users", "MAU"),
    ("credit cost", "Credit cost"),
]

DEFENCE_ANCHORS: list[tuple[str, str]] = [
    ("order book", "Order book"),
    ("order inflow", "Order inflow"),
    ("ebitda margin", "EBITDA margin"),
    ("operating margin", "Operating margin"),
    ("export orders", "Export orders"),
    ("domestic orders", "Domestic orders"),
    ("research and development", "R&D spend"),
    ("revenue grew", "Revenue growth"),
]

SECTOR_ANCHORS: dict[Sector, list[tuple[str, str]]] = {
    Sector.US_BIOTECH:    BIOTECH_ANCHORS,
    Sector.INDIAN_FINTECH: FINTECH_ANCHORS,
    Sector.INDIAN_DEFENCE: DEFENCE_ANCHORS,
}

_PHASE_RE = {
    "Phase 1":  re.compile(r"\bphase[\s\-]?1\b|\bphase\s+I\b", re.IGNORECASE),
    "Phase 2":  re.compile(r"\bphase[\s\-]?2\b|\bphase\s+II\b", re.IGNORECASE),
    "Phase 3":  re.compile(r"\bphase[\s\-]?3\b|\bphase\s+III\b", re.IGNORECASE),
    "NDA/BLA":  re.compile(r"\b(?:NDA|BLA|sNDA|sBLA)\b"),
}


class MinerOutput(NamedTuple):
    snippets: list[str]
    phase_hint: str | None


def _snippet(text: str, anchor: str, window: int = 220) -> str | None:
    idx = text.lower().find(anchor.lower())
    if idx == -1:
        return None
    start = max(0, idx - 20)
    raw = text[start: idx + window].replace("\n", " ").strip()
    return raw if len(raw) > 30 else None


def mine_document(chunks: list[dict], sector: Sector) -> MinerOutput:
    anchors = SECTOR_ANCHORS.get(sector, [])
    all_text = "\n".join(c["text"] for c in chunks)

    found: dict[str, str] = {}
    for phrase, label in anchors:
        if label in found:
            continue
        for chunk in chunks:
            snip = _snippet(chunk["text"], phrase)
            if snip:
                found[label] = snip
                break

    snippets = [f"[Snippet:{label}] {snip}" for label, snip in found.items()]

    phase_hint = None
    if sector == Sector.US_BIOTECH:
        counts = {p: len(rx.findall(all_text)) for p, rx in _PHASE_RE.items()}
        if any(v > 0 for v in counts.values()):
            phase_hint = "Phase mentions: " + ", ".join(
                f"{p} x{n}" for p, n in counts.items() if n > 0
            )

    logger.info("Anchor miner: %d snippets sector=%s", len(snippets), sector)
    return MinerOutput(snippets, phase_hint)
"""Regex/anchor-based section extraction.

Pulls just the MD&A and Business Overview sections from a parsed filing's
chunks. Drops everything else (legal disclaimers, financial statements
tables which we get from XBRL, exhibits).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Section start anchors — matches "Item 7. Management's Discussion..." etc.
MDA_PATTERNS = [
    re.compile(r"item\s*[27]\b.*?management.{0,40}discussion", re.IGNORECASE | re.DOTALL),
    re.compile(r"liquidity\s+and\s+capital\s+resources", re.IGNORECASE),
    re.compile(r"results\s+of\s+operations", re.IGNORECASE),
]
BUSINESS_PATTERNS = [
    re.compile(r"item\s*1\b.*?business\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\boverview\b.{0,40}\bpipeline", re.IGNORECASE | re.DOTALL),
    re.compile(r"clinical\s+programs?", re.IGNORECASE),
    re.compile(r"our\s+pipeline", re.IGNORECASE),
]

# Skip patterns — chunks that are pure noise
SKIP_PATTERNS = [
    re.compile(r"\bsignatures?\s+pursuant\s+to", re.IGNORECASE),
    re.compile(r"\bexhibit\s+index\b", re.IGNORECASE),
    re.compile(r"\bcondensed\s+consolidated\s+balance", re.IGNORECASE),  # we have XBRL
]


def is_relevant_chunk(chunk_text: str) -> bool:
    """A chunk is relevant if it matches any MD&A or Business pattern and no skip."""
    if any(p.search(chunk_text) for p in SKIP_PATTERNS):
        return False
    return any(p.search(chunk_text) for p in MDA_PATTERNS + BUSINESS_PATTERNS)


def extract_relevant_text(chunks: list[dict], max_chars: int = 8000) -> str:
    """Concatenate relevant chunks up to a char budget."""
    selected: list[str] = []
    total = 0
    for chunk in chunks:
        text = chunk.get("text", "")
        if not is_relevant_chunk(text):
            continue
        if total + len(text) > max_chars:
            text = text[: max_chars - total]
        selected.append(text)
        total += len(text)
        if total >= max_chars:
            break
    result = "\n\n--- chunk break ---\n\n".join(selected)
    logger.debug("Section router: kept %d chunks (%d chars)", len(selected), len(result))
    return result
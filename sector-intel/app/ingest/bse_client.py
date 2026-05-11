"""BSE corporate announcements API client.

The endpoint returns JSON for a given scrip code + date range. We filter
client-side by category (e.g. 'Award of Orders', 'Investor Presentation',
'Press Release') because BSE category strings vary slightly over time.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BSE_ANNOUNCEMENTS_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
BSE_PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# BSE blocks default user-agents — must look like a browser
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# Category regex patterns (case-insensitive substring match)
CATEGORY_PATTERNS: dict[str, re.Pattern] = {
    "award_of_orders":        re.compile(r"award.{0,5}of.{0,5}order|order.{0,5}receipt", re.IGNORECASE),
    "investor_presentation":  re.compile(r"investor.{0,5}meet.{0,15}outcome", re.IGNORECASE),
    "concall_transcript":     re.compile(r"earnings.{0,5}call.{0,5}transcript|concall\s+transcript", re.IGNORECASE),
    "press_release":          re.compile(r"press\s*release|media\s*release", re.IGNORECASE),
}


def fetch_announcements(
    scrip_code: str,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    """Fetch BSE announcements. BSE caps each request at ~90 days,
    so we slide a 75-day window across the requested range.
    """
    from datetime import timedelta

    all_rows: list[dict[str, Any]] = []
    window_days = 75

    cursor_to = to_date
    while cursor_to > from_date:
        cursor_from = max(from_date, cursor_to - timedelta(days=window_days))

        page = 1
        while page <= 20:
            params = {
                "pageno": page,
                "strCat": "-1",
                "subcategory": "-1",
                "strPrevDate": cursor_from.strftime("%Y%m%d"),
                "strScrip": scrip_code,
                "strSearch": "P",
                "strToDate": cursor_to.strftime("%Y%m%d"),
                "strType": "C",
            }
            try:
                with httpx.Client(timeout=30.0, headers=_HEADERS) as client:
                    resp = client.get(BSE_ANNOUNCEMENTS_URL, params=params)
                    resp.raise_for_status()
                    payload = resp.json()
            except Exception as exc:
                logger.warning(
                    "BSE fetch failed (scrip %s, %s→%s, page %d): %s",
                    scrip_code, cursor_from, cursor_to, page, exc,
                )
                break

            rows = payload.get("Table") or []
            if not rows:
                break

            for r in rows:
                attachment = r.get("ATTACHMENTNAME") or ""
                url = (BSE_PDF_BASE + attachment) if attachment else ""
                all_rows.append({
                    "date": r.get("NEWS_DT") or r.get("DT_TM"),
                    "category": (
                        r.get("ANNOUNCEMENT_TYPE")
                        or r.get("CATEGORYNAME")
                        or r.get("SUB_CATEGORY")
                        or ""
                    ),
                    "headline": r.get("NEWSSUB") or r.get("HEADLINE") or "",
                    "attachment_url": url,
                    "scrip_code": scrip_code,
                })

            if len(rows) < 50:  # last page in this window
                break
            page += 1

        # slide window backwards
        cursor_to = cursor_from - timedelta(days=1)

    logger.info("BSE %s: fetched %d announcements total", scrip_code, len(all_rows))
    return all_rows


def filter_by_category(
    announcements: list[dict[str, Any]],
    category_keys: list[str],
) -> list[dict[str, Any]]:
    """Keep announcements whose category or headline matches any of the
    given regex keys (e.g. 'award_of_orders', 'investor_presentation')."""
    patterns = [CATEGORY_PATTERNS[k] for k in category_keys if k in CATEGORY_PATTERNS]
    if not patterns:
        return []
    out = []
    for ann in announcements:
        haystack = (ann.get("category") or "") + " " + (ann.get("headline") or "")
        if any(p.search(haystack) for p in patterns):
            out.append(ann)
    return out


def date_to_period(date_str: str) -> str | None:
    """BSE date string ('2024-09-30T...') → 'Q3 2024' (calendar)."""
    from datetime import datetime
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.split("T")[0])
        quarter = (dt.month - 1) // 3 + 1
        return f"Q{quarter} {dt.year}"
    except (ValueError, AttributeError):
        return None
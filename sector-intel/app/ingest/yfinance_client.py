"""Quarterly financials for Indian companies via Yahoo Finance.

Returns 4-5 most recent quarters (yfinance limit). Computes QoQ/YoY
deltas and EBITDA margin in pandas. No LLM, no scraping.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Map yfinance row labels to our schema fields.
# yfinance row labels vary across companies — try a few candidates each.
_REVENUE_LABELS = ["Total Revenue", "Operating Revenue", "Revenue"]
_EBITDA_LABELS  = ["EBITDA", "Normalized EBITDA"]
_OPINC_LABELS   = ["Operating Income", "Operating Revenue"]
# When EBITDA isn't directly reported, EBITDA = Operating Income + D&A
_DA_LABELS      = ["Reconciled Depreciation", "Depreciation And Amortization"]


def _first_match(df: pd.DataFrame, labels: list[str]) -> pd.Series | None:
    for label in labels:
        if label in df.index:
            return df.loc[label]
    return None


def _date_to_period(ts: pd.Timestamp) -> str:
    """2024-09-30 → 'Q3 2024'. Calendar quarters."""
    quarter = (ts.month - 1) // 3 + 1
    return f"Q{quarter} {ts.year}"


def fetch_yfinance_quarterly(ticker: str) -> dict[str, dict[str, float]]:
    """Returns: {period: {revenue_inr_cr, ebitda_inr_cr, ebitda_margin_pct,
                          revenue_growth_qoq_pct, revenue_growth_yoy_pct}}.

    yfinance returns INR for .NS tickers. We convert to crores (÷ 1e7).
    """
    try:
        t = yf.Ticker(ticker)
        df = t.quarterly_income_stmt
    except Exception as exc:
        logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
        return {}

    if df is None or df.empty:
        logger.warning("yfinance: no quarterly data for %s", ticker)
        return {}

    # df columns are timestamps (one per quarter), sorted descending
    rev = _first_match(df, _REVENUE_LABELS)
    ebitda = _first_match(df, _EBITDA_LABELS)

    # If EBITDA missing, derive from operating income + D&A
    if ebitda is None:
        opinc = _first_match(df, _OPINC_LABELS)
        da = _first_match(df, _DA_LABELS)
        if opinc is not None and da is not None:
            ebitda = opinc.add(da, fill_value=0)

    if rev is None:
        logger.warning("yfinance: no revenue row for %s", ticker)
        return {}

    # Sort columns ascending by date so QoQ/YoY math works left-to-right
    rev_sorted = rev.sort_index()
    columns = list(rev_sorted.index)

    result: dict[str, dict[str, float]] = {}
    for i, col_ts in enumerate(columns):
        period = _date_to_period(col_ts)
        rev_val = rev_sorted.iloc[i]
        if pd.isna(rev_val):
            continue
        rev_cr = float(rev_val) / 1e7  # INR → crore

        entry: dict[str, float] = {"revenue_inr_cr": round(rev_cr, 2)}

        # EBITDA + margin
        if ebitda is not None:
            eb_val = ebitda.sort_index().iloc[i] if i < len(ebitda) else None
            if eb_val is not None and not pd.isna(eb_val):
                eb_cr = float(eb_val) / 1e7
                entry["ebitda_inr_cr"] = round(eb_cr, 2)
                if rev_cr > 0:
                    entry["ebitda_margin_pct"] = round((eb_cr / rev_cr) * 100, 2)

        # QoQ growth: vs previous quarter
        if i >= 1:
            prev = rev_sorted.iloc[i - 1]
            if not pd.isna(prev) and prev > 0:
                entry["revenue_growth_qoq_pct"] = round(((rev_val - prev) / prev) * 100, 2)

        # YoY growth: vs same quarter last year (4 quarters back)
        if i >= 4:
            prev_yr = rev_sorted.iloc[i - 4]
            if not pd.isna(prev_yr) and prev_yr > 0:
                entry["revenue_growth_yoy_pct"] = round(((rev_val - prev_yr) / prev_yr) * 100, 2)

        result[period] = entry

    logger.info("yfinance %s: %d quarters extracted", ticker, len(result))
    return result
"""Sector synthesis engine.

For one sector + one fiscal year:
  1. Pull every metric for all 4 quarters of that FY from the metrics table.
  2. Pivot into a per-metric dict:  {metric_name: {company: "<value> <unit>"}}
  3. Send ONE LLM call (LangChain + ChatAnthropic) asking for cross-company,
     cross-quarter synthesis (which metrics improving/deteriorating, common
     bets, structural vs cyclical).
  4. Write the result into the `synthesis` table.

Investing-lens deliberately left blank — to be filled later.

Run:
    python -m app.synthesize.synthesizer                       # default: fintech FY25
    python -m app.synthesize.synthesizer --sector indian_fintech --fy 25
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.config import settings
from app.db import Company, Metric, Sector, Synthesis, get_session

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Metric-name → unit suffix. Matches the field names in
# schemas.QuarterlyFintechMetrics. Anything not listed → no unit (raw number).
# ─────────────────────────────────────────────────────────────────────────────
METRIC_UNIT: dict[str, str] = {
    # INR crore (Fintech & Defence)
    "aum_inr_cr":               "Cr",
    "insurance_premium_inr_cr": "Cr",
    "saaum_inr_cr":             "Cr",
    "revenue_inr_cr":           "Cr",
    "order_book_crore":         "Cr",
    "ebitda_inr_cr":            "Cr",

    # Percent (Fintech & Defence)
    "gross_npa_pct":            "%",
    "net_npa_pct":              "%",
    "nim_pct":                  "%",
    "cost_of_funds_pct":        "%",
    "credit_cost_pct":          "%",
    "aum_growth_qoq_pct":       "%",
    "aum_growth_yoy_pct":       "%",
    "ebitda_margin_pct":        "%",
    "order_book_domestic_pct":  "%",
    "order_book_export_pct":    "%",
    "revenue_growth_qoq_pct":   "%",
    "revenue_growth_yoy_pct":   "%",

    # Millions (count)
    "customer_count_mn":        "mn",
    "active_users_mn":          "mn",
    "demat_accounts_mn":        "mn",
    "avc_count_mn":             "mn",
    "digital_transactions_count_mn": "mn",

    # USD Millions (Biotech)
    "revenue_product_usd_mn":       "$ mn",
    "revenue_royalty_usd_mn":       "$ mn",
    "revenue_collaboration_usd_mn": "$ mn",
    "revenue_total_usd_mn":         "$ mn",
    "cash_and_equiv_usd_mn":        "$ mn",
    "rd_expense_usd_mn":            "$ mn",
}


# Human-friendly labels for metrics in the prompt
METRIC_LABEL: dict[str, str] = {
    # Fintech
    "aum_inr_cr":                    "AUM / Loan Book (INR Cr)",
    "gross_npa_pct":                 "Gross NPA %",
    "net_npa_pct":                   "Net NPA %",
    "nim_pct":                       "Net Interest Margin %",
    "cost_of_funds_pct":             "Cost of Funds %",
    "credit_cost_pct":               "Credit Cost %",
    "aum_growth_qoq_pct":            "AUM Growth QoQ %",
    "aum_growth_yoy_pct":            "AUM Growth YoY %",
    "insurance_premium_inr_cr":      "Total Insurance Premium (INR Cr)",
    "saaum_inr_cr":                  "Serviced AUM (INR Cr)",
    "demat_accounts_mn":             "Demat Accounts (mn)",
    "avc_count_mn":                  "Active Value Counts (mn)",
    "customer_count_mn":             "Customer Count (mn)",
    "active_users_mn":               "Active Users (mn)",
    "digital_transactions_count_mn": "Digital Transactions (mn)",
    "revenue_inr_cr":                "Revenue (INR Cr)",

    # Defence
    "order_book_crore":              "Order Book (INR Cr)",
    "ebitda_inr_cr":                 "EBITDA (INR Cr)",
    "ebitda_margin_pct":             "EBITDA Margin %",
    "order_book_domestic_pct":       "Order Book Domestic %",
    "order_book_export_pct":         "Order Book Export %",
    "revenue_growth_qoq_pct":        "Revenue Growth QoQ %",
    "revenue_growth_yoy_pct":        "Revenue Growth YoY %",

    # Biotech
    "pipeline_phase1_count":         "Phase 1 Pipeline Count",
    "pipeline_phase2_count":         "Phase 2 Pipeline Count",
    "pipeline_phase3_count":         "Phase 3 Pipeline Count",
    "pipeline_nda_bla_count":        "NDA/BLA Pipeline Count",
    "revenue_product_usd_mn":        "Product Revenue ($ mn)",
    "revenue_royalty_usd_mn":        "Royalty Revenue ($ mn)",
    "revenue_collaboration_usd_mn":  "Collaboration Revenue ($ mn)",
    "revenue_total_usd_mn":          "Total Revenue ($ mn)",
    "cash_and_equiv_usd_mn":         "Cash & Equivalents ($ mn)",
    "rd_expense_usd_mn":             "R&D Expense ($ mn)",
    "runway_quarters":               "Runway (Quarters)",
}

# ─────────────────────────────────────────────────────────────────────────────
# Pull + pivot
# ─────────────────────────────────────────────────────────────────────────────
def _possible_period_formats(fy: int) -> list[str]:

    yy = f"{fy:02d}"
    yyyy = f"20{yy}"

    periods = []

    for q in range(1, 5):

        periods.extend([
            f"Q{q} FY{yy}",
            f"Q{q}FY{yy}",
            f"Q{q} {yyyy}",
            f"{yyyy} Q{q}",
            f"FY{yy}Q{q}",
            f"Q{q} FY{yyyy}",
        ])

    periods.extend([
        f"FY {yyyy}",
        f"FY{yyyy}",
        f"FY {yy}",
        f"FY{yy}",
    ])

    return list(set(periods))


def _fmt_value(metric_name: str, value: float) -> str:
    unit = METRIC_UNIT.get(metric_name, "")
    # Integer-ish numbers get comma formatting, decimals get 2 dp
    if abs(value) >= 1000 and value == int(value):
        formatted = f"{int(value):,}"
    elif abs(value) >= 100:
        formatted = f"{value:,.1f}"
    else:
        formatted = f"{value:.2f}"
    return f"{formatted} {unit}".strip()

def aggregate_metrics_for_sector_fy(
    sector: Sector,
    fy: int,
) -> dict[str, dict[str, dict[str, str]]]:
    """Pivot DB rows into:
        {metric_name: {period: {company_ticker: '<value> <unit>'}}}

    Missing (company, period, metric) cells are simply absent — never crashes.
    """
    quarters = _possible_period_formats(fy)
    out: dict[str, dict[str, dict[str, str]]] = defaultdict(
        lambda: defaultdict(dict)
    )

    with get_session() as s:
        companies = (
            s.query(Company).filter(Company.sector == sector).all()
        )
        if not companies:
            logger.warning("No companies for sector=%s", sector)
            return {}
        company_ids = [c.id for c in companies]
        id_to_ticker = {c.id: c.ticker for c in companies}

        rows = (
            s.query(
                Metric.company_id, Metric.period, Metric.metric_name,
                Metric.metric_value, Metric.metric_value_text,
            )
            .filter(
                Metric.company_id.in_(company_ids),
                Metric.period.in_(quarters),
                Metric.metric_value.isnot(None),  # only numeric metrics
            )
            .all()
        )

    for cid, period, mname, mval, _mtext in rows:
        if mval is None:
            continue
        try:
            formatted = _fmt_value(mname, float(mval))
        except (TypeError, ValueError):
            continue
        ticker = id_to_ticker.get(cid)
        if not ticker:
            continue
        out[mname][period][ticker] = formatted

    return {k: dict(v) for k, v in out.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Format the pivot dict into a compact text block the LLM can reason over
# ─────────────────────────────────────────────────────────────────────────────
def _format_pivot_for_prompt(
    pivot: dict[str, dict[str, dict[str, str]]],
    quarters: list[str],
) -> str:
    if not pivot:
        return "(no metrics found)"
    blocks: list[str] = []
    for mname in sorted(pivot.keys()):
        label = METRIC_LABEL.get(mname, mname)
        per_q = pivot[mname]
        blocks.append(f"\n## {label}  [{mname}]")
        for q in quarters:
            company_vals = per_q.get(q, {})
            if not company_vals:
                continue
            row = ", ".join(
                f"{ticker}={val}"
                for ticker, val in sorted(company_vals.items())
            )
            blocks.append(f"  {q}: {row}")
    return "\n".join(blocks) if blocks else "(no metrics found)"


# ─────────────────────────────────────────────────────────────────────────────
# LangChain LLM call
# ─────────────────────────────────────────────────────────────────────────────
SYNTHESIS_SYSTEM = """You are a sector analyst writing a cross-company synthesis.

You receive structured KPI data: one block per metric, showing values across
companies and quarters. Your job is to identify SPECIFIC patterns — not generic
commentary.

Output requirements:
  1. Cite companies by ticker and quote actual numbers (e.g. "BAJFINANCE.NS AUM
     grew from 354,191 Cr in Q1 to 416,750 Cr in Q4").
  2. Cover three angles:
       a. Which metrics are consistently improving / deteriorating across the sector.
       b. What product or operational bets multiple companies are making simultaneously.
       c. What looks structurally new (durable trend) versus cyclical (one-quarter spike).
  3. If a metric only has data for 1-2 companies, note that gap rather than
     overgeneralizing.
  4. 4-7 short paragraphs total. No bullet lists. No filler.
"""

SYNTHESIS_USER_TEMPLATE = """Sector: {sector_label}
Fiscal Year: FY{fy:02d}  (quarters: {quarters})

Below is the per-metric, per-quarter, per-company snapshot:
{pivot_block}

Write the synthesis now."""


def _build_chain():
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    llm = ChatAnthropic(
        model=getattr(settings, "synthesis_model", None) or settings.extraction_model,
        api_key=settings.anthropic_api_key,
        max_tokens=2048,
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYNTHESIS_SYSTEM),
        ("user",   SYNTHESIS_USER_TEMPLATE),
    ])
    return prompt | llm | StrOutputParser()


# ─────────────────────────────────────────────────────────────────────────────
# Persist
# ─────────────────────────────────────────────────────────────────────────────
def _persist_synthesis(sector: Sector, period: str, text: str) -> int:
    """Upsert into the synthesis table. Returns the row id."""
    with get_session() as s:
        existing = (
            s.query(Synthesis)
            .filter_by(sector=sector, period=period)
            .order_by(Synthesis.generated_at.desc())
            .first()
        )
        if existing is not None:
            existing.synthesis_text = text
            # investing_lens left as-is (or empty if creating fresh)
            if existing.investing_lens_text is None:
                existing.investing_lens_text = ""
            s.flush()
            return existing.id
        row = Synthesis(
            sector=sector,
            period=period,
            synthesis_text=text,
            investing_lens_text="",  # filled in later
        )
        s.add(row)
        s.flush()
        return row.id


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def run_synthesis(sector: Sector, fy: int) -> dict[str, Any]:
    quarters = _possible_period_formats(fy)
    pivot = aggregate_metrics_for_sector_fy(sector, fy)
    pivot_block = _format_pivot_for_prompt(pivot, quarters)

    n_metrics = len(pivot)
    n_cells = sum(
        len(company_vals)
        for per_q in pivot.values()
        for company_vals in per_q.values()
    )
    logger.info(
        "Synthesis sector=%s FY%02d  metrics=%d  cells=%d",
        sector.value, fy, n_metrics, n_cells,
    )

    if n_metrics == 0:
        msg = f"No metrics found for sector={sector.value} FY{fy:02d}."
        logger.warning(msg)
        return {"status": "no_data", "message": msg}

    chain = _build_chain()
    sector_label = sector.value.replace("_", " ").title()
    try:
        text = chain.invoke({
            "sector_label": sector_label,
            "fy":           fy,
            "quarters":     ", ".join(quarters),
            "pivot_block":  pivot_block,
        })
    except Exception as exc:
        logger.exception("Synthesis LLM call failed: %s", exc)
        return {"status": "llm_failed", "error": str(exc)}

    period_label = f"FY{fy:02d}"
    row_id = _persist_synthesis(sector, period_label, text)
    logger.info(
        "Synthesis stored: sector=%s period=%s synthesis_id=%d  (%d chars)",
        sector.value, period_label, row_id, len(text),
    )
    return {
        "status": "ok",
        "sector": sector.value,
        "period": period_label,
        "synthesis_id": row_id,
        "metrics_used": n_metrics,
        "data_cells": n_cells,
        "synthesis_text": text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _sector_from_str(s: str) -> Sector:
    try:
        return Sector(s)
    except ValueError:
        raise SystemExit(  # noqa: B904
            f"Unknown sector '{s}'. Valid: "
            f"{', '.join(s.value for s in Sector)}"
        )

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--sector",
        type=str,
        default="all",
        help=(
            "Sector name OR 'all' to run every sector "
            "(default: all)"
        ),
    )

    parser.add_argument(
        "--fy",
        type=int,
        default=25,
        help="Fiscal year as two-digit int (default: 25 for FY25)",
    )

    args = parser.parse_args()

    # ============================================================
    # RUN ALL SECTORS
    # ============================================================

    if args.sector.lower() == "all":

        sectors = list(Sector)

        print("\n" + "=" * 80)
        print(f"RUNNING SYNTHESIS FOR ALL SECTORS | FY{args.fy:02d}")
        print("=" * 80)

        all_results = []

        for sector in sectors:

            print("\n" + "-" * 80)
            print(f"Processing sector: {sector.value}")
            print("-" * 80)

            try:

                result = run_synthesis(
                    sector=sector,
                    fy=args.fy,
                )

                all_results.append(result)

                print(f"Status: {result.get('status')}")

                if result.get("status") == "ok":
                    print(
                        f"Metrics: {result.get('metrics_used')} | "
                        f"Cells: {result.get('data_cells')}"
                    )

            except Exception as exc:

                logger.exception(
                    "Sector failed: %s",
                    sector.value,
                )

                print(f"FAILED: {sector.value}")
                print(str(exc))

        # ========================================================
        # FINAL SUMMARY
        # ========================================================

        print("\n" + "=" * 80)
        print("FINAL SUMMARY")
        print("=" * 80)

        for result in all_results:

            print(
                f"{result.get('sector', 'unknown'):<30} "
                f"{result.get('status')}"
            )

    # ============================================================
    # RUN SINGLE SECTOR
    # ============================================================

    else:

        sector = _sector_from_str(args.sector)

        result = run_synthesis(
            sector=sector,
            fy=args.fy,
        )

        print("\n" + "=" * 70)
        print(f"Synthesis result: {result.get('status')}")
        print("=" * 70)

        for k, v in result.items():

            if k == "synthesis_text":
                continue

            print(f"  {k:<18} {v}")

        if "synthesis_text" in result:

            print("\n--- SYNTHESIS TEXT ---\n")
            print(result["synthesis_text"])

"""Sector Intelligence Monitor — Streamlit UI.

Reads from sector_intel.db only. Never calls LLM at request time.

Run locally:
    cd sector-intel
    streamlit run app/ui/app.py
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import (
    Company,
    Document,
    Metric,
    RefreshLog,
    Sector,
    Synthesis,
    get_session,
)
from app.scheduler import run_refresh_now, start_scheduler

logging.basicConfig(level=logging.INFO)

st.set_page_config(
    page_title="Sector Intelligence Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Start scheduler once per process
if "_scheduler_started" not in st.session_state:
    try:
        start_scheduler()
    except Exception as exc:
        st.warning(f"Scheduler did not start: {exc}")
    st.session_state["_scheduler_started"] = True


METRIC_LABELS = {
    "aum_inr_cr": "AUM (INR Cr)",
    "gross_npa_pct": "Gross NPA %",
    "net_npa_pct": "Net NPA %",
    "nim_pct": "NIM %",
    "cost_of_funds_pct": "Cost of Funds %",
    "credit_cost_pct": "Credit Cost %",
    "aum_growth_yoy_pct": "AUM Growth YoY %",
    "insurance_premium_inr_cr": "Insurance Premium (INR Cr)",
    "saaum_inr_cr": "Serviced AUM (INR Cr)",
    "demat_accounts_mn": "Demat Accounts (mn)",
    "avc_count_mn": "Active Value Counts (mn)",
    "customer_count_mn": "Customer Count (mn)",
    "active_users_mn": "Active Users (mn)",
    "digital_transactions_count_mn": "Digital Transactions (mn)",
    "revenue_inr_cr": "Revenue (INR Cr)",
}

def PERIOD_ORDER_KEY(p):  # noqa: N802
    """Properly extracts the Year and Quarter for chronological sorting."""
    year = 0
    q = 0
    
    # Extract Year (e.g., 2023 or FY24)
    year_match = re.search(r'(\d{4})', str(p))
    if year_match:
        year = int(year_match.group(1))
    else:
        fy_match = re.search(r'FY(\d{2})', str(p))
        if fy_match:
            year = 2000 + int(fy_match.group(1))
            
    # Extract Quarter (e.g., Q1)
    q_match = re.search(r'Q(\d)', str(p))
    if q_match:
        q = int(q_match.group(1))
        
    return (year, q)


# ─────────────────────────────────────────────────────────────────────────────
# Cached DB readers
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def get_companies(sector_value: str) -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(Company.id, Company.name, Company.ticker, Company.exchange)
            .filter(Company.sector == Sector(sector_value))
            .order_by(Company.name).all()
        )
    return pd.DataFrame(rows, columns=["id", "name", "ticker", "exchange"])


@st.cache_data(ttl=60)
def get_metrics(company_id: int) -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(
                Metric.period, Metric.metric_name,
                Metric.metric_value, Metric.metric_value_text,
                Metric.source_document_id,
            )
            .filter(Metric.company_id == company_id)
            .all()
        )
    df = pd.DataFrame(rows, columns=[
        "period", "metric_name", "metric_value", "metric_value_text", "doc_id"
    ])
    if df.empty:
        return df
    df = df[df["period"] != "ALL"]
    
    # Apply correct chronological sorting
    df["period_key"] = df["period"].apply(PERIOD_ORDER_KEY)
    df = df.sort_values("period_key").drop(columns=["period_key"])
    return df


@st.cache_data(ttl=60)
def get_synthesis(sector_value: str) -> dict | None:
    with get_session() as s:
        row = (
            s.query(Synthesis)
            .filter(Synthesis.sector == Sector(sector_value))
            .order_by(Synthesis.generated_at.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "period": row.period,
            "synthesis_text": row.synthesis_text,
            "investing_lens_text": row.investing_lens_text or "",
            "generated_at": row.generated_at,
        }


@st.cache_data(ttl=60)
def get_last_refresh(sector_value: str) -> datetime | None:
    with get_session() as s:
        row = (
            s.query(RefreshLog)
            .filter(RefreshLog.sector == Sector(sector_value))
            .order_by(RefreshLog.run_started_at.desc())
            .first()
        )
        return row.run_started_at if row else None


@st.cache_data(ttl=60)
def get_documents(company_id: int) -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(
                Document.id, Document.period, Document.document_type,
                Document.source_url, Document.parse_status,
            )
            .filter(Document.company_id == company_id)
            .all()
        )
    df = pd.DataFrame(rows, columns=[
        "id", "period", "document_type", "source_url", "parse_status"
    ])
    if not df.empty:
        df["period_key"] = df["period"].apply(PERIOD_ORDER_KEY)
        df = df.sort_values("period_key", ascending=False).drop(columns=["period_key"])
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
st.sidebar.title("Sector Intelligence")

sector_options = {
    "Indian Fintech": "indian_fintech",
    "Indian Defence": "indian_defence",
    "US Biotech":     "us_biotech",
}
sector_label = st.sidebar.selectbox("Sector", list(sector_options.keys()))
sector_value = sector_options[sector_label]

last_refresh = get_last_refresh(sector_value)
if last_refresh:
    st.sidebar.caption(f"Last refresh: {last_refresh:%Y-%m-%d %H:%M UTC}")
else:
    st.sidebar.caption("Last refresh: never")

if st.sidebar.button("Refresh now", type="primary"):
    with st.spinner("Running refresh — may take a couple of minutes…"):
        try:
            result = run_refresh_now()
            st.sidebar.success("Refresh complete.")
            st.sidebar.json(result)
            st.cache_data.clear()
        except Exception as exc:
            st.sidebar.error(f"Refresh failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
st.title(f"{sector_label}")

companies = get_companies(sector_value)

if companies.empty:
    st.warning(f"No companies seeded for {sector_label}.")
    st.stop()


tab_co, tab_synth, tab_lens, tab_docs = st.tabs(
    ["Company Trends", "Sector Synthesis", "Investing Lens", "Documents"]
)


# ── Tab 1: Per-company line charts ──────────────────────────────────────────
with tab_co:
    company_choice = st.selectbox(
        "Company",
        companies["ticker"] + " — " + companies["name"],
    )
    ticker = company_choice.split(" — ")[0]
    company_row = companies[companies["ticker"] == ticker].iloc[0]
    company_id = int(company_row["id"])

    metrics_df = get_metrics(company_id)
    if metrics_df.empty:
        st.info("No metrics extracted yet for this company.")
    else:
        # Separate numeric vs text metrics dynamically
        metrics_df["numeric_value"] = pd.to_numeric(metrics_df["metric_value"], errors="coerce")
        
        numeric_df = metrics_df[metrics_df["numeric_value"].notna()].copy()
        
        # Text dataframe: catches both un-parseable 'metric_value' strings AND 'metric_value_text'
        text_df = metrics_df[
            (metrics_df["numeric_value"].isna() & metrics_df["metric_value"].notna()) | 
            (metrics_df["metric_value"].isna() & metrics_df["metric_value_text"].notna())
        ].copy()

        available = sorted(numeric_df["metric_name"].unique())
        
        if not available:
            st.info("No numeric metrics extracted yet.")
        else:
            chosen = st.multiselect(
                "Quantitative Metrics",
                available,
                default=available[:4],
                format_func=lambda m: METRIC_LABELS.get(m, m),
            )
            for metric_name in chosen:
                sub = numeric_df[numeric_df["metric_name"] == metric_name]
                if sub.empty:
                    continue
                latest = sub.iloc[-1]
                prior = sub.iloc[-2] if len(sub) >= 2 else None
                delta = None
                if prior is not None:
                    delta = float(latest["numeric_value"]) - float(prior["numeric_value"])

                c1, c2 = st.columns([1, 3])
                with c1:
                    st.metric(
                        METRIC_LABELS.get(metric_name, metric_name),
                        value=f"{latest['numeric_value']:,.2f}",
                        delta=(f"{delta:+,.2f} vs {prior['period']}"
                               if delta is not None else None),
                    )
                with c2:
                    fig = px.line(
                        sub, x="period", y="numeric_value",
                        markers=True,
                        title=METRIC_LABELS.get(metric_name, metric_name),
                    )
                    
                    # Force Plotly to respect the chronological dataframe order
                    fig.update_xaxes(categoryorder="trace")
                    
                    fig.update_layout(
                        height=260, margin=dict(l=10, r=10, t=40, b=10),
                        showlegend=False, xaxis_title="", yaxis_title="",
                    )
                    st.plotly_chart(fig, use_container_width=True)

        # ── Text Metrics Section ──
        if not text_df.empty:
            st.divider()
            st.subheader("📝 Qualitative & Text Metrics")
            st.caption("Metrics containing non-numeric data, analysis, or qualitative notes.")
            
            # Helper to grab the correct text value whether it's in metric_value or metric_value_text
            def get_text_val(row):
                if pd.notna(row["metric_value_text"]) and str(row["metric_value_text"]).strip():
                    return row["metric_value_text"]
                return row["metric_value"]

            text_df["Value"] = text_df.apply(get_text_val, axis=1)
            
            # Format and display
            display_df = text_df[["period", "metric_name", "Value"]].rename(columns={
                "period": "Period",
                "metric_name": "Metric Name",
            })
            
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True
            )


# ── Tab 2: Sector synthesis ─────────────────────────────────────────────────
with tab_synth:
    syn = get_synthesis(sector_value)
    if syn is None:
        st.info("No synthesis generated yet. Trigger 'Refresh now' from the sidebar.")
    else:
        st.caption(f"Period: {syn['period']}  ·  generated {syn['generated_at']:%Y-%m-%d %H:%M UTC}")
        st.markdown(syn["synthesis_text"])


# ── Tab 3: Investing lens (placeholder until implemented) ───────────────────
with tab_lens:
    syn = get_synthesis(sector_value)
    if syn and syn.get("investing_lens_text"):
        st.caption(f"Period: {syn['period']}")
        st.markdown(syn["investing_lens_text"])
    else:
        st.info("Investing lens not yet generated for this sector.")


# ── Tab 4: Source documents ─────────────────────────────────────────────────
with tab_docs:
    co_pick = st.selectbox(
        "Company for documents",
        companies["ticker"] + " — " + companies["name"],
        key="docs_company",
    )
    ticker = co_pick.split(" — ")[0]
    company_id = int(companies[companies["ticker"] == ticker].iloc[0]["id"])
    docs = get_documents(company_id)
    if docs.empty:
        st.info("No documents indexed.")
    else:
        st.dataframe(
            docs[["period", "document_type", "parse_status", "source_url"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "source_url": st.column_config.LinkColumn("Source"),
            },
        )
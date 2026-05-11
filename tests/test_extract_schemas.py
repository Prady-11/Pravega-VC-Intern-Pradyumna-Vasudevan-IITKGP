"""Smoke tests for the Pydantic extraction schemas.

These guard the validation layer. If they pass, we have confidence that:
  - hallucinated fields from the LLM are rejected (not silently stored)
  - impossible values (negative AUM, NPA > 100, runway > 40q) are rejected
  - cross-field invariants (net NPA ≤ gross NPA, dom% + exp% ≤ 105) hold
  - the sector → schema dispatch is complete
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.extract.schemas import (
    SECTOR_SCHEMA,
    IndianDefenceMetrics,
    IndianFintechMetrics,
    USBiotechMetrics,
)

COMMON: dict = {
    "company_name": "Acme Corp",
    "period": "Q3FY24",
    "source_doc_url": "https://example.com/x.pdf",
}


# --- Identity / common -----------------------------------------------

def test_period_is_normalized_uppercase_single_spaced():
    m = IndianFintechMetrics(**{**COMMON, "period": "  q3   fy24  "})
    assert m.period == "Q3 FY24"


def test_extra_fields_forbidden():
    """LLM hallucinations of invented metric names must be rejected."""
    with pytest.raises(ValidationError):
        IndianFintechMetrics(**COMMON, made_up_metric=42)


# --- Indian Fintech --------------------------------------------------

def test_fintech_happy_path():
    m = IndianFintechMetrics(
        **COMMON,
        aum_crore=350000,
        gross_npa_pct=1.2,
        net_npa_pct=0.4,
        nim_pct=10.5,
    )
    assert m.aum_crore == 350000
    assert m.gross_npa_pct == 1.2


def test_fintech_rejects_negative_aum():
    with pytest.raises(ValidationError):
        IndianFintechMetrics(**COMMON, aum_crore=-1)


def test_fintech_rejects_npa_above_100():
    with pytest.raises(ValidationError):
        IndianFintechMetrics(**COMMON, gross_npa_pct=120)


def test_fintech_net_npa_cannot_exceed_gross():
    with pytest.raises(ValidationError):
        IndianFintechMetrics(**COMMON, gross_npa_pct=1.0, net_npa_pct=2.0)


def test_fintech_all_metrics_optional():
    """A doc that only mentions notes should still validate."""
    m = IndianFintechMetrics(**COMMON, notes="No quantitative disclosures this period.")
    assert m.aum_crore is None


# --- Indian Defence --------------------------------------------------

def test_defence_order_mix_must_sum_close_to_100():
    # 60 + 50 = 110 → reject
    with pytest.raises(ValidationError):
        IndianDefenceMetrics(
            **COMMON, order_book_domestic_pct=60, order_book_export_pct=50
        )


def test_defence_order_mix_with_slack_passes():
    # 60 + 42 = 102 within 105 slack → pass
    m = IndianDefenceMetrics(
        **COMMON, order_book_domestic_pct=60, order_book_export_pct=42
    )
    assert m.order_book_export_pct == 42


def test_defence_new_order_wins_typed():
    m = IndianDefenceMetrics(
        **COMMON,
        new_order_wins=[
            {"value_crore": 1200, "geography": "domestic", "product_category": "radar"},
            {"value_crore": 300, "geography": "export"},
        ],
    )
    assert len(m.new_order_wins) == 2
    assert m.new_order_wins[0].geography == "domestic"


def test_defence_rejects_invalid_geography():
    with pytest.raises(ValidationError):
        IndianDefenceMetrics(
            **COMMON, new_order_wins=[{"value_crore": 100, "geography": "lunar"}]
        )


# --- US Biotech ------------------------------------------------------

def test_biotech_pipeline_counts_and_runway():
    m = USBiotechMetrics(
        **COMMON,
        pipeline_phase1_count=5,
        pipeline_phase2_count=3,
        runway_quarters=8,
        cash_and_equiv_usd_mn=1450.5,
    )
    assert m.pipeline_phase1_count == 5
    assert m.runway_quarters == 8


def test_biotech_rejects_negative_runway():
    with pytest.raises(ValidationError):
        USBiotechMetrics(**COMMON, runway_quarters=-2)


def test_biotech_trial_readout_phase_enum():
    """The LLM cannot invent a 'Phase 5'."""
    with pytest.raises(ValidationError):
        USBiotechMetrics(
            **COMMON,
            trial_readouts=[{"indication": "NSCLC", "phase": "Phase 5"}],
        )


def test_biotech_trial_readout_outcome_enum():
    with pytest.raises(ValidationError):
        USBiotechMetrics(
            **COMMON,
            trial_readouts=[
                {"indication": "NSCLC", "phase": "Phase 2", "outcome": "great"}
            ],
        )


def test_biotech_ai_ml_callouts_capped():
    with pytest.raises(ValidationError):
        USBiotechMetrics(**COMMON, ai_ml_callouts=[f"callout {i}" for i in range(25)])


# --- Dispatch --------------------------------------------------------

def test_sector_schema_dispatch_complete():
    assert set(SECTOR_SCHEMA.keys()) == {"indian_fintech", "indian_defence", "us_biotech"}
    assert SECTOR_SCHEMA["indian_fintech"] is IndianFintechMetrics
    assert SECTOR_SCHEMA["indian_defence"] is IndianDefenceMetrics
    assert SECTOR_SCHEMA["us_biotech"] is USBiotechMetrics
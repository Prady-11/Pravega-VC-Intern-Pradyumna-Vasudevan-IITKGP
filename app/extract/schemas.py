"""Pydantic v2 schemas for typed metric extraction.

These models are the contract between the LLM and the database. The LLM is
forced to produce output matching one of these schemas (via Anthropic's tool-use
mode); anything that fails validation is retried once and then flagged.

Design rules:
  * `extra="forbid"` so hallucinated fields are rejected, not silently stored.
  * Every numeric field has a sane range — NPA cannot exceed 100%, runway cannot
    be negative, EBITDA margin can be negative but not -200%, etc.
  * Every metric is `Optional`. A single concall transcript rarely contains every
    metric; the extractor returns whatever it found, and the orchestrator merges
    across documents per (company, period).
  * `_Base` carries identity fields the LLM must echo back (company, period, source
    URL) so we can attach extracted metrics to the right document with no guessing.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---- Reusable constrained types ---------------------------------

# Growth percentages can legitimately exceed 100% YoY for early-stage companies.
Pct = Annotated[float, Field(ge=-100, le=1000)]
NonNegFloat = Annotated[float, Field(ge=0)]
# Pipeline counts in biotech rarely exceed double digits, but allow headroom.
SmallInt = Annotated[int, Field(ge=0, le=10000)]
PeriodStr = Annotated[str, Field(min_length=4, max_length=20)]


class _Base(BaseModel):
    """Identity fields every extracted record carries."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company_name: str = Field(min_length=1, max_length=200)
    period: PeriodStr
    source_doc_url: str = Field(min_length=8)

    @field_validator("period")
    @classmethod
    def normalize_period(cls, v: str) -> str:
        # Accept "Q3 FY24", "Q3FY24", "Q3 2024" — store uppercased, single-spaced.
        return " ".join(v.strip().upper().split())


# ---- Indian Fintech ---------------------------------------------

class IndianFintechMetrics(_Base):
    """AUM, NPA, NIM, digital, cost-of-funds. Most fintech filings have a subset."""

    aum_crore: NonNegFloat | None = Field(
        default=None, description="Loan book / AUM in INR crore"
    )
    aum_growth_qoq_pct: Pct | None = None
    aum_growth_yoy_pct: Pct | None = None

    gross_npa_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    net_npa_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    credit_cost_pct_aum: Annotated[float, Field(ge=0, le=50)] | None = None
    nim_pct: Annotated[float, Field(ge=-10, le=50)] | None = None
    cost_of_funds_pct: Annotated[float, Field(ge=0, le=30)] | None = None

    digital_txn_volume: NonNegFloat | None = Field(
        default=None, description="Periodic transaction count or value (see digital_txn_unit)"
    )
    digital_txn_unit: Literal["count", "inr_crore", "inr_lakh", "usd_mn"] | None = None
    active_users_mn: NonNegFloat | None = Field(
        default=None, description="Monthly active users in millions"
    )

    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("net_npa_pct")
    @classmethod
    def net_npa_not_above_gross(cls, v: float | None, info) -> float | None:
        gross = info.data.get("gross_npa_pct")
        if v is not None and gross is not None and v > gross + 0.01:
            raise ValueError(f"net_npa_pct ({v}) cannot exceed gross_npa_pct ({gross})")
        return v

class QuarterlyFintechMetrics(BaseModel):
    """One company-period entry. All fields optional — different sub-sectors
    populate different subsets.
    """
    model_config = ConfigDict(extra="forbid")

    period: str = Field(description="Quarter label as it appears on the slide, e.g. 'Q2 FY26' or 'Q3 2024'")

    # Lending / NBFC / card issuer
    aum_inr_cr: float | None = Field(
        default=None,
        description="Assets Under Management (AUM) / loan book / receivables / advances. Number in INR crore. Example: if slide says 'AUM ₹462,261 Cr', return 462261.",
    )
    gross_npa_pct: float | None = Field(
        default=None,
        description="Gross NPA percentage. Slides label this as 'GNPA' or 'Gross NPA'. Return just the number — if slide says 'GNPA 1.24%' return 1.24.",
    )
    net_npa_pct: float | None = Field(
        default=None,
        description="Net NPA percentage. Slides label this as 'NNPA' or 'Net NPA'. If slide says 'Net NPA 0.60%' or 'Net NPA at 60 bps', return 0.60.",
    )
    nim_pct: float | None = Field(
        default=None,
        description="Net Interest Margin percentage. Slides label this 'NIM'. Return just the percentage number.",
    )
    cost_of_funds_pct: float | None = Field(
        default=None,
        description="Cost of Funds (COF) as percentage. If slide says 'cost of funds was 7.52%', return 7.52.",
    )
    credit_cost_pct: float | None = Field(
        default=None,
        description="Credit cost as percentage of AUM. Convert basis points to percent: '50 bps' = 0.50.",
    )
    aum_growth_qoq_pct: float | None = Field(default=None, description="AUM growth quarter-on-quarter, percent.")
    aum_growth_yoy_pct: float | None = Field(default=None, description="AUM growth year-on-year, percent. If slide says 'AUM up 24% YoY', return 24.")

    # Insurance broker (PB Fintech)
    insurance_premium_inr_cr: float | None = Field(
        default=None,
        description="Total insurance premium (premium booked / GWP) in INR crore. Used by insurance brokers like PB Fintech.",
    )

    # Asset management (CAMS)
    saaum_inr_cr: float | None = Field(
        default=None,
        description="Serviced Assets Under Management (SaAUM) in INR crore. Used by CAMS.",
    )

    # Depository (CDSL)
    demat_accounts_mn: float | None = Field(
        default=None,
        description="Number of demat accounts in millions. Used by CDSL.",
    )
    avc_count_mn: float | None = Field(
        default=None,
        description="Active Value Counts (AVC) in millions. Used by CDSL.",
    )

    # Common
    active_users_mn: float | None = Field(default=None, description="Active users / MAU / DAU in millions.")
    customer_count_mn: float | None = Field(default=None, description="Customer count or customer franchise in millions. Bajaj calls this 'customer franchise'.")
    digital_transactions_count_mn: float | None = Field(default=None, description="Digital transaction count in millions (UPI, payments, etc).")

    revenue_inr_cr: float | None = Field(default=None, description="Total revenue / total income in INR crore. yfinance value is authoritative when provided.")

    notes: str | None = Field(
        default=None,
        max_length=400,
        description="ONLY use for context that doesn't fit any structured field above. Do NOT put numeric KPI values here — those belong in their dedicated fields.",
    )


class IndianFintechTimeSeriesMetrics(BaseModel):
    """One LLM call per company; quarters list covers all periods found."""
    model_config = ConfigDict(extra="forbid")

    company_name: str
    quarters: list[QuarterlyFintechMetrics]
    trend_notes: str | None = None


# ---- Indian Defence ---------------------------------------------

class NewOrderWin(BaseModel):
    """A single new-order announcement within the period."""

    model_config = ConfigDict(extra="forbid")

    value_crore: NonNegFloat | None = None
    geography: Literal["domestic", "export", "mixed"] | None = None
    product_category: str | None = Field(default=None, max_length=200)


class IndianDefenceMetrics(_Base):
    order_book_crore: NonNegFloat | None = None
    order_book_domestic_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    order_book_export_pct: Annotated[float, Field(ge=0, le=100)] | None = None

    revenue_growth_qoq_pct: Pct | None = None
    revenue_growth_yoy_pct: Pct | None = None
    ebitda_margin_pct: Annotated[float, Field(ge=-50, le=80)] | None = None
    rd_pct_revenue: Annotated[float, Field(ge=0, le=50)] | None = None

    new_order_wins: list[NewOrderWin] = Field(default_factory=list, max_length=50)

    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("order_book_export_pct")
    @classmethod
    def domestic_plus_export_sane(cls, v: float | None, info) -> float | None:
        """Domestic + export should sum to ~100%. Allow 5% slack for rounding/'other'."""
        dom = info.data.get("order_book_domestic_pct")
        if v is not None and dom is not None and (v + dom) > 105:
            raise ValueError(
                f"order_book_domestic_pct ({dom}) + export_pct ({v}) > 105"
            )
        return v

class QuarterlyDefenceMetrics(BaseModel):
    """One company-period entry for Indian Defence."""
    model_config = ConfigDict(extra="forbid")

    period: str
    revenue_inr_cr: float | None = None
    ebitda_inr_cr: float | None = None
    revenue_growth_qoq_pct: float | None = None
    revenue_growth_yoy_pct: float | None = None
    ebitda_margin_pct: float | None = None
    order_book_crore: float | None = None
    order_book_domestic_pct: float | None = None
    order_book_export_pct: float | None = None
    new_order_wins: list[NewOrderWin] = []
    notes: str | None = None


class IndianDefenceTimeSeriesMetrics(BaseModel):
    """Output from one LLM call per company."""
    model_config = ConfigDict(extra="forbid")

    company_name: str
    quarters: list[QuarterlyDefenceMetrics]
    trend_notes: str | None = None


# ---- US Biotech -------------------------------------------------

TrialPhase = Literal[
    "Preclinical", "Phase 1", "Phase 2", "Phase 3", "NDA", "BLA", "Filed", "Approved"
]
TrialOutcome = Literal["positive", "mixed", "negative", "ongoing", "delayed"]


class TrialReadout(BaseModel):
    """One clinical trial readout / pipeline event."""

    model_config = ConfigDict(extra="forbid")

    indication: str = Field(min_length=1, max_length=200)
    phase: TrialPhase
    outcome: TrialOutcome | None = None
    notes: str | None = Field(default=None, max_length=500)


class USBiotechMetrics(_Base):
    pipeline_phase1_count: SmallInt | None = None
    pipeline_phase2_count: SmallInt | None = None
    pipeline_phase3_count: SmallInt | None = None
    pipeline_nda_bla_count: SmallInt | None = None

    cash_and_equiv_usd_mn: NonNegFloat | None = None
    runway_quarters: Annotated[float, Field(ge=0, le=40)] | None = None

    revenue_product_usd_mn: NonNegFloat | None = None
    revenue_royalty_usd_mn: NonNegFloat | None = None
    revenue_collaboration_usd_mn: NonNegFloat | None = None

    trial_readouts: list[TrialReadout] = Field(default_factory=list, max_length=50)
    ai_ml_callouts: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Direct quotes/paraphrases of AI/ML investments or partnerships",
    )

    notes: str | None = Field(default=None, max_length=2000)


class QuarterlyBiotechMetrics(BaseModel):
    """One company-period entry. All fields optional."""
    model_config = ConfigDict(extra="forbid")

    period: str
    pipeline_phase1_count: int | None = None
    pipeline_phase2_count: int | None = None
    pipeline_phase3_count: int | None = None
    pipeline_nda_bla_count: int | None = None
    cash_and_equiv_usd_mn: float | None = None
    runway_quarters: float | None = None
    revenue_product_usd_mn: float | None = None
    revenue_collaboration_usd_mn: float | None = None
    revenue_total_usd_mn: float | None = None
    rd_expense_usd_mn: float | None = None
    trial_readouts: list[TrialReadout] = []
    ai_ml_callouts: list[str] = []
    notes: str | None = None


class USBiotechTimeSeriesMetrics(BaseModel):
    """Output from one LLM call covering all quarters of one company."""
    model_config = ConfigDict(extra="forbid")

    company_name: str
    quarters: list[QuarterlyBiotechMetrics]
    trend_notes: str | None = None

# ---- Sector → schema dispatch -----------------------------------

SECTOR_SCHEMA: dict[str, type[_Base]] = {
    "indian_fintech": IndianFintechMetrics,
    "indian_defence": IndianDefenceMetrics,
    "us_biotech": USBiotechMetrics,
}


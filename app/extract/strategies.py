"""Per-company extraction strategies for Indian Fintech.

Built from manual deck research. Each entry tells the extractor exactly which
modalities (text / images / both) and which schema fields to target.
"""
from __future__ import annotations

from typing import Literal, TypedDict


class Strategy(TypedDict):
    mode: Literal["text", "image", "hybrid"]
    text_anchors: list[str]
    image_anchors: list[str]
    max_pages: int
    target_fields: list[str]


STRATEGIES: dict[str, Strategy] = {
    # Bajaj: text-heavy KFI block + 2 deck images for customer/digital txn
    "BAJFINANCE.NS": {
        "mode": "hybrid",
        "text_anchors": [
            "Lead financial indicators",
            "Key financial indicators",
            "Executive summary",
        ],
        "image_anchors": [
            "Customer franchise",
            "Digital transactions",
        ],
        "max_pages": 2,
        "target_fields": [
            "aum_inr_cr", "gross_npa_pct", "net_npa_pct", "nim_pct",
            "cost_of_funds_pct", "credit_cost_pct",
            "customer_count_mn", "digital_transactions_count_mn",
            "aum_growth_yoy_pct",
        ],
    },

    # SBI Card: AUM/GNPA/NNPA in Key Highlights; NIM/COF/credit cost in P&L
    "SBICARD.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Key Highlights",
            "Profit & Loss Statement",
            "Profit and Loss Statement",
            "Receivables",
        ],
        "max_pages": 3,
        "target_fields": [
            "aum_inr_cr", "gross_npa_pct", "net_npa_pct",
            "nim_pct", "cost_of_funds_pct", "credit_cost_pct",
        ],
    },

    # PB Fintech: insurance premium, active users, digital txn
    "POLICYBZR.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Operating Metrics",
            "Insurance Premium",
            "Total Insurance Premium",
            "Active Transacting Consumers",
        ],
        "max_pages": 2,
        "target_fields": [
            "insurance_premium_inr_cr", "active_users_mn",
            "digital_transactions_count_mn",
        ],
    },

    # CAMS: SaAUM, MyCAMS customers, transaction volume
    "CAMS.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Operational Metrics",
            "Operational Highlights",
            "Quarterly Highlights",
            "SaAUM",
        ],
        "max_pages": 2,
        "target_fields": [
            "saaum_inr_cr", "customer_count_mn",
            "digital_transactions_count_mn",
        ],
    },

    # CDSL: demat accounts, AVC
    "CDSL.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Operational Metrics",
            "Operational Highlights",
            "Demat Accounts",
            "Active Value Counts",
        ],
        "max_pages": 2,
        "target_fields": [
            "demat_accounts_mn", "avc_count_mn",
        ],
    },

    # Zaggle: customer/user count
    "ZAGGLE.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Key Metrics",
            "Operational Highlights",
            "Customer Count",
            "User Count",
        ],
        "max_pages": 2,
        "target_fields": [
            "customer_count_mn", "active_users_mn",
        ],
    },

    # CreditAccess Grameen
    "CREDITACC.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Key Financial Metrics",
            "Key Metrics",
            "Financial Highlights",
        ],
        "max_pages": 2,
        "target_fields": [
            "aum_inr_cr", "gross_npa_pct", "net_npa_pct",
            "nim_pct", "credit_cost_pct", "cost_of_funds_pct",
            "aum_growth_yoy_pct",
        ],
    },

    # Five Star
    "FIVESTAR.NS": {
        "mode": "image",
        "text_anchors": [],
        "image_anchors": [
            "Key Financial Metrics",
            "Key Metrics",
            "Financial Highlights",
        ],
        "max_pages": 2,
        "target_fields": [
            "aum_inr_cr", "gross_npa_pct", "net_npa_pct",
            "nim_pct", "credit_cost_pct",
            "aum_growth_yoy_pct",
        ],
    },
}


DEFAULT_STRATEGY: Strategy = {
    "mode": "image",
    "text_anchors": [],
    "image_anchors": [
        "Key Metrics", "Key Highlights", "Executive Summary",
        "Operational Metrics", "Key Financial Metrics",
    ],
    "max_pages": 2,
    "target_fields": [
        "aum_inr_cr", "gross_npa_pct", "net_npa_pct", "nim_pct",
        "cost_of_funds_pct", "credit_cost_pct",
        "customer_count_mn", "active_users_mn",
        "digital_transactions_count_mn",
        "insurance_premium_inr_cr", "saaum_inr_cr",
        "demat_accounts_mn", "avc_count_mn",
        "revenue_inr_cr",
    ],
}


def get_strategy(ticker: str) -> Strategy:
    return STRATEGIES.get(ticker, DEFAULT_STRATEGY)
"""Seed the `companies` table with the fixed list from the assignment.

Idempotent: re-running will update names if changed but never duplicate rows.
The (ticker, exchange) unique constraint guarantees this.
"""
from __future__ import annotations

from app.db import Company, Sector, get_session

# (name, sector, ticker, exchange) — fixed list, NOT to be made dynamic per spec.
COMPANIES: list[tuple[str, Sector, str, str]] = [
    # --- Indian Fintech (9) ---------------------------------------
    ("Bajaj Finance",                Sector.INDIAN_FINTECH, "BAJFINANCE", "NSE"),
    ("SBI Cards",                    Sector.INDIAN_FINTECH, "SBICARD",    "NSE"),
    ("Paytm",                        Sector.INDIAN_FINTECH, "PAYTM",      "NSE"),
    ("PB Fintech",                   Sector.INDIAN_FINTECH, "POLICYBZR",  "NSE"),
    ("CAMS",                         Sector.INDIAN_FINTECH, "CAMS",       "NSE"),
    ("CDSL",                         Sector.INDIAN_FINTECH, "CDSL",       "NSE"),
    ("Zaggle",                       Sector.INDIAN_FINTECH, "ZAGGLE",     "NSE"),
    ("CreditAccess Grameen",         Sector.INDIAN_FINTECH, "CREDITACC",  "NSE"),
    ("Five Star Business Finance",   Sector.INDIAN_FINTECH, "FIVESTAR",   "NSE"),

    # --- Indian Defence (8) ---------------------------------------
    ("HAL",                          Sector.INDIAN_DEFENCE, "HAL",        "NSE"),
    ("BEL",                          Sector.INDIAN_DEFENCE, "BEL",        "NSE"),
    ("MTAR Technologies",            Sector.INDIAN_DEFENCE, "MTARTECH",   "NSE"),
    ("Paras Defence",                Sector.INDIAN_DEFENCE, "PARAS",      "NSE"),
    ("Astra Microwave",              Sector.INDIAN_DEFENCE, "ASTRAMICRO", "NSE"),
    ("Data Patterns",                Sector.INDIAN_DEFENCE, "DATAPATTNS", "NSE"),
    ("Zen Technologies",             Sector.INDIAN_DEFENCE, "ZENTEC",     "NSE"),
    ("Bharat Forge",                 Sector.INDIAN_DEFENCE, "BHARATFORG", "NSE"),

    # --- US Biotech (8) -------------------------------------------
    ("Moderna",                      Sector.US_BIOTECH,     "MRNA",       "NASDAQ"),
    ("Regeneron",                    Sector.US_BIOTECH,     "REGN",       "NASDAQ"),
    ("Vertex Pharmaceuticals",       Sector.US_BIOTECH,     "VRTX",       "NASDAQ"),
    ("Biogen",                       Sector.US_BIOTECH,     "BIIB",       "NASDAQ"),
    ("Illumina",                     Sector.US_BIOTECH,     "ILMN",       "NASDAQ"),
    ("10x Genomics",                 Sector.US_BIOTECH,     "TXG",        "NASDAQ"),
    ("Pacific Biosciences",          Sector.US_BIOTECH,     "PACB",       "NASDAQ"),
    ("Recursion Pharmaceuticals",    Sector.US_BIOTECH,     "RXRX",       "NASDAQ"),
]


def seed() -> None:
    inserted = 0
    updated = 0
    with get_session() as s:
        for name, sector, ticker, exchange in COMPANIES:
            existing = (
                s.query(Company).filter_by(ticker=ticker, exchange=exchange).one_or_none()
            )
            if existing is None:
                s.add(Company(name=name, sector=sector, ticker=ticker, exchange=exchange))
                inserted += 1
            else:
                if existing.name != name or existing.sector != sector:
                    existing.name = name
                    existing.sector = sector
                    updated += 1
    print(f"Seed complete: {inserted} inserted, {updated} updated, {len(COMPANIES)} total expected.")


if __name__ == "__main__":
    seed()

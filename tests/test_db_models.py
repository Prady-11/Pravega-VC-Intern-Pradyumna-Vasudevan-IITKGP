"""Smoke tests for the DB models.

Verifies:
  * schema creates without errors
  * the 5 required tables exist with the names the assignment dictates
  * unique constraints actually enforce
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, Document, DocumentType, ParseStatus, Sector


@pytest.fixture()
def session():
    """Fresh in-memory SQLite per test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_required_tables_exist():
    expected = {"companies", "documents", "metrics", "synthesis", "refresh_log"}
    actual = set(Base.metadata.tables.keys())
    assert expected.issubset(actual), f"Missing: {expected - actual}"


def test_company_unique_ticker_exchange(session):
    session.add(Company(name="HAL", sector=Sector.INDIAN_DEFENCE, ticker="HAL", exchange="NSE"))
    session.commit()

    session.add(Company(name="Hal Dup", sector=Sector.INDIAN_DEFENCE, ticker="HAL", exchange="NSE"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_document_unique_source_url(session):
    c = Company(name="Moderna", sector=Sector.US_BIOTECH, ticker="MRNA", exchange="NASDAQ")
    session.add(c)
    session.flush()

    d1 = Document(
        company_id=c.id,
        source_url="https://sec.gov/x.htm",
        document_type=DocumentType.SEC_10Q,
        period="Q3 2024",
        parse_status=ParseStatus.PENDING,
    )
    session.add(d1)
    session.commit()

    d2 = Document(
        company_id=c.id,
        source_url="https://sec.gov/x.htm",  # duplicate
        document_type=DocumentType.SEC_10Q,
        period="Q3 2024",
        parse_status=ParseStatus.PENDING,
    )
    session.add(d2)
    with pytest.raises(IntegrityError):
        session.commit()


def test_parse_status_default_is_pending(session):
    c = Company(name="X", sector=Sector.US_BIOTECH, ticker="XXX", exchange="NASDAQ")
    session.add(c)
    session.flush()
    d = Document(
        company_id=c.id,
        source_url="https://example.com/a",
        document_type=DocumentType.SEC_10Q,
        period="Q1 2024",
    )
    session.add(d)
    session.commit()
    assert d.parse_status == ParseStatus.PENDING
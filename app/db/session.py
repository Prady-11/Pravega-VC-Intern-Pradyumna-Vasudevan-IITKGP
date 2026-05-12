from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# Ensure persistent disk directories exist on first boot

_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    db_path = settings.database_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)


engine = create_engine(
    settings.database_url,
    # SQLite needs this when accessed across threads (Streamlit + APScheduler).
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a session. Commit on clean exit, rollback on exception, always close."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


print("DB URL:", settings.database_url)
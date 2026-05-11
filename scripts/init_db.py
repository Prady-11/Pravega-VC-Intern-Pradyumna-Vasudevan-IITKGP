"""Create the SQLite schema from SQLAlchemy models.

For a 3-day project with a fixed schema, this beats dragging in Alembic.
A production version would add Alembic migrations; noted in README.
"""
from __future__ import annotations

from app.config import settings
from app.db import Base, engine


def main() -> None:
    settings.ensure_dirs()
    Base.metadata.create_all(engine)
    print(f"Schema initialized at {settings.database_url}")
    print("Tables created:")
    for table_name in sorted(Base.metadata.tables.keys()):
        print(f"  - {table_name}")


if __name__ == "__main__":
    main()
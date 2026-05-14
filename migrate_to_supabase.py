"""
One-time migration: copy local SQLite data → Supabase PostgreSQL.

Usage:
    export SUPABASE_URL="postgresql://postgres.xxx:password@aws-0-xxx.pooler.supabase.com:6543/postgres"
    python migrate_to_supabase.py
"""

import os
import sqlite3

SQLITE_PATH = "data/sector_intel.db"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")

if not SUPABASE_URL:
    raise SystemExit("Set SUPABASE_URL env var to your Supabase connection string")

from sqlalchemy import create_engine, text

print("Connecting to Supabase...")
pg = create_engine(SUPABASE_URL)

print("Connecting to SQLite...")
sqlite = sqlite3.connect(SQLITE_PATH)
sqlite.row_factory = sqlite3.Row


def migrate_table(table, conflict_cols):
    rows = sqlite.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  {table}: 0 rows, skipping")
        return

    cols = list(rows[0].keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)

    sql = text(
        f"INSERT INTO {table} ({col_list}) "
        f"OVERRIDING SYSTEM VALUE "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_cols}) DO NOTHING"
    )

    errors = 0
    with pg.begin() as conn:
        conn.execute(sql, dict(rows[0]))  # test connection
    
    with pg.begin() as conn:
        for row in rows:
            try:
                conn.execute(sql, dict(row))
            except Exception as e:
                print(f"    row error: {e}")
                errors += 1

    suffix = f" ({errors} errors)" if errors else ""
    print(f"  {table}: {len(rows)} rows migrated ✓{suffix}")


# Step 1: wipe all tables in reverse FK order
print("\nClearing existing data...")
with pg.begin() as conn:
    conn.execute(text("DELETE FROM metrics"))
    conn.execute(text("DELETE FROM documents"))
    conn.execute(text("DELETE FROM synthesis"))
    conn.execute(text("DELETE FROM refresh_log"))
    conn.execute(text("DELETE FROM companies"))
print("  Cleared ✓")

# Step 2: insert in correct FK order
print("\nMigrating tables...")
migrate_table("companies", "id")
migrate_table("documents", "id")
migrate_table("metrics", "id")
migrate_table("synthesis", "id")
migrate_table("refresh_log", "id")

# Step 3: fix sequences
print("\nFixing sequences...")
with pg.begin() as conn:
    for table in ["companies", "documents", "metrics", "synthesis", "refresh_log"]:
        try:
            conn.execute(text(
                f"SELECT setval('{table}_id_seq', (SELECT MAX(id) FROM {table}))"
            ))
            print(f"  {table}_id_seq fixed ✓")
        except Exception as e:
            print(f"  {table}_id_seq: {e}")

sqlite.close()
print("\nMigration complete!")
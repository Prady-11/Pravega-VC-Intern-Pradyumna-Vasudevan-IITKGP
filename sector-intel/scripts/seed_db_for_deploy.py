"""One-shot: copy local sector_intel.db into the Render persistent disk.

Run ONCE after first deploy:
    render ssh sector-intel
    cd /var/data
    # then upload local data/sector_intel.db here (rsync / scp / `render disk push`)
"""
import shutil
from pathlib import Path

LOCAL = Path("data/sector_intel.db")
TARGET = Path("/var/data/sector_intel.db")

if not LOCAL.exists():
    raise SystemExit(f"Missing local DB at {LOCAL}")
TARGET.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(LOCAL, TARGET)
print(f"Copied {LOCAL} -> {TARGET}")

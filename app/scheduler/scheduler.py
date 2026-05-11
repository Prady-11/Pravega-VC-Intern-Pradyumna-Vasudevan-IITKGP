"""Weekly refresh scheduler + /refresh trigger.

In-process APScheduler. Runs Mon 03:00 IST every week. Each run:
  - Re-registers any new files in data/raw/ as PARSED Documents.
  - Re-runs the extractor (per-company, idempotent).
  - Re-runs synthesis for FY25 (or whichever FY is current).
  - Logs to refresh_log.

Triggerable on-demand from Streamlit via run_refresh_now().
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import RefreshLog, Sector, get_session
from app.extract.extractor_fintech import run_extractor_indian_fintech
from app.synthesize.synthesizer import run_synthesis

logger = logging.getLogger(__name__)

CURRENT_FY = 25
SECTORS_TO_REFRESH = [Sector.INDIAN_FINTECH]

_scheduler: BackgroundScheduler | None = None


def _do_refresh(sector: Sector, triggered_by: str = "schedule") -> dict:
    run_id = None
    errors: list[dict] = []
    summary = {
        "sector": sector.value,
        "extractor": None,
        "synthesis": None,
    }

    with get_session() as s:
        log_row = RefreshLog(
            sector=sector,
            triggered_by=triggered_by,
            documents_checked=0,
            new_documents_found=0,
        )
        s.add(log_row)
        s.flush()
        run_id = log_row.id

    try:
        if sector == Sector.INDIAN_FINTECH:
            ext = run_extractor_indian_fintech()
        else:
            ext = {"skipped": f"no extractor wired for {sector.value}"}
        summary["extractor"] = ext
    except Exception as exc:
        errors.append({"step": "extractor", "error": str(exc),
                       "trace": traceback.format_exc()[-500:]})
        logger.exception("Extractor failed: %s", exc)

    try:
        syn = run_synthesis(sector, CURRENT_FY)
        summary["synthesis"] = syn
    except Exception as exc:
        errors.append({"step": "synthesis", "error": str(exc),
                       "trace": traceback.format_exc()[-500:]})
        logger.exception("Synthesis failed: %s", exc)

    with get_session() as s:
        log_row = s.get(RefreshLog, run_id)
        if log_row is not None:
            log_row.run_finished_at = datetime.utcnow()
            log_row.documents_checked = (
                (summary["extractor"] or {}).get("companies", 0)
                if isinstance(summary["extractor"], dict) else 0
            )
            log_row.new_documents_found = (
                (summary["extractor"] or {}).get("extracted", 0)
                if isinstance(summary["extractor"], dict) else 0
            )
            log_row.errors = errors if errors else None

    summary["run_id"] = run_id
    summary["errors"] = errors
    return summary


def run_refresh_now() -> list[dict]:
    out = []
    for sector in SECTORS_TO_REFRESH:
        logger.info("Manual refresh: %s", sector.value)
        out.append(_do_refresh(sector, triggered_by="manual"))
    return out


def _scheduled_job():
    logger.info("Scheduled weekly refresh starting")
    for sector in SECTORS_TO_REFRESH:
        try:
            _do_refresh(sector, triggered_by="schedule")
        except Exception as exc:
            logger.exception("Scheduled refresh crashed for %s: %s",
                             sector.value, exc)
    logger.info("Scheduled weekly refresh done")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    sched = BackgroundScheduler(timezone="Asia/Kolkata")
    sched.add_job(
        _scheduled_job,
        trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="weekly_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    logger.info("Scheduler started — next run Mon 03:00 IST")
    return sched


def stop_scheduler():
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s")
    print("Triggering immediate refresh…")
    result = run_refresh_now()
    print("\nDone:", result)
"""Extraction Orchestrator — runs all three sector extractors.

Sectors:
  - US Biotech      (extractor.py)        → XBRL + SEC MD&A sections
  - Indian Defence  (extractor_defence.py) → yfinance + press releases + order book snippets
  - Indian Fintech  (extractor_fintech.py) → yfinance + multi-modal investor presentations

Execution model:
  - Sectors run SEQUENTIALLY by default (safe for shared DB sessions).
  - Pass --parallel to run all three sectors concurrently via ThreadPoolExecutor.
  - Each extractor already batches all companies in one LLM call per company,
    so parallelism here is at sector level, not company level.

Retry policy:
  - Each sector runner is retried up to MAX_SECTOR_RETRIES times on hard failure.
  - Individual company failures inside a sector are already handled per-extractor
    (they flip ParseStatus.EXTRACTION_FAILED and continue); the orchestrator
    surfaces those in the final report.

Exit codes:
  0 — all sectors completed (individual company failures don't block success)
  1 — one or more sectors failed after all retries
  2 — fatal config error (missing API key, etc.)

Usage:
    python orchestrator.py                        # sequential, all sectors
    python orchestrator.py --parallel             # concurrent sectors
    python orchestrator.py --sector biotech       # single sector
    python orchestrator.py --sector defence
    python orchestrator.py --sector fintech
    python orchestrator.py --dry-run              # validate config, don't extract
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

# ── App imports ──────────────────────────────────────────────────────────────
from app.config import settings
from app.extract.extractor import run_extractor
from app.extract.extractor_defence import run_extractor_indian_defence
from app.extract.extractor_fintech import run_extractor_indian_fintech

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)-35s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("orchestrator")

# ── Constants ────────────────────────────────────────────────────────────────
MAX_SECTOR_RETRIES = 2          # how many times to retry a fully-failed sector
RETRY_BACKOFF_SECONDS = 5       # wait between sector-level retries


# ── Per-sector result container ───────────────────────────────────────────────
@dataclass
class SectorResult:
    name: str
    summary: dict = field(default_factory=dict)   # returned by the run_* function
    elapsed_s: float = 0.0
    attempts: int = 0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    @property
    def companies(self) -> int:
        return self.summary.get("companies", 0)

    @property
    def extracted(self) -> int:
        return self.summary.get("extracted", 0)

    @property
    def failed(self) -> int:
        return self.summary.get("failed", 0)

    @property
    def skipped(self) -> int:
        return self.summary.get("skipped", 0)


# ── Sector registry ───────────────────────────────────────────────────────────
SECTOR_RUNNERS: dict[str, Callable[[], dict]] = {
    "biotech":  run_extractor,
    "defence":  run_extractor_indian_defence,
    "fintech":  run_extractor_indian_fintech,
}


# ── Core runner with retry ────────────────────────────────────────────────────
def _run_sector_with_retry(name: str, runner: Callable[[], dict]) -> SectorResult:
    result = SectorResult(name=name)
    for attempt in range(1, MAX_SECTOR_RETRIES + 1):
        result.attempts = attempt
        t0 = time.perf_counter()
        try:
            logger.info("▶  [%s] Starting (attempt %d/%d)", name, attempt, MAX_SECTOR_RETRIES)
            summary = runner()
            result.elapsed_s = time.perf_counter() - t0
            result.summary = summary
            result.error = None
            logger.info(
                "✓  [%s] Done in %.1fs — %s",
                name, result.elapsed_s, summary,
            )
            return result
        except Exception as exc:
            result.elapsed_s = time.perf_counter() - t0
            result.error = str(exc)
            logger.exception(
                "✗  [%s] Attempt %d failed after %.1fs: %s",
                name, attempt, result.elapsed_s, exc,
            )
            if attempt < MAX_SECTOR_RETRIES:
                logger.info(
                    "   [%s] Retrying in %ds…", name, RETRY_BACKOFF_SECONDS,
                )
                time.sleep(RETRY_BACKOFF_SECONDS)

    logger.error("✗  [%s] All %d attempts exhausted.", name, MAX_SECTOR_RETRIES)
    return result


# ── Parallel runner ───────────────────────────────────────────────────────────
def _run_parallel(runners: dict[str, Callable]) -> list[SectorResult]:
    """Run all sectors concurrently. Each sector still serialises its own DB ops."""
    results: list[SectorResult] = []
    with ThreadPoolExecutor(max_workers=len(runners), thread_name_prefix="sector") as pool:
        futures = {
            pool.submit(_run_sector_with_retry, name, fn): name
            for name, fn in runners.items()
        }
        for fut in as_completed(futures):
            results.append(fut.result())
    # Sort for deterministic report output
    results.sort(key=lambda r: r.name)
    return results


# ── Sequential runner ─────────────────────────────────────────────────────────
def _run_sequential(runners: dict[str, Callable]) -> list[SectorResult]:
    return [
        _run_sector_with_retry(name, fn)
        for name, fn in runners.items()
    ]


# ── Summary printer ───────────────────────────────────────────────────────────
def _print_report(results: list[SectorResult], total_elapsed: float) -> None:
    divider = "─" * 72
    print(f"\n{divider}")
    print("  EXTRACTION ORCHESTRATOR — FINAL REPORT")
    print(divider)

    all_ok = True
    totals = {"companies": 0, "extracted": 0, "failed": 0, "skipped": 0}

    for r in results:
        status = "✓ OK   " if r.succeeded else "✗ FAIL "
        if not r.succeeded:
            all_ok = False
        print(
            f"  {status}  {r.name:<12}"
            f"  companies={r.companies:<4}"
            f"  extracted={r.extracted:<4}"
            f"  failed={r.failed:<4}"
            f"  skipped={r.skipped:<4}"
            f"  {r.elapsed_s:>6.1f}s"
            f"  attempts={r.attempts}",
        )
        if r.error:
            print(f"           └─ error: {r.error[:120]}")
        for k in totals:
            totals[k] += r.summary.get(k, 0)

    print(divider)
    print(
        f"  TOTALS  "
        f"  companies={totals['companies']:<4}"
        f"  extracted={totals['extracted']:<4}"
        f"  failed={totals['failed']:<4}"
        f"  skipped={totals['skipped']:<4}"
        f"  wall={total_elapsed:.1f}s",
    )
    print(divider)

    if totals["failed"]:
        pct = totals["failed"] / max(totals["companies"], 1) * 100
        logger.warning(
            "%.1f%% of companies had extraction failures (%d/%d). "
            "Check ParseStatus.EXTRACTION_FAILED in the DB.",
            pct, totals["failed"], totals["companies"],
        )

    if not all_ok:
        logger.error(
            "One or more SECTORS failed entirely after %d retries. "
            "Check logs above for root cause.",
            MAX_SECTOR_RETRIES,
        )


# ── Config validation ─────────────────────────────────────────────────────────
def _validate_config() -> list[str]:
    """Return a list of config problems (empty = OK)."""
    problems: list[str] = []
    if not getattr(settings, "anthropic_api_key", None):
        problems.append("ANTHROPIC_API_KEY is not set in .env / settings.")
    if not getattr(settings, "extraction_model", None):
        problems.append("extraction_model is not set in settings.")
    if not getattr(settings, "sec_user_agent", None):
        logger.warning(
            "sec_user_agent is not set — XBRL fetch will be skipped for US Biotech."
        )
    return problems


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run sector extraction pipelines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--sector",
        choices=list(SECTOR_RUNNERS.keys()),
        default=None,
        help="Run only this sector (default: all).",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help="Run sectors concurrently (default: sequential).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config only, do not extract.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity.",
    )
    return p.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    args = _parse_args()
    logging.getLogger().setLevel(args.log_level)

    # Config check
    problems = _validate_config()
    if problems:
        for p in problems:
            logger.critical("CONFIG ERROR: %s", p)
        return 2

    if args.dry_run:
        logger.info("Dry run — config OK, no extraction performed.")
        return 0

    # Select sectors
    runners = (
        {args.sector: SECTOR_RUNNERS[args.sector]}
        if args.sector
        else SECTOR_RUNNERS
    )

    logger.info(
        "Orchestrator starting: sectors=%s  mode=%s  max_retries=%d",
        list(runners.keys()),
        "parallel" if args.parallel else "sequential",
        MAX_SECTOR_RETRIES,
    )

    wall_t0 = time.perf_counter()
    results = (
        _run_parallel(runners)
        if args.parallel
        else _run_sequential(runners)
    )
    total_elapsed = time.perf_counter() - wall_t0

    _print_report(results, total_elapsed)

    # Exit 1 if any sector hard-failed
    return 0 if all(r.succeeded for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
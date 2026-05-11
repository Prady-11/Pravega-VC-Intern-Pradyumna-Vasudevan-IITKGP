"""Tier 3: Assembles tracks A/B/C into a token-budgeted fact sheet."""
from __future__ import annotations

from app.db import Sector
from app.extract.anchor_miner import MinerOutput
from app.extract.trials_client import TrialSummary
from app.extract.xbrl_client import XbrlFacts

# Hard cap on fact sheet size. ~2400 chars ≈ ~600 tokens.
MAX_CHARS = 2400


def _truncate(lines: list[str], max_chars: int) -> list[str]:
    """Drop trailing lines until the joined length is ≤ max_chars."""
    while lines and sum(len(s) + 1 for s in lines) > max_chars:
        lines.pop()
    return lines


def assemble_fact_sheet(
    company_name: str,
    period: str,
    sector: Sector,
    xbrl: XbrlFacts,
    miner: MinerOutput,
    trials: list[TrialSummary],
) -> str:
    lines: list[str] = [
        "=== FILING FACT SHEET ===",
        f"Company: {company_name}",
        f"Period:  {period}",
        f"Sector:  {sector}",
        "",
    ]

    if xbrl.provenance:
        lines.append("-- FINANCIAL FACTS [XBRL — AUDITOR-CERTIFIED] --")
        lines.extend(xbrl.provenance)
        lines.append("NOTE: XBRL values are authoritative. Match them exactly.")
        lines.append("")

    if miner.snippets:
        lines.append("-- DOCUMENT SNIPPETS --")
        lines.extend(miner.snippets[:8])
        lines.append("")

    if miner.phase_hint:
        lines.append(f"-- PHASE HINT --\n{miner.phase_hint}\n")

    if trials:
        lines.append(f"-- CLINICAL TRIALS [{len(trials)} total] --")
        by_phase: dict[str, list[str]] = {}
        for t in trials:
            by_phase.setdefault(t.phase, []).append(f"{t.indication} ({t.status})")
        for phase in sorted(by_phase):
            lines.append(f"[{phase}] " + " // ".join(by_phase[phase][:5]))
        lines.append("")

    lines.append("=== END ===")
    return "\n".join(_truncate(lines, MAX_CHARS))
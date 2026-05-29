"""Shared ``ReportBundler`` for the PRD-B divergence harness.

Given a list of divergence rows + report metadata, produce the four-file
bundle ``day-N.{exec|replay}.{md,json,parquet,hashes.json}``. Encapsulates
manifest hashing, pass/fail-gate evaluation (gate = zero GATING-severity
divergences), and bundle layout. Reused unchanged across Layer A and
Layer B — the layer's category set and tolerance set arrive as inputs, not
hard-coded (PRD-B Implementation Decisions → ReportBundler).

Mirrors the SHA-256 manifest + per-day bundle prior art in
``app.engine.live.reconcile`` (``file_sha256`` / ``write_day_report``).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from app.engine.live.divergence.common import Severity
from app.engine.live.reconcile import file_sha256


class DivergenceLike(Protocol):
    """The structural contract both layers' divergence rows satisfy."""

    category: object  # a StrEnum member
    severity: Severity
    magnitude: float
    applied_tolerance: float


@dataclass(frozen=True)
class ReportMetadata:
    """Report header recorded verbatim into the JSON bundle."""

    run_id: str
    strategy_instance_id: str
    trading_day: int
    session_window_ms: tuple[int, int]
    layer: str  # "exec" | "replay"
    tolerances: dict


@dataclass(frozen=True)
class BundlePaths:
    markdown: Path
    json: Path
    parquet: Path
    hashes: Path


def write_report_bundle(
    divergences: Sequence[DivergenceLike],
    *,
    metadata: ReportMetadata,
    reports_dir: Path,
) -> BundlePaths:
    """Write the four-file bundle and return its paths."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"day-{metadata.trading_day}.{metadata.layer}"
    paths = BundlePaths(
        markdown=reports_dir / f"{stem}.md",
        json=reports_dir / f"{stem}.json",
        parquet=reports_dir / f"{stem}.parquet",
        hashes=reports_dir / f"{stem}.hashes.json",
    )

    gating = [d for d in divergences if d.severity is Severity.GATING]
    counts_by_category: dict[str, int] = {}
    for d in divergences:
        key = str(d.category)
        counts_by_category[key] = counts_by_category.get(key, 0) + 1
    summary = {
        "passed": not gating,
        "gating_breach_count": len(gating),
        "gating_categories": sorted({str(d.category) for d in gating}),
        "counts_by_category": counts_by_category,
        "tolerances": metadata.tolerances,
    }

    _write_parquet(divergences, paths.parquet)
    paths.json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    paths.markdown.write_text(_render_markdown(metadata, summary), encoding="utf-8")
    paths.hashes.write_text(
        json.dumps(_manifest(paths), indent=2, sort_keys=True), encoding="utf-8"
    )
    return paths


def _write_parquet(divergences: Sequence[DivergenceLike], path: Path) -> None:
    rows = []
    for d in divergences:
        row = {k: v for k, v in dataclasses.asdict(d).items()}
        row["category"] = str(d.category)
        row["severity"] = str(d.severity)
        rows.append(row)
    pd.DataFrame(rows).to_parquet(path, index=False)


def _manifest(paths: BundlePaths) -> dict:
    return {
        "markdown_sha256": file_sha256(paths.markdown) if paths.markdown.exists() else None,
        "json_sha256": file_sha256(paths.json) if paths.json.exists() else None,
        "parquet_sha256": file_sha256(paths.parquet),
    }


def _render_markdown(metadata: ReportMetadata, summary: dict) -> str:
    status = "PASS" if summary["passed"] else "FAIL"
    lines = [
        f"# Divergence report — day-{metadata.trading_day} ({metadata.layer}) — {status}",
        "",
        f"- run_id: {metadata.run_id}",
        f"- strategy_instance_id: {metadata.strategy_instance_id}",
        f"- session_window_ms: {metadata.session_window_ms}",
        f"- gating breaches: {summary['gating_breach_count']}",
        f"- gating categories: {summary['gating_categories']}",
    ]
    return "\n".join(lines) + "\n"

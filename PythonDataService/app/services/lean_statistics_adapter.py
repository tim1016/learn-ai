"""Adapt normalized LEAN artifacts and prove native-statistics parity.

This module owns the boundary between the lossless ``lean-native-r1`` artifact
and persistence-facing response models.  It intentionally keeps LEAN's native
values separate from the independently reproduced statistics receipt.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.engine.results.lean_statistics import (
    LEAN_STATISTICS_SOURCE_COMMIT,
    format_lean_statistics_summary,
    reproduce_lean_total_performance,
)

logger = logging.getLogger(__name__)


@dataclass
class NormalizedResult:
    """Schema-versioned view of normalized/result.json."""

    parser_version: str
    order_events: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    statistics: dict[str, Any]
    runtime_statistics: dict[str, Any]
    total_performance: dict[str, Any]
    orders: dict[str, Any]
    analysis: list[dict[str, Any]]
    profit_loss: list[dict[str, Any]]
    full_charts: dict[str, Any]
    algorithm_configuration: dict[str, Any]
    artifacts: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> NormalizedResult:
        data = json.loads(path.read_text())
        return cls(
            parser_version=data.get("parser_version", "unknown"),
            order_events=data.get("order_events") or [],
            equity_curve=data.get("equity_curve") or [],
            statistics=data.get("statistics") or {},
            runtime_statistics=data.get("runtime_statistics") or {},
            total_performance=data.get("total_performance") or {},
            orders=data.get("orders") or {},
            analysis=data.get("analysis") or [],
            profit_loss=data.get("profit_loss") or [],
            full_charts=data.get("full_charts") or {},
            algorithm_configuration=data.get("algorithm_configuration") or {},
            artifacts=data.get("artifacts") or {},
        )

    def lean_reproduction_input(self) -> dict[str, Any]:
        return {
            "total_performance": self.total_performance,
            "profit_loss": self.profit_loss,
            "full_charts": self.full_charts,
            "algorithm_configuration": self.algorithm_configuration,
        }


def _parse_pct(raw: Any) -> float:
    """Parse a LEAN percent string; return zero for absent legacy values."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    value = str(raw).strip()
    if not value:
        return 0.0
    try:
        if value.endswith("%"):
            return float(value[:-1].replace(",", "").strip()) / 100.0
        return float(value.replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_dollar(raw: Any) -> float:
    """Parse a LEAN currency string; return zero for absent legacy values."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    value = str(raw).strip()
    if not value:
        return 0.0
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_ratio(raw: Any) -> float:
    """Parse a LEAN ratio string; return zero for absent legacy values."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    value = str(raw).strip().replace(",", "")
    if not value:
        return 0.0
    if value.endswith("%"):
        value = value[:-1].strip()
    try:
        return float(value)
    except ValueError:
        return 0.0


def _parse_int(raw: Any) -> int:
    """Parse an integer-shaped LEAN value; return zero for absent values."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    value = str(raw).strip().replace(",", "")
    if not value:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def _format_duration_seconds(total_seconds: float) -> str:
    """Format an average trade duration as ``H:MM:SS`` like LEAN."""
    if total_seconds <= 0:
        return "0:00:00"
    total = round(total_seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _lean_native_parity_envelope(
    normalized: NormalizedResult,
    workspace_path: Path,
) -> dict[str, Any]:
    """Compare an independent Python reproduction to every native LEAN value."""
    interest_rate_csv = (
        workspace_path
        / "workspace"
        / "data"
        / "alternative"
        / "interest-rate"
        / "usa"
        / "interest-rate.csv"
    )
    base: dict[str, Any] = {
        "contract_id": f"lean-native-statistics-{LEAN_STATISTICS_SOURCE_COMMIT}",
        "source_commit": LEAN_STATISTICS_SOURCE_COMMIT,
        "absolute_tolerance": 0.0000500001,
        "native_metric_count": 0,
        "formatted_metric_count": 0,
        "divergences": [],
    }
    if not interest_rate_csv.exists():
        return base | {
            "status": "unavailable",
            "reason": "lean_interest_rate_fixture_missing",
            "lean_native": None,
            "python_lean_compatible": None,
        }
    try:
        reproduced = reproduce_lean_total_performance(
            normalized.lean_reproduction_input(),
            interest_rate_csv=interest_rate_csv,
        )
        native = {
            section: normalized.total_performance.get(section, {})
            for section in ("portfolioStatistics", "tradeStatistics")
        }
        divergences: list[dict[str, Any]] = []
        metric_count = 0
        for section, calculated_values in reproduced.items():
            native_values = native.get(section)
            if not isinstance(native_values, Mapping):
                divergences.append({"section": section, "metric": "*", "reason": "native_section_missing"})
                continue
            for metric, calculated in calculated_values.items():
                metric_count += 1
                expected = native_values.get(metric)
                try:
                    delta = abs(float(calculated) - float(expected))
                    if delta > base["absolute_tolerance"]:
                        divergences.append(
                            {
                                "section": section,
                                "metric": metric,
                                "lean_native": expected,
                                "python_lean_compatible": str(calculated),
                                "absolute_delta": delta,
                            }
                        )
                except (TypeError, ValueError):
                    if str(calculated) != str(expected):
                        divergences.append(
                            {
                                "section": section,
                                "metric": metric,
                                "lean_native": expected,
                                "python_lean_compatible": str(calculated),
                                "reason": "formatted_value_mismatch",
                            }
                        )

        total_fees = str(reproduced["tradeStatistics"]["totalFees"])
        reproduced_summary = format_lean_statistics_summary(
            reproduced,
            total_orders=len(normalized.orders),
            total_fees=Decimal(total_fees),
        )
        for metric, calculated in reproduced_summary.items():
            expected = normalized.statistics.get(metric)
            if calculated != expected:
                divergences.append(
                    {
                        "section": "formattedStatistics",
                        "metric": metric,
                        "lean_native": expected,
                        "python_lean_compatible": calculated,
                        "reason": "formatted_value_mismatch",
                    }
                )
        json_safe_reproduced = json.loads(json.dumps(reproduced, default=str))
        return base | {
            "status": "match" if not divergences else "mismatch",
            "reason": None,
            "native_metric_count": metric_count,
            "formatted_metric_count": len(reproduced_summary),
            "divergences": divergences,
            "lean_native": native,
            "python_lean_compatible": json_safe_reproduced,
            "formatted_python_lean_compatible": reproduced_summary,
            "primitive_receipts": normalized.artifacts,
        }
    except (KeyError, OSError, TypeError, ValueError) as exc:
        logger.warning("LEAN native metric reproduction unavailable: %s", exc)
        return base | {
            "status": "unavailable",
            "reason": f"lean_native_reproduction_error:{type(exc).__name__}",
            "lean_native": None,
            "python_lean_compatible": None,
        }

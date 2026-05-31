"""Decision-column descriptors for the operator console (#396).

The strategy spec is the source of truth for decision-column semantics
(``DecisionColumnSpec``: name, dtype, nullable, semantic). This derives the
operator-facing ``label`` and ``format`` from the spec so the instance console
renders *any* strategy's indicators generically — zero hardcoded names. The
descriptors ride in the instance status payload; the UI never joins the spec.
"""

from __future__ import annotations

import re

from app.engine.strategy.spec.schema import StrategySpec


def _format_for_dtype(dtype: str) -> str:
    d = dtype.lower()
    if "float" in d:
        return "decimal"
    if "int" in d:
        return "integer"
    if "bool" in d:
        return "boolean"
    return "text"


def humanize_column(name: str) -> str:
    """``ema5`` -> ``EMA 5``, ``rsi`` -> ``RSI``, ``supertrend`` -> ``Supertrend``."""
    words: list[str] = []
    for token in re.findall(r"[A-Za-z]+|\d+", name):
        words.append(token.upper() if token.isalpha() and len(token) <= 4 else token.capitalize())
    return " ".join(words) or name


def decision_column_descriptors(spec: StrategySpec) -> list[dict]:
    """Resolved descriptor view of a spec's strategy-specific decision columns."""
    return [
        {
            "name": c.name,
            "label": humanize_column(c.name),
            "type": c.dtype,
            "format": _format_for_dtype(c.dtype),
            "semantic": c.semantic,
        }
        for c in spec.decision_columns
    ]

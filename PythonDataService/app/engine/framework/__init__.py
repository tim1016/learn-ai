"""LEAN Algorithm Framework — Insight, Alpha, Portfolio Construction, Risk, Execution.

This package ports QuantConnect LEAN's Algorithm Framework into our lightweight
Python engine. The framework is additive: existing strategies that call
set_holdings() / liquidate() directly continue to work unchanged. New strategies
can optionally emit Insights (structured predictions) that are tracked and
scored by the InsightManager.

Today the package holds the Insight data model, the InsightManager and
the scorer (``insight.py``, ``insight_manager.py``, ``insight_scorer.py``).
The AlphaModel interface and built-in alpha models were cut in #2752.
"""

"""LEAN Algorithm Framework — Insight, Alpha, Portfolio Construction, Risk, Execution.

This package ports QuantConnect LEAN's Algorithm Framework into our lightweight
Python engine. The framework is additive: existing strategies that call
set_holdings() / liquidate() directly continue to work unchanged. New strategies
can optionally emit Insights (structured predictions) that are tracked and
scored by the InsightManager.

Phase 1: Insight data model + InsightManager + scoring
Phase 2: AlphaModel interface + built-in alpha models
"""

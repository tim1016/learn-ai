"""Interactive Brokers integration (paper-first).

Phase 1: read-only option-chain streaming with Greeks, used as a third
authority alongside the engine's QuantLib / py_vollib calculations. See
ADR 0062 ("Retained market-data provider") for the read-only feed
decision.

This subpackage wraps the full ``ib_async`` surface area we plausibly
need; ``app.routers.broker`` exposes only the curated subset the rest of
the app is allowed to touch.
"""

"""Interactive Brokers integration (paper-first).

Phase 1: read-only option-chain streaming with Greeks, used as a third
authority alongside the engine's QuantLib / py_vollib calculations. See
docs/architecture/ibkr-integration-phase1.md for the design and safety
patterns enforced here.

This subpackage wraps the full ``ib_async`` surface area we plausibly
need; ``app.routers.broker`` exposes only the curated subset the rest of
the app is allowed to touch.
"""

"""Autouse fixture wiring for the bot_runner test package (split of the
former ``tests/services/test_bot_runner.py`` per issue #1737 -- see that
issue for the seam rationale).

Originally: tests for app.services.bot_runner -- the in-container bot task
registry. Covers issue #1260 acceptance criteria:
- deploy -> running asyncio task + durable ON_DUTY evidence readable without
  the runner (raw artifact files).
- stop -> durable STOPPED desired-state, clean task exit, OFF_DUTY evidence.
- simulated crash -> typed durable crash evidence distinct from a clean stop;
  the registry reaps and never renders the bot healthy.
- daemon-free by construction (no daemon-client / subprocess imports).
- container-side artifact paths only (everything under the tmp_path root).
- broker-tagged bindings.
- restart-intensity guard reusing the canonical policy semantics.

This file holds only the two autouse fixtures every test in the package
needs by default. Test doubles, constants, and fixture-data builders used
across module boundaries live in ``tests/_helpers/bot_runner/``; the ones
shared only within this local package live in ``tests/services/bot_runner/
_support.py`` (issue #1810 -- see the PR description for the extraction
rationale: this file used to also hold 36 top-level helpers/doubles/
constants that nine local and eleven outside test modules imported
directly, turning pytest configuration into an undeclared public library).
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from tests._helpers.bot_runner.custody import _custody_proof
from tests._helpers.bot_runner.doubles import _CustodyClerk
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness


@pytest.fixture(autouse=True)
def _fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fresh_live_market_liveness(monkeypatch)


@pytest.fixture(autouse=True)
def _default_lifecycle_clerk() -> None:
    """Give runner unit tests a local duty-authority Adapter by default."""

    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
    yield
    set_alpaca_clerk(None)

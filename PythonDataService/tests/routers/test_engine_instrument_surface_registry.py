"""Slice 1A — strategy registry exposes ``instrument_surface``.

PRD #593 §"The instrument-surface registry flag" — every CURRENT strategy
registers as ``explicit`` (policy-surface strategies don't yet exist;
``policy`` is in the enum for forward-compat with Slice 4). The field is
informational at the deploy boundary in Slices 1–3 — Slice 4 introduces
runtime enforcement.

Prior art: ``test_run_cli.test_lookup_sizing_surface_resolves_module_name_to_registry_key``.
"""

from __future__ import annotations

import pytest

from app.routers.engine import _STRATEGY_REGISTRY


_EXPECTED_INSTRUMENT_SURFACE = "explicit"


@pytest.mark.parametrize("strategy_key", sorted(_STRATEGY_REGISTRY.keys()))
def test_every_registered_strategy_declares_explicit_instrument_surface(
    strategy_key: str,
) -> None:
    reg = _STRATEGY_REGISTRY[strategy_key]

    assert reg.instrument_surface == _EXPECTED_INSTRUMENT_SURFACE

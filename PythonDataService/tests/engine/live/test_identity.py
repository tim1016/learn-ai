"""Tests for the shared ``strategy_instance_id`` validator.

The validator is the single creation-time guard that keeps a deployment name in
lockstep with what the operate endpoints (``status`` / ``start`` / ``stop``)
will accept, so a run is never created under a name that can never be operated
on. See ``app.engine.live.identity``.
"""

from __future__ import annotations

import pytest

from app.engine.live.identity import validate_strategy_instance_id


@pytest.mark.parametrize(
    "value",
    [
        "deployment-validation-jun3",
        "spy_ema_crossover",
        "a",
        "Bot.1",
        "X" * 128,
    ],
)
def test_validate_strategy_instance_id_accepts_valid(value: str) -> None:
    assert validate_strategy_instance_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "Deploy morning Jun 3",  # internal spaces — the reported bug
        "",  # empty
        " leading",
        "trailing ",
        "..",
        ".",
        "a/b",
        "a\\b",
        "-startshyphen",  # must start with a letter or digit
        "bad@name",
        "X" * 129,  # too long
        "nul\x00byte",
    ],
)
def test_validate_strategy_instance_id_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        validate_strategy_instance_id(value)


def test_instance_id_pattern_matches_operate_endpoint_guard() -> None:
    """Single source of truth: the creation-time pattern in ``identity`` must
    stay byte-identical to the operate-endpoint guard in ``live_instances`` so a
    name accepted at deploy is never rejected at status/start/stop (and vice
    versa). The router keeps its own literal so CodeQL recognises the
    path-injection barrier; this test pins them in lockstep."""
    from app.engine.live.identity import _INSTANCE_ID_RE as creation_re
    from app.routers.live_instances import _INSTANCE_ID_RE as operate_re

    assert creation_re.pattern == operate_re.pattern

from __future__ import annotations

import pytest

from app.services.lifecycle_action_reasons import (
    LIFECYCLE_ACTION_REASON_CODES,
    expected_lifecycle_action_reason_codes,
    lifecycle_action_reason_for_code,
)


def test_lifecycle_action_reason_copy_covers_every_server_action_code() -> None:
    assert expected_lifecycle_action_reason_codes() == LIFECYCLE_ACTION_REASON_CODES


@pytest.mark.parametrize("code", sorted(expected_lifecycle_action_reason_codes()))
def test_lifecycle_action_reason_copy_is_trader_prose(code: str) -> None:
    reason = lifecycle_action_reason_for_code(code)

    assert reason.code == code
    assert reason.headline
    assert reason.detail
    assert code not in reason.headline
    assert "unrecognized" not in reason.detail.lower()

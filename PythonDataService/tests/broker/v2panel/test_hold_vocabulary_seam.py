"""DB -> compatibility projection -> panel card: a live hold stays a hold.

The SQLite authority stores its own spelling of a hold cause; the panel
renders a closed ``HoldReason`` vocabulary. Nothing crossed both seams with
a value actually read out of the database -- every other projection test
builds its ``ClerkProjection`` by hand -- so the two spellings were free to
disagree, and they did: an active, account-wide, entry-blocking
unexplained-order hold rendered on the operator panel as "No hold".

These tests read the cause out of a real database, through the production
fold, the production projection reader and the production compatibility
projection, and assert on the card an operator actually sees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import STREAM_HEALTH_REASON_CODE
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODES,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.alpaca.clerk.trade_evidence import UNEXPLAINED_TRADE_UPDATE_REASON_CODE
from app.schemas.broker_v2_panel import ClerkCard
from app.services.broker_v2_panel.panel_projection_service import build_clerk_card
from app.services.sqlite_clerk_compat import sqlite_clerk_status
from tests.broker.alpaca.clerk.sqlite.conftest import _hold_transition

ACCOUNT_ID = "PA-HOLD-SEAM"
SID = "spy-bot"
_NOW = 1_700_000_000_000

# Every reason code a production writer can store into `holds`: each writer's
# own constant, so a writer that renames its code cannot quietly stop being
# renderable, plus every cause the clerk registry admits, so a cause added
# there without a panel vocabulary row fails here instead of rendering as
# `UNKNOWN_HOLD` (ADR 0059 D4 added the third).
_STORED_HOLD_CAUSES = tuple(
    sorted(
        {
            UNEXPLAINED_ORDER_HOLD_REASON_CODE,
            UNEXPLAINED_TRADE_UPDATE_REASON_CODE,
            STREAM_HEALTH_REASON_CODE,
            *HOLD_REASON_CODES,
        }
    )
)

_LOSS_CAUSE = LossHoldCause(
    day_start_ms=1_788_000_000_000,
    day_pnl_usd=-5_250.0,
    loss_limit_usd=5_000.0,
    last_equity_usd=100_000.0,
    observed_at_ms=1_788_040_000_000,
)


class _Clock:
    def __init__(self) -> None:
        self.value = _NOW

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _write_hold(repo: ClerkSqliteRepository, reason_code: str) -> None:
    """Write one hold the way its own producer does.

    The two v12 causes still arrive as the pre-v12 ``ACCOUNT_HOLD_RAISED``
    transition replayed from a mirror, which is the path this file exists to
    cross. The ADR 0059 D4 loss hold postdates v12 and cannot arrive that way:
    its cause is the breached day-P&L numbers, not evidence lines, so it is
    raised through the seam that carries them.
    """
    if reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE:
        raise_account_hold(
            repo,
            reason_code=reason_code,
            evidence_refs=[f"day-pnl:{_LOSS_CAUSE.day_start_ms}"],
            cause_facts=_LOSS_CAUSE.to_mapping(),
        )
        return
    repo.append_transition(_hold_transition(reason_code=reason_code))


def _card_for_stored_cause(tmp_path: Path, reason_code: str) -> ClerkCard:
    """Write one hold through the real fold and render the operator's card."""
    clock = _Clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="spy-hash",
    )
    _write_hold(repo, reason_code)
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        projection = reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert projection is not None
    return build_clerk_card(sqlite_clerk_status(projection), _NOW)


@pytest.mark.parametrize("reason_code", _STORED_HOLD_CAUSES)
def test_every_stored_hold_cause_renders_as_an_active_hold(
    tmp_path: Path, reason_code: str
) -> None:
    """No production hold cause may render as "No hold".

    This is the regression: `UNEXPLAINED_ORDER` is stored by three separate
    writers and the wire vocabulary spells it `UNEXPLAINED_ORDER_HOLD`, so
    the narrowing step resolved it to `NO_HOLD` -- denying, on the operator's
    only view of it, a freeze that was blocking every entry on the account.
    """
    card = _card_for_stored_cause(tmp_path, reason_code)

    assert card.hold_active is True
    assert card.hold_reason != "NO_HOLD"
    assert card.hold_reason_label.strip()
    assert card.hold_reason_label != "No hold"


def test_the_unexplained_order_cause_keeps_its_own_identity(tmp_path: Path) -> None:
    """Failing closed is not enough -- the operator is told *which* hold."""
    card = _card_for_stored_cause(tmp_path, UNEXPLAINED_ORDER_HOLD_REASON_CODE)

    assert card.hold_reason == "UNEXPLAINED_ORDER_HOLD"
    assert card.hold_reason_label == "Unexplained-order hold"


def test_an_unregistered_hold_cause_cannot_be_written_at_all(tmp_path: Path) -> None:
    """The fail-closed point moved earlier, from render time to write time.

    Before v12 an unnameable cause could reach a ``holds`` row and the card
    narrowed it to ``UNKNOWN_HOLD`` -- fail-closed at the last possible
    moment. ADR 0048 Decision 2 makes a hold an uncertainty, and every
    uncertainty must declare a policy, so a cause with no registered policy
    is now refused where it is written instead of being described after the
    fact. ``UNKNOWN_HOLD`` survives in the vocabulary for a row written by a
    *future* build and read by this one, which is the only way one can still
    arrive.
    """
    with pytest.raises(KeyError, match="SOME_FUTURE_HOLD_CAUSE"):
        _card_for_stored_cause(tmp_path, "SOME_FUTURE_HOLD_CAUSE")


def test_no_hold_renders_as_no_hold(tmp_path: Path) -> None:
    """The inactive case is unchanged: absence of a hold reads as absence."""
    clock = _Clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="spy-hash",
    )
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        projection = reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert projection is not None
    card = build_clerk_card(sqlite_clerk_status(projection), _NOW)

    assert card.hold_active is False
    assert card.hold_reason == "NO_HOLD"
    assert card.hold_reason_label == "No hold"

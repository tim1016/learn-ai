"""A filled manual chain's exact executions prove its total, against the head's quantity (#2786).

The two sequences the #2686 review found, and a chain of one that hit the
same wall, all driven through the real sweep:

1. A fills 2 shares on the stream; its replacement B fills 3 where only REST
   sees it, and B's ``filled_qty`` counts only its own shares. B's execution
   is not in the account activity yet when the sweep first sees B
   ``filled``, so that pass folds a REST cumulative that under-credits the
   chain -- 3 shares against the broker's 5. Once the activity posts B's
   execution, the chain's exact executions cover the head's quantity: they
   supersede the cumulative, the position is corrected to 5 and the leg ends
   without an operator.
2. A asks for 5 and the owner's edit raises it to 10 (B). Exact executions
   that happen to total the original 5 while B is ``filled`` must not end the
   leg: it ends only once they cover B's 10.
3. An unreplaced order whose every exact execution was held aside behind
   its REST cumulative ends on them once they cover its quantity.

Each store then replays from its custody mirror to the same rows.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    ChainTotalCoverageEvidence,
    chain_total_proves_coverage,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import POSITION_DRIFT_REASON_CODE
from tests.broker.alpaca.clerk.sqlite.test_manual_order_filled_over_rest import (
    _EXEC_1,
    _EXEC_2,
    _EXEC_A,
    _EXEC_B,
    _ActivityFeed,
    _coverage_conflict_active,
    _coverage_conflict_episodes,
    _exact_credited,
    _fill_frame,
    _filled,
    _sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_manual_order_replaced_at_alpaca import (
    _B,
    _account_hold_active,
    _another_bots_entry,
    _buy_limit,
    _manual_endings,
    _replace_at_alpaca,
    _Website,
)
from tests.broker.alpaca.clerk.sqlite.test_reconcile import (
    ACCOUNT_ID,
    _register_second_spy_lane,
    clocked_repo,  # noqa: F401 -- pytest fixture, used by name
)

_EXEC_B1 = str(uuid.UUID(int=0xEB1))
_EXEC_B2 = str(uuid.UUID(int=0xEB2))
_EXEC_B3 = str(uuid.UUID(int=0xEB3))

#: The pinned share tolerance of the chain-total proof (ADR 0036, 2026-10-01 amendment).
_QTY_ATOL = 1e-9

#: Rows a rebuild writes for itself: the authority's own identity and lease,
#: and the mirror fence of the rebuild.
_REBUILD_OWN_TABLES = frozenset({"control_meta", "mirror_fence"})


def _projection(repo: ClerkSqliteRepository) -> dict[str, list[tuple]]:
    """Every row of every table the folds write, in a stable order."""
    tables = [
        row["name"]
        for row in repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if row["name"] not in _REBUILD_OWN_TABLES
    ]
    return {
        table: sorted(tuple(row) for row in repo._conn.execute(f"SELECT * FROM {table}").fetchall())
        for table in tables
    }


def _assert_replays_identically(
    repo: ClerkSqliteRepository, *, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A rebuild from the custody mirror folds every row the live appends folded."""
    replay_root = tmp_path_factory.mktemp("replay")
    shutil.copytree(tmp_path, replay_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("clerk.db*"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=replay_root)
    try:
        assert _projection(rebuilt) == _projection(repo)
    finally:
        rebuilt.close()


async def test_an_under_credited_chain_ends_on_its_exact_executions_once_the_heads_execution_posts(
    clocked_repo, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,  # noqa: F811
) -> None:
    """A fills 2 on the stream; B fills 3 over REST, its ``filled_qty`` its own 3, its execution not posted yet.

    The first pass folds B's cumulative: 1 share past A's 2, so the chain is
    credited 3 against the broker's 5 and the symbol drifts. The next pass
    reads B's execution: A's 2 and B's 3 cover B's quantity, so they replace
    the cumulative, the leg's position becomes the broker's 5, the drift and
    the coverage conflict clear, and the leg ends -- no operator.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    sid_b, _run_b = _register_second_spy_lane(repo)
    now = repo.clock()
    a_partial = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    website.orders[order_ref] = a_partial
    await _fill_frame(repo, a_partial, execution_id=_EXEC_A, quantity=2, price=99.90)
    _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref)
    website.replacements[_B] = _filled(replacement_b, repo, filled_quantity=3, avg=99.80)
    feed = _ActivityFeed()
    feed.fill(execution_id=_EXEC_A, order_id=a_partial.order_id, quantity=2, price=99.90, at_ms=now)

    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 3.0}, abs=1e-9, rel=0)
    assert _account_hold_active(repo, POSITION_DRIFT_REASON_CODE), "the under-credited chain drifts"
    assert repo.effect_operation(effect_id).state == "in_progress"

    feed.fill(execution_id=_EXEC_B, order_id=_B, quantity=3, price=99.80, at_ms=now + 1)
    clock.value += 15_000
    result = await _sweep(repo, website, feed, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded", "the chain's executions must end the leg"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert _exact_credited(repo, order_ref) == [
        (_EXEC_A, "websocket", 2.0, 99.90),
        (_EXEC_B, "activity_recovery", 3.0, 99.80),
    ]
    assert [fill["evidence_source"] for fill in repo.fills_for_order(order_ref)].count("cumulative_recovery") == 0
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(5.0, abs=1e-9, rel=0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert _coverage_conflict_episodes(repo) == ["resolved"], "raised for B's execution, closed by the proof"
    assert not _account_hold_active(repo, POSITION_DRIFT_REASON_CODE)
    assert result.verdict == "clean"
    assert _another_bots_entry(repo, sid_b) == (True, None)
    _assert_replays_identically(repo, tmp_path=tmp_path, tmp_path_factory=tmp_path_factory)


async def test_a_raised_replacement_ends_only_when_exact_executions_cover_its_own_quantity(
    clocked_repo, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,  # noqa: F811
) -> None:
    """A asks for 5 and fills 2 over REST; the owner raises it to 10 (B), which fills the other 8.

    REST sees B at 5 (its ``filled_qty`` carries A's 2) and the stream B's
    last execution with B ``filled``. A pass that then reads only A's
    execution proves A's 2 and B's last 3 stand for the cumulative 5: the
    exact total reaches A's 5 while B is ``filled``. That must not end the
    leg -- B asked for 10. It ends on the pass that reads B's other two
    executions, at 10.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    now = repo.clock()
    website.orders[order_ref] = website.orders[order_ref].model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    original = website.orders[order_ref]
    feed = _ActivityFeed()

    await _sweep(repo, website, feed, spy_held=2.0)

    _replaced_a, replacement_b = _replace_at_alpaca(repo, website, order_ref, quantity=10)
    website.replacements[_B] = replacement_b.model_copy(update={
        "status": "partially_filled", "filled_quantity": 5, "filled_avg_price": 99.90,
        "updated_at_ms": repo.clock(), "observed_at_ms": repo.clock(),
    })
    clock.value += 15_000
    await _sweep(repo, website, feed, spy_held=5.0)

    assert repo.order(order_ref).broker_order_id == _B
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(5.0, abs=1e-9, rel=0)
    clock.value += 15_000
    filled_b = _filled(replacement_b, repo, filled_quantity=10, avg=99.90)
    website.replacements[_B] = filled_b
    await _fill_frame(repo, filled_b, execution_id=_EXEC_B3, quantity=3, price=99.90)

    assert _coverage_conflict_active(repo), "B's last execution cannot stand for the 5-share cumulative alone"
    feed.fill(execution_id=_EXEC_A, order_id=original.order_id, quantity=2, price=99.90, at_ms=now)
    clock.value += 15_000
    await _sweep(repo, website, feed, spy_held=10.0)

    assert sorted(item[0] for item in _exact_credited(repo, order_ref)) == sorted([_EXEC_A, _EXEC_B3])
    assert repo.effect_operation(effect_id).state == "in_progress", (
        "exact executions reaching A's 5 must not end a leg whose head asked for 10"
    )
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 10.0}, abs=1e-9, rel=0)

    feed.fill(execution_id=_EXEC_B1, order_id=_B, quantity=3, price=99.90, at_ms=now + 1)
    feed.fill(execution_id=_EXEC_B2, order_id=_B, quantity=2, price=99.90, at_ms=now + 2)
    clock.value += 15_000
    result = await _sweep(repo, website, feed, spy_held=10.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert [t["transition_kind"] for t in _manual_endings(repo, order_ref)] == ["MANUAL_ORDER_FILLED"]
    assert sorted(item[0] for item in _exact_credited(repo, order_ref)) == sorted(
        [_EXEC_A, _EXEC_B1, _EXEC_B2, _EXEC_B3]
    )
    assert repo.effective_fill_totals_for_order(order_ref)[0] == pytest.approx(10.0, abs=1e-9, rel=0)
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 10.0}, abs=1e-9, rel=0)
    assert not _coverage_conflict_active(repo)
    assert result.verdict == "clean"
    _assert_replays_identically(repo, tmp_path=tmp_path, tmp_path_factory=tmp_path_factory)


async def test_a_filled_order_whose_executions_were_all_held_aside_ends_on_them(
    clocked_repo, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,  # noqa: F811
) -> None:
    """An unreplaced manual order: REST credits 2 shares, the stream's 3-share execution is held aside.

    The next pass reads both executions: the 2-share one is held aside with
    the 3, the REST cumulative brings the order to its 5, and before #2786
    the order-total proof closed the conflict with both executions still held
    aside -- every later read of them was a duplicate, so the leg never ended.
    Their total is the order's 5, so they replace the cumulative and the leg
    ends in that pass.
    """
    repo, clock = clocked_repo
    website = _Website(repo=repo)
    manual = await _buy_limit(repo, website)
    order_ref, effect_id = manual.leg.order_ref, manual.leg.effect_operation_id
    original = website.orders[order_ref]
    now = repo.clock()
    website.orders[order_ref] = original.model_copy(update={
        "status": "partially_filled", "filled_quantity": 2, "filled_avg_price": 99.90,
        "updated_at_ms": now, "observed_at_ms": now,
    })
    feed = _ActivityFeed()

    await _sweep(repo, website, feed, spy_held=2.0)

    clock.value += 15_000
    filled = _filled(original, repo, filled_quantity=5, avg=99.84)
    website.orders[order_ref] = filled
    await _fill_frame(repo, filled, execution_id=_EXEC_2, quantity=3, price=99.80)

    assert _coverage_conflict_active(repo)
    feed.fill(execution_id=_EXEC_1, order_id=original.order_id, quantity=2, price=99.90, at_ms=now)
    feed.fill(execution_id=_EXEC_2, order_id=original.order_id, quantity=3, price=99.80, at_ms=now + 1)
    clock.value += 15_000
    result = await _sweep(repo, website, feed, spy_held=5.0)

    effect = repo.effect_operation(effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert _exact_credited(repo, order_ref) == [
        (_EXEC_1, "activity_recovery", 2.0, 99.90),
        (_EXEC_2, "websocket", 3.0, 99.80),
    ]
    assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
    assert _coverage_conflict_episodes(repo) == ["resolved"]
    assert result.verdict == "clean"
    _assert_replays_identically(repo, tmp_path=tmp_path, tmp_path_factory=tmp_path_factory)


@pytest.mark.parametrize(
    ("head_state", "head_quantity", "exact_quantities", "proven"),
    [
        ("filled", 5.0, (2.0, 3.0), True),
        ("FILLED", 5.0, (2.0, 3.0), True),
        ("partially_filled", 5.0, (2.0, 3.0), False),
        ("filled", 10.0, (2.0, 3.0), False),
        ("filled", 1.5 * _QTY_ATOL, (_QTY_ATOL,), True),
        ("filled", 2 * _QTY_ATOL, (_QTY_ATOL,), False),
        ("filled", None, (5.0,), False),
        ("filled", 5.0, (), False),
        ("filled", 5.0, (2.0, 3.0, 0.0), False),
        ("filled", 5.0, (float("nan"),), False),
        ("filled", float("inf"), (float("inf"),), False),
    ],
    ids=[
        "covers_the_head", "state_case_insensitive", "head_not_filled", "raised_head_not_covered",
        "inside_the_strict_tolerance", "exactly_one_tolerance_apart", "head_quantity_unread",
        "no_exact_execution", "a_zero_share_execution", "a_nonfinite_execution", "a_nonfinite_head",
    ],
)
def test_chain_total_proves_coverage_on_its_strict_share_boundary(
    head_state: str, head_quantity: float | None, exact_quantities: tuple[float, ...], proven: bool
) -> None:
    """Only a filled head's own quantity, matched within ``|Σq − Q| < 1e-9`` shares by positive finite executions."""
    assert chain_total_proves_coverage(
        ChainTotalCoverageEvidence(
            head_state=head_state, head_quantity=head_quantity, exact_quantities=exact_quantities
        )
    ) is proven

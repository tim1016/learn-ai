"""Wiring coverage for the canary rollback verdict at Stop (issue #1729 AC10).

``app.services.canary_admission.evaluate_canary_rollback`` is a pure
classifier; ``tests/services/test_canary_admission.py`` already covers its
logic in isolation. This file proves the *caller* actually exists: that
``BotTaskRegistry.stop`` (via ``BotRunTerminalRecorder.replace_provisional_stop``)
computes the verdict for a canary instance and records it next to the
Clerk-proved Stop custody outcome that already gets persisted --
``BotRunOutcomeRecord`` / ``BotRunTerminalOutcomeView``, read back here through
the public ``current_run`` projection.

The canary allowlist (``CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS``) ships empty
and must stay empty (see ``test_canary_admission.py::test_canary_allowlist_ships_empty``),
which means a real "canary" bot can never reach RUNNING through the public
``deploy``/``deploy_with_admission`` surface in this test process either --
the same allowlist gate that protects production also blocks the front door
here. Every test below therefore deploys a bot and then substitutes the *running*
binding's ``program_build`` with a simulated ``PROVEN`` Signal-Program fact
-- exactly the fact shape a real admitted canary Start/Resume would have
attached. This isolates the thing under test (does Stop's wiring key off the
running binding's program-build proof and record a verdict?) from
Start-admission policy, which is already exercised by
``test_run_admission.py``.

Note that the deploy step itself is no longer outside the canary gate.
``deployment_validation`` -- this file's real deployed strategy -- was
promoted through the governed Signal Program seam (issue #1730 Slice 5), so
its build now resolves ``PROVEN`` at deploy time and the real allowlist gate
applies at the front door. Each ``_deploy_trade_bot_as_simulated_canary``
caller admits exactly the one pairing it deploys under; see
``_REAL_DEPLOY_PROGRAM_ACCOUNT_PAIR`` below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.models import AccountFreezeState
from app.schemas.run_admission import ProgramBuildAdmissionFact
from tests._helpers.canary_admission import admit_canary_pairing
from tests.services.test_bot_runner import (
    _SID,
    _T0,
    _custody_proof,
    _CustodyClerk,
    _FakeFeed,
    _fresh_live_market_liveness,  # noqa: F401 -- autouse fixture, registered by import
    _registry,
)

_PROGRAM_KEY = "ema_crossover_signal"
_ACCOUNT_ID = "paper-account"
# "deployment_validation" -- this file's real, undecorated deployed
# strategy_key (module docstring / `_deploy_trade_bot_as_simulated_canary`)
# -- was promoted through the governed Signal Program seam (issue #1730
# Slice 5) after this file was written. Its real build now resolves PROVEN
# at deploy time (a real, committed qualification receipt matches its
# current bytes), which means the real (not simulated) canary-allowlist
# gate now applies at the front door too, before any test ever reaches the
# simulated-canary override this file's tests are actually about. Every
# `_deploy_trade_bot_as_simulated_canary` caller admits exactly this
# pairing for the deploy step alone -- distinct from `_PROGRAM_KEY`/
# `_ACCOUNT_ID` above, which simulate a *different* fabricated PROVEN fact
# for the Stop-side wiring under test.
_REAL_DEPLOY_PROGRAM_ACCOUNT_PAIR = ("deployment_validation", _ACCOUNT_ID)


def _proven_build() -> ProgramBuildAdmissionFact:
    """A simulated live-reproof PROVEN fact, shaped like a real canary Start's."""
    return ProgramBuildAdmissionFact(
        state="PROVEN",
        program_key=_PROGRAM_KEY,
        program_version="1",
        golden_trace_root="a" * 64,
        running_artifact_digest="b" * 64,
        qualification_receipt_hash="c" * 64,
        verified_at_ms=_T0,
        evidence_refs=(f"program-build-digest:{'b' * 64}",),
        explanation="test: simulated proven Signal Program build for Stop-side wiring coverage.",
    )


def _active_freeze() -> AccountFreezeState:
    return AccountFreezeState(
        active=True,
        category="ACCOUNT_STATE_UNPROVABLE",
        explanation="Fresh order and exposure truth is unavailable.",
        next_step="Restore broker observation, then reconcile.",
        observed_at_ms=_T0,
    )


async def _deploy_trade_bot_as_simulated_canary(
    registry,
    monkeypatch: pytest.MonkeyPatch,
    *,
    admitted_pairing: tuple[str, str] = _REAL_DEPLOY_PROGRAM_ACCOUNT_PAIR,
    binding_overrides: dict | None = None,
):
    """Deploy a real (non-canary) trade bot, then substitute its running
    binding's ``program_build`` (and any extra overrides) to simulate what a
    genuinely admitted canary run's binding carries. See module docstring for
    why the front door itself cannot produce this.

    The deploy step itself now needs its own allowlist entry: this file's
    real deployed strategy ("deployment_validation") became a registered
    Signal Program (issue #1730 Slice 5) and its real build proves PROVEN at
    deploy time, so the front door's own canary gate applies before any
    simulated override runs. Admitting ``admitted_pairing`` (exactly one
    pairing, for the duration of the deploy call) is a real, visible
    mutation of the shared module-level allowlist and is unrelated to
    whatever allowlist state an individual test wants in place afterward, at
    Stop -- callers are free to admit a different pairing again once this
    returns.
    """
    admit_canary_pairing(monkeypatch, *admitted_pairing)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade")
    managed = registry._bots[_SID]
    managed.binding = managed.binding.model_copy(
        update={"program_build": _proven_build(), **(binding_overrides or {})}
    )
    return managed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proof_kwargs", "binding_overrides", "expected_stop_outcome", "expected_allowed", "expected_reason_code"),
    [
        pytest.param(
            {"exposure": {}},
            {},
            "STOPPED_FLAT",
            True,
            "CANARY_ROLLBACK_ADMITTED",
            id="flat",
        ),
        pytest.param(
            {"exposure": {"SPY": 3.0}},
            {"carryover_policy": "ALLOW"},
            "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
            True,
            "CANARY_ROLLBACK_ADMITTED",
            id="approved-carried-exposure",
        ),
        pytest.param(
            {"exposure": {"SPY": 3.0}},
            {},
            "STOP_REQUIRES_FLATTEN",
            False,
            "CANARY_ROLLBACK_REQUIRES_FLATTEN",
            id="requires-flatten",
        ),
        pytest.param(
            {"exposure": {}, "freeze": _active_freeze()},
            {},
            "STOPPED_CUSTODY_UNPROVABLE",
            False,
            "CANARY_ROLLBACK_BOUNDARY_UNPROVABLE",
            id="custody-unprovable",
        ),
    ],
)
async def test_canary_rollback_recorded_for_each_stop_custody_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof_kwargs: dict,
    binding_overrides: dict,
    expected_stop_outcome: str,
    expected_allowed: bool,
    expected_reason_code: str,
) -> None:
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    try:
        registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
        await _deploy_trade_bot_as_simulated_canary(
            registry, monkeypatch, binding_overrides=binding_overrides
        )
        clerk.proof = _custody_proof(**proof_kwargs)

        view = await registry.stop("alpaca", _SID)

        assert view.running is False
        assert view.duty_outcome is not None
        assert view.duty_outcome.reason_code == expected_stop_outcome

        terminal = registry.current_run("alpaca", _SID).terminal_outcome
        assert terminal is not None
        assert terminal.reason_code == expected_stop_outcome
        assert terminal.canary_rollback is not None
        assert terminal.canary_rollback.stop_outcome == expected_stop_outcome
        assert terminal.canary_rollback.allowed is expected_allowed
        assert terminal.canary_rollback.reason_code == expected_reason_code
        assert terminal.canary_rollback.strategy_instance_id == _SID
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_stop_completes_and_records_outcome_when_rollback_verdict_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constraint 1 (#1729): rollback must NEVER block, delay, or refuse a
    Stop. An operator must always be able to stop a running bot; the verdict
    is an evidence record about whether the canary may be considered rolled
    back, never a veto over process termination. Prove it by driving Stop to
    completion under a boundary the verdict itself refuses (unapproved
    attributed exposure) and checking both the honest Stop outcome and the
    refusing verdict are recorded side by side."""
    clerk = _CustodyClerk(_custody_proof(exposure={"SPY": 3.0}))
    set_alpaca_clerk(clerk)
    try:
        registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
        await _deploy_trade_bot_as_simulated_canary(registry, monkeypatch)

        view = await registry.stop("alpaca", _SID)

        # Stop itself completed and recorded its honest outcome -- unblocked,
        # undelayed, unrefused.
        assert view.running is False
        assert view.phase == "OFF_DUTY"
        assert view.desired_state == "STOPPED"
        assert view.duty_outcome is not None
        assert view.duty_outcome.kind == "STOPPED"
        assert view.duty_outcome.reason_code == "STOP_REQUIRES_FLATTEN"

        # The rollback verdict is recorded honestly as refusing, alongside
        # -- not instead of -- that completed Stop.
        terminal = registry.current_run("alpaca", _SID).terminal_outcome
        assert terminal is not None
        assert terminal.canary_rollback is not None
        assert terminal.canary_rollback.allowed is False
        assert terminal.canary_rollback.reason_code == "CANARY_ROLLBACK_REQUIRES_FLATTEN"
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_canary_rollback_verdict_absent_for_dry_run_instance(tmp_path: Path) -> None:
    """PRD Sec 15: Dry Run uses its own isolated synthetic authority and is
    never subject to the canary gate, even if its build proves PROVEN."""
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    try:
        registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="dry_run")
        managed = registry._bots[_SID]
        managed.binding = managed.binding.model_copy(update={"program_build": _proven_build()})

        view = await registry.stop("alpaca", _SID)

        assert view.running is False
        terminal = registry.current_run("alpaca", _SID).terminal_outcome
        assert terminal is not None
        assert terminal.canary_rollback is None
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_canary_rollback_verdict_absent_for_legacy_non_program_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constraint 5 (#1729): legacy strategies with no registered Signal
    Program (``program_build.state == "NOT_APPLICABLE"``) are untouched.

    Simulated here, unlike before issue #1730 Slice 5: this file's real
    deployed strategy ("deployment_validation") was itself promoted through
    the governed Signal Program seam, and no strategy remains in
    ``app.services.bot_trade_strategy.supported_alpaca_paper_strategy_keys()``
    with a real, undecorated NOT_APPLICABLE build any more -- there is no live
    trade-deployable subject left to exercise unmodified. The override below
    reproduces exactly the fact shape a real legacy (no registered Signal
    Program) instance's binding carried when this test could still get one
    for real."""
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    try:
        registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
        await _deploy_trade_bot_as_simulated_canary(
            registry,
            monkeypatch,
            binding_overrides={
                "program_build": ProgramBuildAdmissionFact(
                    state="NOT_APPLICABLE",
                    program_key="deployment_validation",
                    verified_at_ms=_T0,
                    explanation="test: simulated legacy strategy with no registered Signal Program.",
                )
            },
        )
        managed = registry._bots[_SID]
        assert managed.binding.program_build is not None
        assert managed.binding.program_build.state == "NOT_APPLICABLE"

        await registry.stop("alpaca", _SID)

        terminal = registry.current_run("alpaca", _SID).terminal_outcome
        assert terminal is not None
        assert terminal.reason_code == "STOPPED_FLAT"
        assert terminal.canary_rollback is None
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_canary_rollback_verdict_ignores_current_allowlist_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subtlety this wiring must get right: a rollback plausibly *means*
    removing the pairing from the allowlist, so a verdict computed by testing
    current allowlist membership would evaluate to "not a canary" precisely
    when it matters most. The verdict must key off the run's own admitted
    program-build proof, not off whether the exact pairing is currently
    allowlisted. Prove it by stopping the same simulated-canary configuration
    once against an empty allowlist and once against one that *does* contain
    this exact pairing, and showing the recorded verdict is identical either
    way."""

    async def _stop_and_read_verdict(root: Path, pairs: frozenset[tuple[str, str]]):
        clerk = _CustodyClerk(_custody_proof(exposure={}))
        set_alpaca_clerk(clerk)
        try:
            registry = _registry(root, _FakeFeed([], mode="hold"))
            await _deploy_trade_bot_as_simulated_canary(registry, monkeypatch)
            monkeypatch.setattr(
                "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
                pairs,
            )

            await registry.stop("alpaca", _SID)

            return registry.current_run("alpaca", _SID).terminal_outcome.canary_rollback
        finally:
            set_alpaca_clerk(None)

    without_entry = await _stop_and_read_verdict(tmp_path / "no-entry", frozenset())
    with_entry = await _stop_and_read_verdict(
        tmp_path / "with-entry",
        frozenset({(_PROGRAM_KEY, _ACCOUNT_ID)}),
    )

    assert without_entry is not None
    assert with_entry is not None
    assert without_entry.allowed is with_entry.allowed
    assert without_entry.reason_code == with_entry.reason_code
    assert without_entry.stop_outcome == with_entry.stop_outcome

"""The prior-account obligations probe must prove clear, or refuse.

Every root here is under ``tmp_path``: no real Clerk volume, no container, no
credential. The two cases that matter most are the two ends — a genuinely clean
account really does come back clear (a probe that always refuses would silently
disable every switch), and everything else, readable or not, comes back
refusing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import custody_account_id_for
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationStore
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME, ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.broker_configuration.prior_obligations import ClerkPriorAccountObligations
from app.broker_configuration.worker_binding import PriorAccountObligations
from app.services.bot_binding_repository import (
    STRATEGY_INSTANCE_FILENAME,
    BrokerBotBinding,
    alpaca_v1_action_plan,
    live_state_binding_repository,
)

ACCOUNT_ID = "PA-PRIOR"
OTHER_ACCOUNT_ID = "PA-OTHER"
NOW_MS = 1_800_000_000_000
SID = "alpaca-spy-ema-01"
RUN_ID = "run-001"
SYMBOL = "SPY"


# ── Roots and the probe under test ────────────────────────────────────────────


@pytest.fixture
def clerk_dir(tmp_path: Path) -> Path:
    """The Clerk volume: holds ``accounts/alpaca/<account>/clerk.db``."""
    root = tmp_path / "clerk"
    root.mkdir()
    return root


@pytest.fixture
def live_state_root(tmp_path: Path) -> Path:
    """The artifacts root that contains ``live_state/`` — a different volume."""
    root = tmp_path / "live_runs"
    root.mkdir()
    return root


@pytest.fixture
def probe(clerk_dir: Path, live_state_root: Path) -> ClerkPriorAccountObligations:
    return ClerkPriorAccountObligations(clerk_dir=clerk_dir, live_state_root=live_state_root)


# ── Building a real custody authority (never hand-rolled SQL) ─────────────────


def _db_path(clerk_dir: Path, account_id: str = ACCOUNT_ID) -> Path:
    return clerk_dir / "accounts" / "alpaca" / account_id / DB_FILENAME


def _record_activation(
    clerk_dir: Path,
    account_id: str = ACCOUNT_ID,
    *,
    db_identity_token: str | None = None,
) -> None:
    """Append the cutover's activation evidence for an existing database.

    The same ceremony ``tests/broker/alpaca/clerk/sqlite/test_activation_inventory.py``
    performs. ``db_identity_token`` overrides the observed one so a test can
    describe an activation record that contradicts the database it names.
    """
    database = verify_database(_db_path(clerk_dir, account_id), expected_account_id=account_id)
    proof_reference = f"accounts/alpaca/{account_id}/cutover/broker-proof.json"
    manifest_reference = f"accounts/alpaca/{account_id}/cutover/quarantine.json"
    identity = db_identity_token or database.db_identity_token
    payload = {
        "account_id": account_id,
        "authority_generation": database.authority_generation,
        "db_identity_token": identity,
    }
    proof_sha256 = atomic_write_json(clerk_dir / proof_reference, payload)
    manifest_sha256 = atomic_write_json(clerk_dir / manifest_reference, payload)
    ActivationStore(clerk_dir / "accounts" / "alpaca").append(
        ActivationRecord.create(
            account_id=account_id,
            authority_generation=database.authority_generation,
            db_identity_token=identity,
            broker_proof_reference=proof_reference,
            broker_proof_sha256=proof_sha256,
            legacy_quarantine_manifest=manifest_reference,
            legacy_quarantine_manifest_sha256=manifest_sha256,
            activated_at_ms=NOW_MS - 10_000,
        )
    )


def _activate(
    clerk_dir: Path,
    *,
    account_id: str = ACCOUNT_ID,
    seed: Callable[[ClerkSqliteRepository], None] | None = None,
) -> None:
    """Initialize one account's authority, optionally seed it, then activate it.

    The repository is closed before the activation is recorded and before any
    probe runs, so the execution lease this created is released and the WAL is
    checkpointed — the state a real worker leaves behind when it stops.
    """
    repository = ClerkSqliteRepository.initialize(
        account_id=account_id, artifacts_root=clerk_dir, clock=lambda: NOW_MS
    )
    try:
        if seed is not None:
            seed(repository)
    finally:
        repository.close()
    _record_activation(clerk_dir, account_id)


def _start_a_bot(repository: ClerkSqliteRepository) -> None:
    repository.register_strategy_instance(
        strategy_instance_id=SID, symbol=SYMBOL, config_hash="config-hash"
    )
    submit_start_run(
        repository,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID,
        clock=lambda: NOW_MS,
    )


def _accept_an_enter(repository: ClerkSqliteRepository) -> EnterSubmission:
    _start_a_bot(repository)
    accepted = accept_enter(
        repository,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-1",
        lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol=SYMBOL, side="buy", quantity=10),
    )
    assert accepted.order_ref is not None
    return accepted


def _fill_an_enter(repository: ClerkSqliteRepository) -> None:
    """Accept an ENTER and fold one execution slice, leaving an open position."""
    accepted = _accept_an_enter(repository)
    facts = ExecutionSliceFilledFacts(
        execution_id="exec-1",
        symbol=SYMBOL,
        side="BUY",
        slice_qty=10.0,
        slice_price=100.0,
        fee=0.0,
        fee_fidelity="reported",
        evidence_source="websocket",
        source_event_at_ms=NOW_MS,
    )
    outcome = repository.append_execution_slice_if_absent(
        execution_id="exec-1",
        order_ref=accepted.order_ref or "",
        build_transition=lambda: TransitionInput(
            strategy_instance_id=SID,
            run_id=accepted.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=NOW_MS,
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        ),
        build_coverage_conflict=lambda: (_ for _ in ()).throw(
            AssertionError("a first exact execution has nothing to conflict with")
        ),
    )
    assert outcome == "appended"


def _accept_a_manual_order(repository: ClerkSqliteRepository) -> None:
    """One operator ticket leg, finalized locally and never settled."""
    accept_manual_order(
        repository,
        account_id=ACCOUNT_ID,
        operator_id="operator-1",
        ticket_id="ticket-1",
        leg_id="leg-1",
        leg=BrokerOrderLeg(symbol=SYMBOL, side="buy", quantity=1),
    )


def _observe_a_foreign_order(repository: ClerkSqliteRepository) -> None:
    """One order the Clerk did not place, and nobody has reviewed."""
    observe_external_order(
        repository,
        order=BrokerOrder(
            broker="alpaca",
            order_id="external-order-1",
            client_order_id="alpaca-console:external-1",
            symbol="MSFT",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=3.0,
            filled_quantity=3.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=50.0,
            status="filled",
            submitted_at_ms=NOW_MS,
            created_at_ms=NOW_MS,
            updated_at_ms=NOW_MS,
            filled_at_ms=NOW_MS,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=NOW_MS,
        ),
    )


def _record_binding(
    live_state_root: Path,
    *,
    sealed_account_id: str | None,
    strategy_instance_id: str = SID,
) -> None:
    """Persist one launched Alpaca bot binding, with no v2 program seal.

    Deliberately unsealed: ``instance_seal_hashes`` skips a binding like this,
    and the obligations probe must not.
    """
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id=strategy_instance_id,
            broker="alpaca",
            symbol=SYMBOL,
            action_plan=alpaca_v1_action_plan(SYMBOL),
            sealed_account_id=sealed_account_id,
            run_id=RUN_ID,
            created_at_ms=NOW_MS,
        ),
        launch_reason="deploy",
    )


# ── Never activated ───────────────────────────────────────────────────────────


def test_the_probe_satisfies_the_preflight_protocol(
    probe: ClerkPriorAccountObligations,
) -> None:
    """``resolve_worker_binding`` accepts it wherever ``UnprovableObligations`` goes."""
    assert isinstance(probe, PriorAccountObligations)


async def test_observe_never_activated_account_is_clear(
    probe: ClerkPriorAccountObligations,
) -> None:
    """No activation and nothing on disk: no custody this installation can owe."""
    observed = await probe.observe(ACCOUNT_ID)

    assert observed.is_clear
    assert observed.readable
    assert observed.blocking_facts == ()


async def test_observe_never_activated_account_with_a_custody_directory_is_unreadable(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """A custody directory with no activation record is evidence we cannot explain."""
    _db_path(clerk_dir).parent.mkdir(parents=True)

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable
    assert not observed.is_clear


async def test_observe_database_without_an_activation_record_is_unreadable(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    repository = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=clerk_dir, clock=lambda: NOW_MS
    )
    repository.close()

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable


# ── Activated, but unreadable ─────────────────────────────────────────────────


async def test_observe_activated_account_with_a_missing_database_is_unreadable(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    _activate(clerk_dir)
    _db_path(clerk_dir).unlink()

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable
    assert "could not be inspected" in observed.describe()


async def test_observe_corrupt_database_is_unreadable(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    _activate(clerk_dir)
    _db_path(clerk_dir).write_bytes(b"this is not a SQLite database")

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable


async def test_observe_database_contradicting_its_activation_record_is_unreadable(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """A substituted database is a refusal, not a clean read of the wrong account."""
    repository = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=clerk_dir, clock=lambda: NOW_MS
    )
    repository.close()
    _record_activation(clerk_dir, db_identity_token="a-different-database")

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable


async def test_observe_turns_an_unanticipated_failure_into_a_refusal(
    probe: ClerkPriorAccountObligations,
    clerk_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-closed guard: a surprise from any subsystem must not read as clear."""
    _activate(clerk_dir)

    def _explode(_conn: object) -> set[str]:
        raise RuntimeError("a failure mode this module has never heard of")

    monkeypatch.setattr(reads, "strategy_instances_with_live_custody", _explode)

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable


# ── Activated and readable: what still counts as an obligation ────────────────


async def test_observe_clean_activated_account_is_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """The case that matters: a probe that never proves clear disables switching."""
    _activate(clerk_dir)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.is_clear
    assert observed.blocking_facts == ()


async def test_observe_account_with_an_open_position_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    _activate(clerk_dir, seed=_fill_an_enter)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.readable
    assert not observed.is_clear
    described = observed.describe()
    assert "open position" in described
    assert SYMBOL in described


async def test_observe_account_with_an_unresolved_order_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    _activate(clerk_dir, seed=lambda repository: _accept_an_enter(repository))

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.readable
    assert not observed.is_clear
    assert "unresolved order" in observed.describe()


async def test_observe_account_with_a_running_bot_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """An ACTIVE run owes nothing yet and is still an obligation."""
    _activate(clerk_dir, seed=_start_a_bot)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.readable
    assert not observed.is_clear
    assert "live custody" in observed.describe()


async def test_observe_account_with_an_unfinished_manual_order_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """An operator ticket belongs to no bot, so only the account-wide read sees it."""
    _activate(clerk_dir, seed=_accept_a_manual_order)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.readable
    assert not observed.is_clear
    assert "unfinished manual order" in observed.describe()


async def test_observe_account_with_an_unreviewed_external_order_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """Activity the Clerk did not originate is still unfinished business here."""
    _activate(clerk_dir, seed=_observe_a_foreign_order)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.readable
    assert not observed.is_clear
    assert "outside the bots" in observed.describe()


# ── Sealed bot bindings, on their own volume ──────────────────────────────────


async def test_observe_account_with_a_sealed_binding_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    _activate(clerk_dir)
    _record_binding(live_state_root, sealed_account_id=ACCOUNT_ID)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.readable
    assert not observed.is_clear
    described = observed.describe()
    assert "still bound to it" in described
    assert SID in described


async def test_observe_account_with_a_shadow_sealed_binding_is_not_clear(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    """A rehearsal seals ``shadow:<account>``; it is the same account to an operator."""
    _activate(clerk_dir)
    _record_binding(
        live_state_root, sealed_account_id=custody_account_id_for("shadow", ACCOUNT_ID)
    )

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.is_clear


async def test_observe_describes_several_bindings_in_readable_prose(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    """The refusal is operator copy: every noun here is a phrase, not a word + "s"."""
    _activate(clerk_dir)
    _record_binding(live_state_root, sealed_account_id=ACCOUNT_ID, strategy_instance_id="bot-a")
    _record_binding(live_state_root, sealed_account_id=ACCOUNT_ID, strategy_instance_id="bot-b")

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.describe() == (
        f"account {ACCOUNT_ID} still has 2 bots still bound to it (bot-a, bot-b)"
    )


async def test_observe_ignores_a_binding_sealed_to_another_account(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    """Someone else's bot must not refuse this account's switch."""
    _activate(clerk_dir)
    _record_binding(live_state_root, sealed_account_id=OTHER_ACCOUNT_ID)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.is_clear


async def test_observe_unreadable_binding_row_is_unreadable(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    """``list_for_broker`` skips a corrupt row with a warning; the probe must not."""
    _activate(clerk_dir)
    instance_dir = live_state_root / "live_state" / SID
    instance_dir.mkdir(parents=True)
    (instance_dir / STRATEGY_INSTANCE_FILENAME).write_text("{ not json", encoding="utf-8")

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable


async def test_observe_is_unreadable_when_a_binding_row_returns_no_record(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    """``read`` does not always RAISE on a row it cannot materialise.

    It returns ``None`` when ``current_run.json`` is missing, or when the run
    record it names is gone. Those directories still pass the enumeration
    filter, and their ``strategy_instance.json`` may carry a
    ``sealed_account_id`` for the very account being proven clear — so treating
    ``None`` as "not a binding" answers "provably clear" about an account that
    still has a bot sealed to it. It is also the shape an ungraceful stop
    leaves behind: ``record_launch`` writes ``current_run.json`` last.
    """
    _activate(clerk_dir)
    _record_binding(live_state_root, sealed_account_id=ACCOUNT_ID)
    instance_dir = live_state_root / "live_state" / SID
    (instance_dir / "current_run.json").unlink()

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable
    assert not observed.is_clear


async def test_observe_is_unreadable_when_a_binding_run_record_is_gone(
    probe: ClerkPriorAccountObligations, clerk_dir: Path, live_state_root: Path
) -> None:
    """The other shape ``read`` answers ``None`` for."""
    import shutil

    _activate(clerk_dir)
    _record_binding(live_state_root, sealed_account_id=ACCOUNT_ID)
    shutil.rmtree(live_state_root / "live_state" / SID / "runs")

    observed = await probe.observe(ACCOUNT_ID)

    assert not observed.readable
    assert not observed.is_clear


# ── The constraint the whole probe is built around ────────────────────────────


async def test_observe_does_not_acquire_the_execution_lease(
    probe: ClerkPriorAccountObligations, clerk_dir: Path
) -> None:
    """Taking the lease on the account being left would be the bug being checked for."""
    _activate(clerk_dir)
    before = verify_database(_db_path(clerk_dir), expected_account_id=ACCOUNT_ID)

    observed = await probe.observe(ACCOUNT_ID)

    assert observed.is_clear
    after = verify_database(_db_path(clerk_dir), expected_account_id=ACCOUNT_ID)
    assert after == before
    # The lease is still free, so a real worker can still open this authority.
    repository = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=clerk_dir, clock=lambda: NOW_MS
    )
    repository.close()

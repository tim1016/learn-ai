"""Durable cancel → prove terminal → reduce once → verify flat EXIT machine."""

from __future__ import annotations

import base64
import hashlib

from app.broker.alpaca.clerk.exposure import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.recovery import UNCERTAIN_SUBMIT_GRACE_MS
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import (
    ExitSubmission,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    entry_order_symbol,
    fold_failed,
    fold_order_evidence,
    fold_uncertain,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    require_capability,
)
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from app.broker.contract.ports import BrokerTradePort
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref


async def resolve_exit(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    trade: BrokerTradePort,
) -> ExitSubmission:
    """Advance one EXIT under one exclusive, attempt-scoped broker claim."""
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    if effect.state in ("succeeded", "failed", "rejected"):
        return _snapshot(repo, effect_operation_id)

    claim = repo.claim_before_broker_contact(effect_operation_id)
    broker = ClaimedBrokerIO(
        repo=repo,
        effect_operation_id=effect_operation_id,
        claim_token=claim.token,
        trade=trade,
    )
    try:
        return await _resolve_claimed(
            repo,
            effect_operation_id=effect_operation_id,
            broker=broker,
        )
    finally:
        repo.release_operation_claim(effect_operation_id=effect_operation_id, token=claim.token)


async def _resolve_claimed(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    broker: ClaimedBrokerIO,
) -> ExitSubmission:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    orders = repo.orders_for_effect_operation(effect_operation_id)
    entries = [order for order in orders if order.role == "ENTRY"]
    reducing = next((order for order in orders if order.role == "REDUCING"), None)
    assert entries
    symbol = _single_entry_symbol(repo, entries)
    if not await _prove_entry_set_terminal(
        repo,
        effect_operation_id=effect_operation_id,
        entries=entries,
        reducing_exists=reducing is not None,
        broker=broker,
    ):
        return _snapshot(repo, effect_operation_id)

    remaining_qty = repo.position(effect.strategy_instance_id, symbol)
    if reducing is None and abs(remaining_qty) < POSITION_QTY_EPSILON:
        _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, entries))
        return _snapshot(repo, effect_operation_id)

    if reducing is None:
        # A previous call may have proven terminal minutes ago. Refresh every
        # exact entry identity under the same claim immediately before the
        # reducing quantity is fixed.
        if not await _refresh_terminal_entries(
            repo,
            effect_operation_id=effect_operation_id,
            entries=entries,
            broker=broker,
        ):
            return _snapshot(repo, effect_operation_id)
        remaining_qty = repo.position(effect.strategy_instance_id, symbol)
        if abs(remaining_qty) < POSITION_QTY_EPSILON:
            _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, entries))
            return _snapshot(repo, effect_operation_id)
        require_capability(
            repo,
            capability=Capability.REDUCE,
            strategy_instance_id=effect.strategy_instance_id,
            reduction_intent=ReductionIntent(
                symbol=symbol,
                side="SELL" if remaining_qty > 0 else "BUY",
                quantity=abs(remaining_qty),
            ),
        )
        reducing = _create_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            symbol=symbol,
            quantity=remaining_qty,
        )
        await _submit_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            reducing=reducing,
            broker=broker,
        )
    elif not _is_terminal(reducing.broker_state):
        await _refresh_or_resume_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            reducing=reducing,
            broker=broker,
        )

    reducing = repo.order(reducing.order_ref)
    assert reducing is not None
    if not _is_terminal(reducing.broker_state):
        return _snapshot(repo, effect_operation_id)

    final_qty = repo.position(effect.strategy_instance_id, symbol)
    if abs(final_qty) < POSITION_QTY_EPSILON:
        _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, entries))
    else:
        fold_failed(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            summary_code="EXIT_NOT_FLAT",
            reason="The reducing order resolved without flattening the position.",
            why=f"attributed_qty={final_qty} remains for {symbol!r} after terminal evidence.",
            transition_kind="EXIT_NOT_FLAT",
        )
    return _snapshot(repo, effect_operation_id)


def _single_entry_symbol(repo: ClerkSqliteRepository, entries: list[OrderResource]) -> str:
    symbols = {entry_order_symbol(repo, order.order_ref) for order in entries}
    if len(symbols) != 1:
        raise AssertionError("one EXIT may only link entry orders for one symbol")
    return next(iter(symbols))


async def _prove_entry_set_terminal(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entries: list[OrderResource],
    reducing_exists: bool,
    broker: ClaimedBrokerIO,
) -> bool:
    for entry in entries:
        if reducing_exists and _is_terminal(entry.broker_state):
            continue
        if not await _cancel_and_prove_entry(
            repo,
            effect_operation_id=effect_operation_id,
            entry=entry,
            broker=broker,
        ):
            return False
    return True


async def _cancel_and_prove_entry(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entry: OrderResource,
    broker: ClaimedBrokerIO,
) -> bool:
    if not _is_terminal(entry.broker_state) and entry.broker_order_id is not None:
        _append_order_phase_once(repo, effect_operation_id, entry, "ORDER_CANCEL_REQUESTED")
        try:
            # Reissuing cancel for the same broker identity is a retry of the
            # durable phase, not a second intent. This makes a lost cancel
            # response recoverable when the exact poll still shows working.
            await broker.cancel(entry.broker_order_id)
        except BrokerUnavailable as exc:
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=entry.order_ref,
                why=str(exc),
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )
            return False
        except BrokerError:
            # The exact lookup below, not the cancel response, proves state.
            pass

    observed = await broker.observe_exact(entry.client_order_id)
    if isinstance(observed, BrokerError) or observed is None:
        fold_uncertain(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=entry.order_ref,
            why=(str(observed) if isinstance(observed, BrokerError) else "No exact order evidence yet."),
            transition_kind="ORDER_CANCEL_UNCERTAIN",
        )
        return False
    fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=observed)
    refreshed = repo.order(entry.order_ref)
    assert refreshed is not None
    if not _is_terminal(refreshed.broker_state):
        return False
    _append_order_phase_once(repo, effect_operation_id, refreshed, "ENTRY_TERMINAL_CONFIRMED")
    return True


async def _refresh_terminal_entries(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entries: list[OrderResource],
    broker: ClaimedBrokerIO,
) -> bool:
    for entry in entries:
        observed = await broker.observe_exact(entry.client_order_id)
        if isinstance(observed, BrokerError) or observed is None:
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=entry.order_ref,
                why=(
                    str(observed)
                    if isinstance(observed, BrokerError)
                    else "Terminal entry evidence could not be refreshed."
                ),
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )
            return False
        fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=observed)
        current = repo.order(entry.order_ref)
        assert current is not None
        if not _is_terminal(current.broker_state):
            return False
    return True


def _create_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    symbol: str,
    quantity: float,
) -> OrderResource:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    side = "sell" if quantity > 0 else "buy"
    order_ref = build_order_ref(
        build_bot_order_namespace(effect.strategy_instance_id),
        _deterministic_intent_id(effect_operation_id),
    )
    facts = ExitReducingOrderCreatedFacts(
        symbol=symbol,
        side=side.upper(),
        quantity=abs(quantity),
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="EXIT_REDUCING_ORDER_CREATED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_REDUCING_ORDER_CREATED",
            facts_json=facts.to_facts_json(),
        )
    )
    created = repo.order(order_ref)
    assert created is not None
    return created


async def _refresh_or_resume_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    broker: ClaimedBrokerIO,
) -> None:
    observed = await broker.observe_exact(reducing.client_order_id)
    if isinstance(observed, BrokerError):
        fold_uncertain(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            why=str(observed),
        )
        return
    if observed is not None:
        fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=observed)
        return
    if reducing.broker_order_id is not None or not _absence_grace_elapsed(repo, reducing.order_ref):
        fold_uncertain(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            why="No exact reducing-order evidence yet; retaining custody.",
        )
        return
    await _submit_reducing_order(
        repo,
        effect_operation_id=effect_operation_id,
        reducing=reducing,
        broker=broker,
    )


async def _submit_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    broker: ClaimedBrokerIO,
) -> None:
    facts = _reducing_order_facts(repo, reducing.order_ref)
    leg = BrokerOrderLeg(
        symbol=facts.symbol,
        side=facts.side.lower(),
        quantity=facts.quantity,
    )
    _append_order_phase(repo, effect_operation_id, reducing, "ORDER_SUBMIT_REQUESTED")
    try:
        observed = await broker.submit(leg, client_order_id=reducing.client_order_id)
    except BrokerUnavailable as exc:
        fold_uncertain(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            why=str(exc),
        )
        return
    except BrokerError as exc:
        fold_failed(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            summary_code="ORDER_SUBMIT_FAILED",
            reason="The reducing order did not reach the broker.",
            why=str(exc),
        )
        return
    if observed.client_order_id != reducing.client_order_id:
        fold_uncertain(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            why=(
                f"broker returned client_order_id={observed.client_order_id!r}, expected {reducing.client_order_id!r}"
            ),
        )
        return
    fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=observed)


def _append_order_phase(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    order: OrderResource,
    transition_kind: str,
) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order.order_ref,
            broker_order_id=order.broker_order_id,
            broker_state=order.broker_state,
            transition_kind=transition_kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=effect.state,
            clerk_observed_at_ms=repo.clock(),
            summary_code=transition_kind,
            facts_json=canonicalize({}),
        )
    )


def _append_order_phase_once(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    order: OrderResource,
    transition_kind: str,
) -> None:
    if not repo.has_order_transition(order_ref=order.order_ref, transition_kind=transition_kind):
        _append_order_phase(repo, effect_operation_id, order, transition_kind)


def _fold_attributed_flat(repo: ClerkSqliteRepository, effect_operation_id: str, order_ref: str) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="EXIT_ATTRIBUTED_FLAT",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="STOPPED_AND_ATTRIBUTED_FLAT",
            facts_json=canonicalize({}),
        )
    )


def _primary_entry_ref(repo: ClerkSqliteRepository, entries: list[OrderResource]) -> str:
    for entry in entries:
        for transition in repo.transitions_for_order(entry.order_ref):
            if transition["transition_kind"] == "EXIT_ACCEPTED":
                return ExitAcceptedFacts.from_facts_json(transition["facts_json"]).entry_order_ref
    return entries[0].order_ref


def _reducing_order_facts(repo: ClerkSqliteRepository, order_ref: str) -> ExitReducingOrderCreatedFacts:
    for transition in repo.transitions_for_order(order_ref):
        if transition["transition_kind"] == "EXIT_REDUCING_ORDER_CREATED":
            return ExitReducingOrderCreatedFacts.from_facts_json(transition["facts_json"])
    raise AssertionError(f"no reducing-order creation fact for {order_ref!r}")


def _absence_grace_elapsed(repo: ClerkSqliteRepository, order_ref: str) -> bool:
    transitions = repo.transitions_for_order(order_ref)
    return repo.clock() - transitions[0]["recorded_at_ms"] >= UNCERTAIN_SUBMIT_GRACE_MS


def _deterministic_intent_id(effect_operation_id: str) -> str:
    digest = hashlib.sha256(effect_operation_id.encode("utf-8")).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _is_terminal(broker_state: str | None) -> bool:
    return broker_state is not None and broker_state.lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


def _snapshot(repo: ClerkSqliteRepository, effect_operation_id: str) -> ExitSubmission:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    command = repo.get_command(effect.command_id)
    assert command is not None
    orders = repo.orders_for_effect_operation(effect_operation_id)
    entries = [order for order in orders if order.role == "ENTRY"]
    reducing = next((order for order in orders if order.role == "REDUCING"), None)
    return ExitSubmission(
        command=command,
        effect_operation_id=effect_operation_id,
        entry_order_ref=_primary_entry_ref(repo, entries),
        reducing_order_ref=reducing.order_ref if reducing is not None else None,
        created=True,
    )


__all__ = ["resolve_exit"]

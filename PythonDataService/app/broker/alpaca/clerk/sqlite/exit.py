"""Strategy-decision EXIT over the SQLite spine (#1379).

Cancel-first, prove-terminal, attributed flatten — three phases:

- :func:`accept_exit` — purely local, mirrors :func:`accept_enter`'s R1
  fence: no broker call happens inside it, and none may happen before it
  returns. Instead of creating a fresh order row, its ``EXIT_ACCEPTED`` fold
  atomically creates the effect operation (kind ``EXIT``, ``accepted``) and
  reassigns the targeted ``entry_order_ref``'s custody
  (``orders.effect_operation_id``) to this new EXIT — from that instant on,
  every custody_transitions row this module appends against the entry order
  nests under the EXIT's own ``effect_operation_id``, satisfying the pinned
  contract's §6 operation-first timeline. The entry's own ``ENTER_ACCEPTED``
  history is untouched (append-only); only the live FK pointer moves.
- :func:`resolve_exit` — the idempotent, repeatedly-callable state-machine
  step. Each call advances exactly as far as the evidence available right
  now allows, then returns; a caller (an initial :func:`submit_exit`, or a
  later recovery/reconciliation pass) calls it again to continue from
  wherever it left off:

  1. **Cancel the working entry, prove its terminal outcome.** If the entry
     isn't already confirmed terminal (filled/canceled/expired/rejected/
     replaced), claims the operation, attempts ``trade.cancel`` on it (a
     lost response — ``BrokerUnavailable`` — folds ``ORDER_CANCEL_UNCERTAIN``
     and returns; any other ``BrokerError``, e.g. "already resolved,"
     is not itself failure — it just means the cancel request didn't apply,
     so the very next step still discovers the truth), then unconditionally
     polls ``get_order_by_client_order_id`` for the entry's authoritative
     current state and folds whatever it finds through the shared
     :func:`~.order_evidence.fold_order_evidence` gate (idempotent, handles
     a partial fill that raced the cancel exactly the way any other fill
     observation does). If the entry still isn't terminal after that, this
     call returns non-terminal — no reducing quantity is computed, and
     definitely no reducing order is submitted (the acceptance criteria's
     "no closing order until cancellation/fill truth is proven").
  2. **Compute the final attributed quantity.** Only reachable once the
     entry is confirmed terminal. Reads ``positions.attributed_qty`` for
     this bot/symbol — the Clerk-proven current exposure, already reflecting
     any fill folded in step 1 — never a value computed ahead of time. Zero
     remaining quantity skips straight to verification (nothing to close).
  3. **Submit (or resolve) the reducing order.** A fresh reducing order is
     created (``EXIT_REDUCING_ORDER_CREATED``, ``role='REDUCING'``) and
     submitted directly, mirroring :func:`submit_enter`'s own first-attempt
     shape. A *subsequent* call whose reducing order already exists but
     hasn't resolved delegates to :func:`resolve_enter_submission` — which,
     despite its name, is a generic by-``client_order_id`` R4/R7 resolver
     over whatever ``effect_operation_id`` owns the order; reused here
     rather than duplicated, since this EXIT already durably owns that
     order by this point.
  4. **Verify flat, write the precise terminal receipt.** Once the reducing
     order is itself confirmed terminal, re-reads ``positions.attributed_qty``
     one more time and only *then* folds ``EXIT_ATTRIBUTED_FLAT``
     (``succeeded``) if it is genuinely zero — a reducing order that
     resolved without flattening the position folds a precise ``failed``
     instead. Pending/unknown intermediate states are never wrapped as a
     generic success (the acceptance criteria's core rule); only this last
     check ever marks the operation ``succeeded``.

- :func:`submit_exit` = :func:`accept_exit` then :func:`resolve_exit`,
  always — unlike :func:`submit_enter`, there is no "skip the broker" branch
  for a duplicate accept: :func:`resolve_exit` is safe and correct to call
  any number of times by construction, so a transport-retried EXIT decision
  simply continues the same idempotent state machine rather than needing a
  special case.

Deliberately deferred: retrying with a *second* reducing order if the first
one resolves without flattening (residual-quantity retry) is not built here
— a precise ``failed`` receipt is the honest outcome instead, matching the
acceptance criteria's "precise terminal receipt" requirement without
inventing a retry policy this slice's own criteria don't call for.
Conflicting-concurrent-EXIT admission (a second EXIT decision racing the
first for the same entry) gets only a conservative reject
(``idempotency.require_owned_entry_order``), not a full policy — both are
R6/#1380 territory ("uncertainty scope, admission, and recovery actions").
An entry whose *own* ENTER effect is still ``unknown`` when EXIT begins is
the same deferral.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from app.broker.alpaca.clerk.exposure import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.enter import resolve_enter_submission
from app.broker.alpaca.clerk.sqlite.facts import (
    EnterAcceptedFacts,
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    reject_colon,
    require_active_run,
    require_owned_entry_order,
    require_strategy_instance,
)
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_failed,
    fold_order_evidence,
    fold_uncertain,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from app.broker.contract.ports import BrokerTradePort
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref, mint_intent_id

ACTION_EXIT = "EXIT"

__all__ = [
    "ExitSubmission",
    "accept_exit",
    "resolve_exit",
    "submit_exit",
]


@dataclass(frozen=True)
class ExitSubmission:
    command: CommandResource
    effect_operation_id: str | None
    entry_order_ref: str
    reducing_order_ref: str | None
    created: bool  # False for a transport retry / genuine re-request of an existing decision


def _exit_identity(
    *, account_id: str, strategy_instance_id: str, decision_id: str, entry_order_ref: str
) -> tuple[str, str, str, str]:
    """The (idempotency_key, payload_hash, command_id, effect_idempotency_key)
    quadruple one EXIT decision commits under — same shape as ENTER's
    ``_enter_identity`` (pinned contract §3a: ``idempotency_key`` is
    ``{strategy_instance_id}:{decision_id}`` for every ``strategy_decision``,
    not scoped per action; the caller's own decision_id stream is assumed
    unique across both ENTER and EXIT decisions)."""
    idempotency_key = f"{strategy_instance_id}:{decision_id}"
    command_id = f"cmd:{idempotency_key}"
    effect_idempotency_key = f"exit:{idempotency_key}"
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "decision_id": decision_id,
                "action": ACTION_EXIT,
                "entry_order_ref": entry_order_ref,
            }
        ).encode("utf-8")
    ).hexdigest()
    return idempotency_key, payload_hash, command_id, effect_idempotency_key


def accept_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    entry_order_ref: str,
) -> ExitSubmission:
    """Reserve + accept, entirely local (no broker call). R1's fence,
    mirroring :func:`~.enter.accept_enter`.

    Validates the active-run fence (same as ENTER/Stop) and that
    ``entry_order_ref`` is a live ``ENTRY``-role order owned by this bot,
    not already under another active EXIT
    (:func:`~.idempotency.require_owned_entry_order`) — both checked inside
    ``build_transition``, under the write lock, so a concurrent accept can
    never race either check.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("decision_id", decision_id)
    reject_colon("lifecycle_run_id", lifecycle_run_id)

    idempotency_key, payload_hash, command_id, effect_idempotency_key = _exit_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
    )

    def build_transition() -> TransitionInput:
        require_strategy_instance(repo, strategy_instance_id)
        active = require_active_run(repo, strategy_instance_id, lifecycle_run_id)
        require_owned_entry_order(
            repo, strategy_instance_id=strategy_instance_id, entry_order_ref=entry_order_ref
        )
        effect_operation_id = f"effect:{idempotency_key}"
        facts = ExitAcceptedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="strategy_decision",
            action=ACTION_EXIT,
            intended_end_state=None,
            effect_idempotency_key=effect_idempotency_key,
            effect_kind="EXIT",
            decision_id=decision_id,
            entry_order_ref=entry_order_ref,
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=entry_order_ref,
            transition_kind="EXIT_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_ACCEPTED",
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        return ExitSubmission(
            command=outcome.command,
            effect_operation_id=outcome.command.effect_operation_id,
            entry_order_ref=entry_order_ref,
            reducing_order_ref=_reducing_order_ref(repo, outcome.command.effect_operation_id),
            created=False,
        )
    assert isinstance(outcome, CommandCreated)
    return ExitSubmission(
        command=outcome.command,
        effect_operation_id=outcome.command.effect_operation_id,
        entry_order_ref=entry_order_ref,
        reducing_order_ref=None,
        created=True,
    )


async def submit_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    entry_order_ref: str,
    trade: BrokerTradePort,
) -> ExitSubmission:
    """Accept, then always resolve — see the module docstring for why this
    (unlike :func:`~.enter.submit_enter`) has no "skip the broker" branch.

    ``resolve_exit``'s own snapshot always reports ``created=True`` (it has
    no concept of "was the accept fresh" — it only ever advances an
    already-existing operation), so the accurate bit from
    :func:`accept_exit` is grafted onto the final result here.
    """
    accepted = accept_exit(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=lifecycle_run_id,
        entry_order_ref=entry_order_ref,
    )
    assert accepted.effect_operation_id is not None
    resolved = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
    return replace(resolved, created=accepted.created)


async def resolve_exit(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    trade: BrokerTradePort,
) -> ExitSubmission:
    """Advance the EXIT state machine exactly one step's worth of evidence,
    then return — see the module docstring for the full four-step sequence.
    Safe and idempotent to call any number of times; already-terminal is a
    true no-op (no broker call, no new transition).
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    if effect.state in ("succeeded", "failed"):
        return _snapshot(repo, effect_operation_id=effect_operation_id)

    orders = repo.orders_for_effect_operation(effect_operation_id)
    entry_order = next(o for o in orders if o.role == "ENTRY")
    reducing_order = next((o for o in orders if o.role == "REDUCING"), None)

    if not _is_terminal(entry_order.broker_state):
        repo.claim_before_broker_contact(effect_operation_id)
        if entry_order.broker_order_id is not None:
            try:
                await trade.cancel(entry_order.broker_order_id)
            except BrokerUnavailable as exc:
                fold_uncertain(
                    repo,
                    effect_operation_id=effect_operation_id,
                    order_ref=entry_order.order_ref,
                    why=str(exc),
                    transition_kind="ORDER_CANCEL_UNCERTAIN",
                )
                return _snapshot(repo, effect_operation_id=effect_operation_id)
            except BrokerError:
                # e.g. a 422 "not cancelable" because the order already
                # resolved at the broker — not itself a failure; the poll
                # below discovers what actually happened.
                pass

        try:
            polled = await trade.get_order_by_client_order_id(entry_order.client_order_id)
        except BrokerError as exc:
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=entry_order.order_ref,
                why=str(exc),
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )
            return _snapshot(repo, effect_operation_id=effect_operation_id)

        if polled is None or polled.client_order_id != entry_order.client_order_id:
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=entry_order.order_ref,
                why=(
                    "Alpaca has no confirmed status for this client_order_id yet."
                    if polled is None
                    else (
                        f"broker returned client_order_id={polled.client_order_id!r}, "
                        f"expected {entry_order.client_order_id!r}"
                    )
                ),
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )
            return _snapshot(repo, effect_operation_id=effect_operation_id)

        fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=polled)
        entry_order = repo.order(entry_order.order_ref)
        assert entry_order is not None
        if not _is_terminal(entry_order.broker_state):
            return _snapshot(repo, effect_operation_id=effect_operation_id)

    entry_symbol = _entry_symbol(repo, entry_order.order_ref)

    if reducing_order is None:
        remaining_qty = repo.position(effect.strategy_instance_id, entry_symbol)
        if abs(remaining_qty) < POSITION_QTY_EPSILON:
            _fold_attributed_flat(
                repo, effect_operation_id=effect_operation_id, order_ref=entry_order.order_ref
            )
            return _snapshot(repo, effect_operation_id=effect_operation_id)

        reducing_side = "sell" if remaining_qty > 0 else "buy"
        namespace = build_bot_order_namespace(effect.strategy_instance_id)
        reducing_order_ref = build_order_ref(namespace, mint_intent_id())
        facts = ExitReducingOrderCreatedFacts(
            symbol=entry_symbol, side=reducing_side.upper(), quantity=abs(remaining_qty)
        )
        repo.append_transition(
            TransitionInput(
                strategy_instance_id=effect.strategy_instance_id,
                run_id=effect.run_id,
                command_id=effect.command_id,
                effect_operation_id=effect_operation_id,
                order_ref=reducing_order_ref,
                transition_kind="EXIT_REDUCING_ORDER_CREATED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                clerk_observed_at_ms=repo.clock(),
                summary_code="EXIT_REDUCING_ORDER_CREATED",
                facts_json=facts.to_facts_json(),
            )
        )
        reducing_leg = BrokerOrderLeg(symbol=entry_symbol, side=reducing_side, quantity=abs(remaining_qty))
        repo.claim_before_broker_contact(effect_operation_id)
        try:
            order = await trade.submit(reducing_leg, client_order_id=reducing_order_ref)
        except BrokerUnavailable as exc:
            fold_uncertain(
                repo, effect_operation_id=effect_operation_id, order_ref=reducing_order_ref, why=str(exc)
            )
            return _snapshot(repo, effect_operation_id=effect_operation_id)
        except BrokerError as exc:
            fold_failed(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing_order_ref,
                summary_code="ORDER_SUBMIT_FAILED",
                reason="The reducing order did not reach the broker.",
                why=str(exc),
            )
            return _snapshot(repo, effect_operation_id=effect_operation_id)

        if order.client_order_id != reducing_order_ref:
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing_order_ref,
                why=(
                    f"broker returned client_order_id={order.client_order_id!r}, "
                    f"expected {reducing_order_ref!r}"
                ),
            )
            return _snapshot(repo, effect_operation_id=effect_operation_id)

        fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=order)
        reducing_order = repo.order(reducing_order_ref)
        assert reducing_order is not None
    elif reducing_order.broker_order_id is None and not _is_terminal(reducing_order.broker_state):
        # A prior call already created + attempted the reducing order but its
        # outcome was lost/uncertain: resolve_enter_submission is a generic
        # by-client_order_id R4/R7 resolver over whatever effect_operation_id
        # owns the order — reused rather than duplicated, since this EXIT
        # already durably owns it.
        await resolve_enter_submission(repo, order_ref=reducing_order.order_ref, trade=trade)
        reducing_order = repo.order(reducing_order.order_ref)
        assert reducing_order is not None

    if not _is_terminal(reducing_order.broker_state):
        return _snapshot(repo, effect_operation_id=effect_operation_id)

    final_qty = repo.position(effect.strategy_instance_id, entry_symbol)
    if abs(final_qty) < POSITION_QTY_EPSILON:
        _fold_attributed_flat(
            repo, effect_operation_id=effect_operation_id, order_ref=entry_order.order_ref
        )
    else:
        fold_failed(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing_order.order_ref,
            summary_code="EXIT_NOT_FLAT",
            reason="The reducing order resolved without flattening the position.",
            why=(
                f"attributed_qty={final_qty} remains for {entry_symbol!r} after the "
                "reducing order reached a terminal broker state."
            ),
        )
    return _snapshot(repo, effect_operation_id=effect_operation_id)


def _is_terminal(broker_state: str | None) -> bool:
    return broker_state is not None and broker_state.lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


def _entry_symbol(repo: ClerkSqliteRepository, entry_order_ref: str) -> str:
    """The entry order's symbol, derived from its own immutable
    ``ENTER_ACCEPTED`` facts — never a caller-supplied parameter (the entry
    already recorded it once; EXIT reads history rather than re-asking)."""
    for transition in repo.transitions_for_order(entry_order_ref):
        if transition["transition_kind"] == "ENTER_ACCEPTED":
            facts = EnterAcceptedFacts.from_facts_json(transition["facts_json"])
            return facts.leg["symbol"]
    raise AssertionError(f"no ENTER_ACCEPTED transition found for {entry_order_ref!r}")


def _reducing_order_ref(repo: ClerkSqliteRepository, effect_operation_id: str | None) -> str | None:
    if effect_operation_id is None:
        return None
    reducing = next(
        (o for o in repo.orders_for_effect_operation(effect_operation_id) if o.role == "REDUCING"), None
    )
    return reducing.order_ref if reducing is not None else None


def _fold_attributed_flat(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, order_ref: str
) -> None:
    """``order_ref`` is the entry order — the terminal-flat proof always
    relates conceptually to closing it out, whether or not a reducing order
    was ever needed (the zero-remaining-quantity path has none), so this
    also gives the entry's own per-order timeline a visible closing event."""
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


def _snapshot(repo: ClerkSqliteRepository, *, effect_operation_id: str) -> ExitSubmission:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    command = repo.get_command(effect.command_id)
    assert command is not None
    orders = repo.orders_for_effect_operation(effect_operation_id)
    entry = next(o for o in orders if o.role == "ENTRY")
    reducing = next((o for o in orders if o.role == "REDUCING"), None)
    return ExitSubmission(
        command=command,
        effect_operation_id=effect_operation_id,
        entry_order_ref=entry.order_ref,
        reducing_order_ref=reducing.order_ref if reducing is not None else None,
        created=True,
    )

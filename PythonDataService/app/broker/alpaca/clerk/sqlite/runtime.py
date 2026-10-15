"""Runtime facade for the activated SQLite Alpaca Account Clerk.

Strategy code speaks in semantic ENTER/EXIT decisions and lifecycle runs.  It
never receives the repository or a broker port, and therefore cannot mint an
order identity or bypass capture-before-contact custody.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal, Protocol

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    authority_kind_for_account,
    require_real_account_id,
    require_shadow_account_id,
    require_synthetic_account_id,
)
from app.broker.alpaca.clerk.active_protocol import ClerkAdmissionSnapshotStaleError
from app.broker.alpaca.clerk.budgets import BudgetUnavailable, entry_requirement
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.exit_terms import (
    ExitTerms,
    policy_with_exit_terms,
    upgrade_exit_terms,
)
from app.broker.alpaca.clerk.live_arming import latest_arming
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED, LiveEnvelopeGate
from app.broker.alpaca.clerk.models import (
    CLERK_CUSTODY_BROKER_UNREACHABLE,
    AccountFreezeState,
    ChannelHealth,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    EffectOperationReceipt,
    EffectOperationState,
    EffectPurpose,
    HoldState,
    InstanceCustodyProof,
    ReconciliationCut,
    ReconciliationVerdict,
    RecoveryEvaluationObservation,
)
from app.broker.alpaca.clerk.option_effects import (
    OptionEffectRefusal,
    OptionEntryEvidence,
    OptionExitEvidence,
    OptionPlanRefused,
    is_option_plan,
    option_plan_taken,
)
from app.broker.alpaca.clerk.program_leg import (
    PRICING_BAR_MISMATCH,
    PRICING_BAR_UNAVAILABLE,
    LegRefusal,
    ProgramLegPolicy,
    ProgramLegRefused,
    shape_program_leg,
)
from app.broker.alpaca.clerk.recovery_reduction import (
    SIMULATED_RECOVERY_PRICE_UNAVAILABLE,
    UNPRICEABLE_RECOVERY,
    ConfirmedRecoveryLimit,
    ConfirmedRecoveryShape,
    QuoteSource,
    RecoveryPricing,
    RecoveryReductionPricing,
    RegularSessionReduction,
    flatten_send_verdict,
    market_leg_sendable,
    price_recovery_reduction,
    recovery_reduction_shape,
)
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.simulated_option_settlement import option_settlement_simulation
from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    GuardedBrokerTradePort,
    guard_broker_ports,
)
from app.broker.alpaca.clerk.sqlite.budget_commands import require_deploy_admissible, submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import (
    CommandSubmission,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ContractTerms,
    ContractTermsConflict,
    is_option_contract_symbol,
)
from app.broker.alpaca.clerk.sqlite.decision_receipts import AtomicDecisionReceipt
from app.broker.alpaca.clerk.sqlite.economic_projection_models import MarketMark
from app.broker.alpaca.clerk.sqlite.enter import (
    MARKET_CLOSED,
    MARKET_LIVENESS_BLOCKED,
    EnterSubmission,
    EntrySubmissionRefusal,
    OptionEntryProofWindowClosed,
    accept_enter,
    accept_option_enter,
    enter_command_id,
    submit_accepted_enter,
)
from app.broker.alpaca.clerk.sqlite.entry_buying_power import (
    EntryBuyingPowerWatch,
    review_under_reserved_hold,
)
from app.broker.alpaca.clerk.sqlite.entry_outcome import (
    ENTRY_OUTCOME_LIMIT_TOO_FAR,
    MAX_ENTRY_OUTCOME_WAIT_MS,
    EntryOnTheBooks,
    EntryOutcomeWaitRefused,
    SettledEntry,
    entry_outcome,
    recorded_settlement,
    settled_now,
)
from app.broker.alpaca.clerk.sqlite.exit import (
    OPTION_CLOSE_TAKES_NO_LIMIT,
    accept_exit,
    newest_reducible_entry,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    cancel_and_prove_owned_entry,
    expired_option_close,
    note_expired_option_position,
    option_close_unpriced,
    option_flatten_waits_for_open,
)
from app.broker.alpaca.clerk.sqlite.facts import DecisionPricingFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import (
    HistoricalExecutionRecoveryPlan,
    HistoricalExecutionRecoveryRefused,
    confirm_historical_execution_recovery,
    prepare_historical_execution_recovery,
    replay_historical_execution_recovery,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError, UnknownEntryOrderError
from app.broker.alpaca.clerk.sqlite.intake_fence import (
    IntakeFencePoisonedError,
    IntakeFenceYieldError,
    ReentrantAsyncLock,
)
from app.broker.alpaca.clerk.sqlite.lane_quiet import (
    AccountQuietObservation,
    observe_account_quiet,
)
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import EntryReadingOutcome
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancellationSubmission,
    submit_manual_order_cancellation,
    submit_manual_ticket_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_order_runtime import (
    ManualOrderCapability,
    ManualOrderPreview,
    get_manual_ticket,
    manual_order_capability,
    preview_manual_order,
    submit_previewed_manual_order,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualOrderSubmission, ManualTicketLeg
from app.broker.alpaca.clerk.sqlite.models import (
    CommandResource,
    ControlMetaSnapshot,
    ExecutionCoverageResolutionReceipt,
    ManualOrderTicketResource,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.option_admission import (
    ENTRY_CLOSE_TERMS,
    HeldByAnother,
    OptionEntryRefused,
    entry_past_cutoff,
    entry_quote_refusal,
    held_on_the_other_side,
    work_out_option_entry,
)
from app.broker.alpaca.clerk.sqlite.option_decision_fills import OptionDecisionFill, option_fills_of_decision
from app.broker.alpaca.clerk.sqlite.option_entry import (
    MultiLegOptionEntryFacts,
    OptionEntry,
    option_entry_contracts,
    option_entry_of_order,
)
from app.broker.alpaca.clerk.sqlite.option_event_acknowledgement import (
    HoldAcknowledgementReceipt,
    HoldAcknowledgementRequest,
)
from app.broker.alpaca.clerk.sqlite.option_events import (
    OwnerEndingReceipt,
    acknowledge_option_event_hold_after_a_pass,
    end_option_at_the_owners_word,
)
from app.broker.alpaca.clerk.sqlite.option_owner_ending import OwnerEndingRequest
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    is_working_order,
    trade_port_folds_simulated_evidence,
    unresolved_order_refs,
    working_order_refs,
)
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ProjectedUnknownExitOrder,
    SafeFlattenPlan,
    SafeFlattenPlanLeg,
)
from app.broker.alpaca.clerk.sqlite.reconcile import (
    AccountReconciliationResult,
    Trigger,
)
from app.broker.alpaca.clerk.sqlite.reconcile import (
    reconcile_account as reconcile_sqlite_account,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    SWEEP_LIVENESS_BOUND_MS,
    PublishedPass,
    RecoveryPolicyContext,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import (
    ResidueDischargeReceipt,
    discharge_attributed_residue,
)
from app.broker.alpaca.clerk.sqlite.risk_admission import AccountReadingBehindExecutions
from app.broker.alpaca.clerk.sqlite.run_ownership import RunOwner, RunOwnership
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    SafeFlattenResult,
    execute_safe_flatten_plan,
)
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import FAILED_ENTER_FILLED_REASON_CODE
from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import (
    AttestationFindings,
    OrderOutcomeAttestation,
    UnknownOrderSettlementReceipt,
    check_unknown_exit_order,
    settle_unknown_exit_order,
)
from app.broker.alpaca.clerk.stream_health import (
    STREAM_HEALTH_REASON_CODE,
    StreamHealthGate,
    stream_health_refusal,
)
from app.broker.alpaca.live_option_bots import live_option_bots_enabled
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.alpaca.option_buying_power_judged import option_buying_power_judged
from app.broker.contract.errors import BrokerError, BrokerOrderRejected
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide, OrderType
from app.broker.contract.options import (
    BrokerOptionContract,
    BrokerOptionOrder,
    OptionOrderRequest,
    option_contract_refusal,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.config import settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.contract import OptionContract
from app.schemas.action_plan import ActionPlan, StockEntryLeg
from app.services.market_liveness import (
    MarketEntryPolicy,
    market_liveness_fact,
    prepared_top_of_book,
)
from app.services.session_authority import SessionAuthorityState
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.models import ExitSubmission
    from app.broker.alpaca.clerk.synthesized_orders import SynthesizedAnchor, SynthesizedOptionFill
    from app.options.quotes import HeldQuote
    from app.services.bot_binding_repository import BrokerBotBinding
    from app.services.source_bar_ledger import RetainedSourceBar

_ENCODED_DECISION_PREFIX = "encoded-"
# The authorities whose fills are synthesized from evidence, so every effect
# must carry the exact retained decision bar it was priced from.
_BAR_BOUND_AUTHORITIES: Final = frozenset({"synthetic", "shadow"})
# The authorities that *are*, or stand in for, the live Clerk: their ENTER is
# rechecked against the live market clock. The shadow authority is here
# because it must reproduce every refusal the live Clerk would make, or the
# paper twin stops reconciling trade-for-trade (ADR 0059 D2). Deliberately
# not `_BAR_BOUND_AUTHORITIES` -- that set is {synthetic, shadow} and means
# the opposite thing (fills synthesized from evidence).
_LIVE_MARKET_CLOCK_AUTHORITIES: Final = frozenset({"sqlite", "shadow"})
# The authorities that fill an option order themselves, from quotes, and send nothing: a Dry Run's
# and a Live Shadow's (ADR 0076 decision 25). Each takes an option plan, and each has the Clerk's own
# keeping ask for the lines of what its bots hold.
_OPTION_PLAN_SIMULATIONS: Final = frozenset({"synthetic", "shadow"})
# An EXIT reduces the program's own position, so its leg is the opposite side.
_REDUCING_SIDE: Final = {OrderSide.BUY: OrderSide.SELL, OrderSide.SELL: OrderSide.BUY}
# A refused entry is dropped, never queued: the bot's next bar decides afresh.
_ENTRY_NOT_RETRIED: Final = "No order was sent. The bot decides again on its next bar."
_MARKET_ENTRY_AFTER_THE_CLOSE: Final = (
    "The regular session has closed, or closes within seconds. A market order sent now "
    "could reach the broker after the close and wait there until the next open."
)
# An option order is for the day and for regular hours, whatever its type (ADR 0076).
_OPTION_ORDER_OUTSIDE_THE_SESSION: Final = (
    "The regular session has closed, or closes within seconds. An option order is sent only inside "
    "the regular session."
)
# How often a wait for an option entry's outcome reads the books again (ADR 0076).
_ENTRY_OUTCOME_POLL_S: Final = 0.2
# The longest a Paper entry waits for the broker's listing of its contract. The entry's
# quotes are good for five seconds and age while the read is out: longer than this and
# the entry is refused for the listing, by name, not for quotes the wait itself made stale.
CONTRACT_REFERENCE_READ_BOUND_MS = 3_000
# What comes back for an entry the broker's listing was not asked about, because the entry
# was to be refused without it: the session was closed, or a channel unhealthy, when it came.
# Returned as the entry's refusal only if neither is so by the time the entry is judged.
_LISTING_NOT_ASKED: Final = OptionEntryRefused(
    OptionEffectRefusal.CONTRACT_REFERENCE_UNREAD.value,
    "Alpaca's listing of the contract was not read: when the entry reached the account Clerk the session was "
    "closed or a channel was unhealthy, and an entry is not taken in a contract whose listing was not read.",
)
logger = logging.getLogger(__name__)


def _listing_unread(named: str, why: object) -> OptionEntryRefused:
    """The refusal of an entry whose contract's listing the broker could not answer."""
    return OptionEntryRefused(
        OptionEffectRefusal.CONTRACT_REFERENCE_UNREAD.value,
        f"Alpaca's listing of {named} could not be read ({why}), so the contract was not "
        "confirmed and the entry was not taken.",
    )


class EntryReading(Protocol):
    """Wait for an account reading covering every execution (``LiveEnvelopeSync.read_for_entry``)."""

    async def __call__(self, *, until_ms: int, wanted: Callable[[], bool]) -> EntryReadingOutcome: ...


@dataclass(frozen=True)
class _AwaitReading:
    """An ENTER refused because executions postdate the last account reading (#2623).

    Returned from under the intake fence so the account is read outside it;
    the decision is then judged again from the start. ``until_ms`` is the
    decision's own time limit (``EffectDecisionEvidence.decision_valid_until_ms``).
    """

    until_ms: int


# Why an ENTER's wait for an account reading ended without it (#2623): one
# entry per ending, in the owner's words on the ENTER's ``blocked`` receipt.
_ENTRY_WAIT_ENDINGS: Final[Mapping[str, str]] = {
    "run_stopped": (
        "Executions changed after the last account reading, and the bot was stopped "
        "while this entry waited for a new reading, so the entry was dropped."
    ),
    "unread": (
        "Executions changed after the last account reading, and the account could not "
        "be read again, so this entry was dropped."
    ),
    "out_of_time": (
        "Executions changed after the last account reading, and no newer reading arrived "
        "while this decision was still on time, so the entry was dropped."
    ),
    "stopped": (
        "Executions changed after the last account reading, and the account Clerk was "
        "shutting down before a new reading arrived, so the entry was dropped."
    ),
}


def _entry_wait_ending(outcome: EntryReadingOutcome, *, run_active: bool) -> str | None:
    """The key of why an ENTER's wait ended without it, or ``None`` to judge it again.

    A stopped run comes first: whatever the reading found, an entry the run
    no longer owns is never judged again.
    """
    if not run_active:
        return "run_stopped"
    return None if outcome == "covered" else outcome


class _PricingBarBoundTradePort:
    """Bind each synthetic Clerk order to the one immutable retained bar that prices it.

    ``decided_at_ms`` is the decision bar's close. A pricing bar that closed
    before it is an older price: the fill is stamped at the decision's close,
    at the older bar's price, and only a market order may fill that way.
    """

    def __init__(self, inner: GuardedBrokerTradePort, retained_bar: RetainedSourceBar, *, decided_at_ms: int) -> None:
        self._inner = inner
        self._retained_bar = retained_bar
        self._decided_at_ms = decided_at_ms

    @property
    def broker_id(self) -> str:
        return self._inner.broker_id

    @property
    def submission_response_is_authoritative_evidence(self) -> bool:
        """Binding a bar changes the price, never whose execution this is: the no-submit port says."""
        return self._inner.submission_response_is_authoritative_evidence

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        if leg.extended_hours and self._retained_bar.end_ms < self._decided_at_ms:
            # The sale was accepted while a market order could still go out and
            # is being sent after it no longer can, re-priced as a limit to
            # rest. An older price anchors no limit, so the send is refused as
            # a broker would refuse it, and the EXIT's own recovery takes over.
            raise BrokerOrderRejected(
                "The simulation priced this sale from an earlier minute and will not rest a limit at that price.",
                broker=self._inner.broker_id,
            )
        self._inner.bind_evaluated_bar(client_order_id, self._retained_bar, fill_at_ms=self._decided_at_ms)
        return await self._inner.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        await self._inner.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return await self._inner.get_order_by_client_order_id(client_order_id)

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        return await self._inner.get_order_by_broker_order_id(order_id)

    # No bar is bound to an option order: one is filled from a quote, which
    # ``_OptionQuoteBoundTradePort`` binds, never from a bar (ADR 0076). The
    # wrapped port's answer is passed on as it is, so that its answer or its
    # refusal never becomes a missing method here.

    async def submit_option_order(self, order: OptionOrderRequest, *, client_order_id: str) -> BrokerOptionOrder:
        return await self._inner.submit_option_order(order, client_order_id=client_order_id)

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        return await self._inner.get_option_order_by_client_order_id(client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        return await self._inner.get_option_order_by_broker_order_id(order_id)


class _OptionQuoteBoundTradePort:
    """Bind a simulated option entry to the quotes its limit price was worked out from (ADR 0076).

    The option twin of ``_PricingBarBoundTradePort``: each leg's quote is
    bound to the order's own id just before the order is sent, so the
    simulation fills every leg from the quote the entry was admitted on and
    from no other. Each contract's terms go with its quote, as the books
    record them: the simulation holds no listing, and counts the position's
    dollars by their multiplier. One leg is one binding; an order of several
    legs binds one for each, under the one order id.
    """

    def __init__(
        self,
        inner: GuardedBrokerTradePort,
        *,
        terms: ContractTerms,
        quote: HeldQuote,
        other_legs: Sequence[tuple[ContractTerms, HeldQuote]] = (),
    ) -> None:
        self._inner = inner
        # The first leg, and for an order of several legs each leg after it, in the order's leg order.
        self._priced_from = ((terms, quote), *other_legs)

    @classmethod
    def of_entry(cls, inner: GuardedBrokerTradePort, entry: OptionEntry) -> _OptionQuoteBoundTradePort:
        """The port that binds every leg of ``entry`` to the quote that leg was priced from."""
        (first, *others) = (
            (terms, leg.quote.held) for terms, leg in zip(entry.leg_terms, entry.legs, strict=True)
        )
        return cls(inner, terms=first[0], quote=first[1], other_legs=others)

    @property
    def broker_id(self) -> str:
        return self._inner.broker_id

    @property
    def submission_response_is_authoritative_evidence(self) -> bool:
        """Binding a quote changes the price, never whose execution this is: the no-submit port says."""
        return self._inner.submission_response_is_authoritative_evidence

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return await self._inner.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        await self._inner.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return await self._inner.get_order_by_client_order_id(client_order_id)

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        return await self._inner.get_order_by_broker_order_id(order_id)

    async def submit_option_order(self, order: OptionOrderRequest, *, client_order_id: str) -> BrokerOptionOrder:
        for terms, quote in self._priced_from:
            self._inner.bind_option_entry_quote(
                client_order_id, symbol=terms.symbol, quote=quote, multiplier=terms.multiplier
            )
        return await self._inner.submit_option_order(order, client_order_id=client_order_id)

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        return await self._inner.get_option_order_by_client_order_id(client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        return await self._inner.get_option_order_by_broker_order_id(order_id)


class StrategyRegistrationConflictError(RuntimeError):
    """One strategy identity was reused with different immutable semantics."""


class DeployBudgetRequiredError(BudgetUnavailable):
    """A run was registered with no budget: a Deploy sets one aside or it does not start (#3216)."""

    reason_code = "deploy_budget_required"


class SealedAccountMismatchError(RuntimeError):
    """A deployment seal names an account other than this Clerk repository."""

    reason_code = "SEALED_ACCOUNT_MISMATCH"


class StrategyAdmissionStaleError(ClerkAdmissionSnapshotStaleError):
    """A Start or Resume snapshot no longer matches SQLite Clerk authority."""


class StartupBrokerTruthUnavailable(RuntimeError):
    """Startup recovery's reconciliation could not read fresh broker truth.

    ``broker_error`` is the broker's own error when the read failed -- the one
    fact the startup selector needs to tell an unreachable Alpaca (retried)
    from a read that answered but could not be proven (terminal) (#2582).
    """

    def __init__(self, broker_error: BrokerError | None) -> None:
        self.broker_error = broker_error
        super().__init__(
            "Startup recovery could not prove this account's orders and positions against Alpaca."
            if broker_error is None
            else f"Startup recovery could not read this account's orders and positions from Alpaca: {broker_error}"
        )


@dataclass(frozen=True)
class _PublishedReconciliation:
    """A reconciliation verdict bound to the instant it was observed.

    ``observed_at_ms`` is when the verdict was reached
    (``AccountReconciliationResult.verdict_at_ms``), never when it was
    published here: the Clerk can record more between the two, and a pass
    dated at its publish would read that as older than a comparison that
    never saw it (#2839). A pass that reached no verdict has only its publish
    to be dated by.

    ``cut`` names the ledger point the verdict's final broker comparison saw
    through (``AccountReconciliationResult.through_sequence``); ``None`` for a
    pass that reached no verdict.
    """

    result: AccountReconciliationResult
    observed_at_ms: int
    cut: ReconciliationCut | None = None


class SqliteAlpacaClerkFacade:
    """Narrow live-control surface backed by one account repository."""

    authority_kind: Literal["sqlite", "synthetic", "shadow"]
    broker_id = "alpaca"
    supports_revision_bound_admission = True

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        stream_health: StreamHealthGate | None = None,
        intake: ReentrantAsyncLock | None = None,
        authority_kind: Literal["sqlite", "synthetic", "shadow"] = "sqlite",
        account_mode: Literal["paper", "live"],
        program_leg_policy: ProgramLegPolicy | None = None,
        live_envelope: LiveEnvelopeGate | None = None,
        quote_source: QuoteSource | None = None,
        entry_reading: EntryReading | None = None,
        entry_deadline_nudge: Callable[[], None] | None = None,
        simulation: SimulatedAccountProjection | None = None,
    ) -> None:
        if authority_kind == "synthetic":
            require_synthetic_account_id(repo.account_id)
            if account_mode != "paper":
                raise AccountAuthorityIdentityError("a synthetic authority is a paper environment")
        elif authority_kind == "shadow":
            require_shadow_account_id(repo.account_id)
            if account_mode != "live":
                raise AccountAuthorityIdentityError("a shadow authority reads a live account")
        else:
            require_real_account_id(repo.account_id)
            # ADR 0059 D4: a real-money facade is never a permissive default.
            # The live selector composes its envelope; a live facade without
            # one is a composition bug, refused here. No arming gate is
            # composed any more (#2629): only a budgeted deployment admits an
            # ENTER, and an account on authority version 1 refuses every one.
            if account_mode == "live" and live_envelope is None:
                raise AccountAuthorityIdentityError(
                    "a live sqlite authority requires a risk envelope"
                )
        self._repo = repo
        self._simulation = simulation
        self._intake = intake or ReentrantAsyncLock()
        self._read, self._trade = guard_broker_ports(read=read, trade=trade, intake=self._intake)
        self._stream_health = stream_health
        self.authority_kind = authority_kind
        # The environment every custody answer names (ADR 0054): what the
        # caller positively learned from the broker at activation, never a
        # guess from the account-id shape.
        self._account_mode = account_mode
        # ADR 0059 D5.3: what this authority may shape an extended-session leg
        # from. Passed in, never read off the ports here — the composition root
        # owns which capabilities and which operator allowances apply.
        self._program_leg_policy = program_leg_policy or ProgramLegPolicy.regular_only()
        # ADR 0059 D4: the risk envelope every ENTER is judged by -- it
        # refuses one on an account not switched to budgets (#2553) and bounds
        # the rest by their deployment's budget. Every account authority
        # composes one; ``None`` only on a store no authority composed
        # (test fixtures). Composed by the authority selector,
        # never built here.
        self._live_envelope = live_envelope
        # #2623: how an ENTER refused only because executions postdate the
        # last account reading asks for a reading now instead of waiting for
        # the envelope's 15 s cadence. Composed beside the envelope's sync;
        # ``None`` where no sync reads the account, and such an ENTER is
        # dropped as before.
        self._entry_reading = entry_reading
        # ADR 0076: wakes the deadline keeper when an option entry is accepted,
        # so its cancel time is known to the keeper at once. It carries no
        # meaning: lost, the keeper's own wake and the next pass still find
        # the entry. ``None`` where no keeper is composed.
        self._entry_deadline_nudge = entry_deadline_nudge
        # The pause between two reads of an option entry's outcome.
        self._outcome_pause: Callable[[float], Awaitable[None]] = asyncio.sleep
        # ADR 0076 decision 26: the readings of the account's option buying power that a real
        # account's option entries are compared on. ``None`` on a simulation, which has no broker
        # figure to read. An account whose bots trade shares never asks it for anything.
        self._buying_power: EntryBuyingPowerWatch | None = (
            EntryBuyingPowerWatch(
                repo=repo, read=self._read, intake=self._intake,
                judged=option_buying_power_judged,
            )
            if authority_kind == "sqlite" and not trade_port_folds_simulated_evidence(trade)
            else None
        )
        self._buying_power_round: asyncio.Task[tuple[str, ...]] | None = None
        # #2007: the live IBKR bid/ask an operator's extended-hours flatten is
        # priced against -- the process's market-liveness store unless a test
        # states the quote directly.
        self._quote_source = quote_source or prepared_top_of_book
        self._effect_tasks: dict[tuple[str, str], asyncio.Task[EffectOperationReceipt]] = {}
        # Latest verdict from the reconciliation sweep -- the sole automatic
        # reconciler (#1776). Panel reads project this instead of forcing
        # their own reconciliation. Process-local by design: it is a cache of
        # an observation, not a custody fact, so it never enters the ledger.
        #
        # Verdict and observation time are one value: read separately, a
        # concurrent reconciliation can replace the timestamp between the two
        # reads and the snapshot reports one verdict with another pass's time.
        self._last_published: _PublishedReconciliation | None = None
        # #1808: anchors the recovery-evaluation episode Start/Resume
        # admission reads passively, so a post-outage unresolved-intent count
        # can present as "the sweep is still evaluating" instead of
        # "intervene" while this authority is young.
        self._evaluation_started_at_ms = repo.clock()
        # Stamped only by publish_sweep_reconciliation — the periodic sweep's
        # own wiring — never by one-off admission/action reconciliations that
        # also publish. A custody-guard pass during a mutating Start/Resume
        # must not read as evidence the background sweep is alive.
        self._last_sweep_pass_completed_at_ms: int | None = None
        # #2369: which in-process runner holds each ACTIVE run this facade
        # admitted. Read and written only under ``self._intake``.
        self._run_ownership = RunOwnership()
        self._exit_terms = {
            instance["strategy_instance_id"]: terms
            for instance in self._repo.strategy_instances()
            if (terms := self._repo.exit_terms(instance["strategy_instance_id"])) is not None
        }

    @property
    def account_id(self) -> str:
        return self._repo.account_id

    @property
    def repository(self) -> ClerkSqliteRepository:
        """Read-model integration seam; strategy callers never receive this."""
        return self._repo

    @property
    def live_envelope(self) -> LiveEnvelopeGate | None:
        """The envelope this authority admits ENTERs against, if it has one."""
        return self._live_envelope

    @property
    def account_mode(self) -> Literal["paper", "live"]:
        """The environment every custody answer names (ADR 0054), learned at activation."""
        return self._account_mode

    def simulated_marks(self, *, strategy_instance_id: str, now_ms: int) -> tuple[ControlMetaSnapshot, dict[str, MarketMark]] | None:
        """The composed simulator's custody-fenced marks; absent on real accounts."""
        return None if self._simulation is None else self._simulation.current_marks(strategy_instance_id=strategy_instance_id, now_ms=now_ms)

    def synthesized_fill_anchors(self) -> Mapping[str, SynthesizedAnchor]:
        """How this authority priced the fills no venue made, by order ref (#2912).

        Read-model seam for the bot page. A Paper or Live authority's fills
        come from Alpaca, so it has none: an empty answer says "nothing on
        record" only where ``simulates_fills`` is true.
        """
        return self._trade.synthesized_fill_anchors()

    @property
    def intake(self) -> ReentrantAsyncLock:
        return self._intake

    @property
    def run_ownership(self) -> RunOwnership:
        """The book the reconciliation sweep retires runner-less runs from (#2369)."""
        return self._run_ownership

    @property
    def recovery_pricing(self) -> RecoveryPricing:
        """The one seam this authority prices an automatic reduction from.

        Every path that can drive an EXIT hands it to ``resolve_exit`` — the
        deciding runner, restart recovery, the reconciliation sweep (and its
        stuck-EXIT watchdog, which prices its extended-hours re-drives from it,
        #2229), operator Reconcile Now and the operator's safe flatten — so an
        EXIT whose leg can no longer go out as recorded is re-priced, or folds,
        the same way whichever of them drives it (#2440).

        The policy is resolved per read (the envelope in force, never a
        boot-time snapshot) and the quote is the same live top-of-book read
        the operator's safe flatten rides — IBKR via the market-liveness
        store, never Alpaca market data (#2229). A synthetic authority prices
        nothing: it fills against retained source bars rather than the live
        market, so pricing its reductions off live quotes would couple it to a
        market it does not execute in (PR #2230 review); such an EXIT folds
        for the operator instead.
        """
        if self.authority_kind == "synthetic":
            return UNPRICEABLE_RECOVERY
        return RecoveryPricing(
            quote_source=self._quote_source,
            liveness_source=market_liveness_fact,
            policy_for=self.exit_policy_for_instance,
        )

    @property
    def program_leg_policy(self) -> ProgramLegPolicy:
        """The account's entry policy, refreshed from the current envelope.

        Exit allowance, band and spread cap come exclusively from the bot's
        immutable custody seal through ``exit_policy_for_instance``.
        """
        policy = self._program_leg_policy
        if self._live_envelope is not None and self._live_envelope.values is not None:
            values = self._live_envelope.values
            return replace(policy, allowance_refusal=None, allowances=ExtendedHoursAllowances(
                entry_bps=Decimal(str(values.xh_entry_bps)), exit_bps=None,
            ))
        return policy if policy.allowances is None else replace(
            policy, allowances=replace(policy.allowances, exit_bps=None),
        )

    def upgrade_legacy_exit_terms(self, arming_ledger: LiveArmingLedger | None = None) -> None:
        """Seal each legacy bot from its own arming history, else the effective revision.

        Historical arming is read here only to price a bot armed before exit
        terms existed, from its own newest arming, once; it grants nothing
        (#2629).
        """
        if self._repo.exit_terms_upgrade_completed():
            return
        records = ()
        if arming_ledger is not None:
            try:
                records = tuple(arming_ledger.records())
            except (OSError, ValueError):
                logger.error("Legacy exit terms cannot read arming history; the effective revision supplies the allowance",
                             extra={"account_id": self.account_id, "action": "exit_terms_upgrade_arming_unreadable"}, exc_info=True)

        def policy_for(sid: str) -> ProgramLegPolicy:
            policy = self._program_leg_policy
            own_seal = latest_arming(tuple(row for row in records if row.strategy_instance_id == sid))
            values = own_seal.envelope if own_seal is not None else (
                self._live_envelope.values if self._live_envelope is not None else None
            )
            if values is not None:
                policy = replace(policy, allowance_refusal=None, allowances=ExtendedHoursAllowances.from_envelope(values))
            return policy

        upgrade_exit_terms(self._repo, policy_for)
        self._exit_terms = {instance["strategy_instance_id"]: self._repo.exit_terms(instance["strategy_instance_id"])
                            for instance in self._repo.strategy_instances()}

    def exit_terms_for_instance(self, sid: str) -> ExitTerms | None:
        return self._exit_terms.get(sid)

    def exit_policy_for_instance(self, sid: str) -> ProgramLegPolicy:
        """Use cached immutable custody terms; never read files or SQLite on a price tick."""
        return policy_with_exit_terms(self.program_leg_policy, self._exit_terms.get(sid))

    @property
    def simulates_fills(self) -> bool:
        """Whether this authority fills its own orders from retained bars.

        True for the two synthesized-custody worlds (``sim:`` and ``shadow:``).
        Every effect there must carry its decision bar to price the fill.
        """
        return self.authority_kind in _BAR_BOUND_AUTHORITIES

    def channel_health_snapshot(
        self,
        symbol: str | None = None,
    ) -> tuple[ChannelHealth, ChannelHealth] | None:
        """Expose the exact submission-gate facts to retained status surfaces."""
        if self._stream_health is None:
            return None
        return self._stream_health.snapshot(symbol)

    async def manual_order_capability(self) -> ManualOrderCapability:
        """Return the server-owned policy gate for the manual market tracer."""
        return await manual_order_capability(
            read=self._read,
            stream_health=self._stream_health,
            manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
            account_id=self.account_id,
        )

    async def prepare_historical_execution_recovery(
        self,
        *,
        context: RecoveryPolicyContext,
        concurrency_token: str,
    ) -> HistoricalExecutionRecoveryPlan:
        """Read paper broker evidence outside the Clerk intake boundary."""
        return await prepare_historical_execution_recovery(
            repo=self._repo,
            read=self._read,
            context=context,
            concurrency_token=concurrency_token,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
        )

    async def confirm_historical_execution_recovery(
        self,
        *,
        plan: HistoricalExecutionRecoveryPlan,
        confirmation_token: str,
    ) -> ExecutionCoverageResolutionReceipt:
        """Append only the signed plan's exact evidence and closed resolution."""
        replay = await asyncio.to_thread(
            replay_historical_execution_recovery,
            repo=self._repo,
            plan=plan,
            confirmation_token=confirmation_token,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
        )
        if replay is not None:
            return replay
        try:
            account = await self._read.get_account()
        except BrokerError as exc:
            raise HistoricalExecutionRecoveryRefused(
                "HISTORICAL_EVIDENCE_UNAVAILABLE",
                "Alpaca account evidence is temporarily unavailable. Keep the exposure blocked and retry later.",
            ) from exc
        except Exception as exc:
            raise HistoricalExecutionRecoveryRefused(
                "HISTORICAL_EVIDENCE_UNAVAILABLE",
                "Alpaca account evidence is temporarily unavailable. Keep the exposure blocked and retry later.",
            ) from exc
        return await asyncio.to_thread(
            confirm_historical_execution_recovery,
            repo=self._repo,
            plan=plan,
            confirmation_token=confirmation_token,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
            observed_account=account,
        )

    async def preview_manual_order(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        legs: tuple[ManualTicketLeg, ...],
    ) -> ManualOrderPreview:
        """Bind one browser-stable ticket leg to current SQLite authority facts."""
        return await preview_manual_order(
            repo=self._repo,
            read=self._read,
            stream_health=self._stream_health,
            manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
            account_id=self.account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            legs=legs,
        )

    async def submit_manual_order(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        legs: tuple[ManualTicketLeg, ...],
        preview_token: str,
        continuation: bool = False,
    ) -> ManualOrderSubmission:
        """Accept locally, then drive the previewed manual leg outside intake."""
        return await submit_previewed_manual_order(
            repo=self._repo,
            read=self._read,
            trade=self._trade,
            stream_health=self._stream_health,
            manual_trading_enabled=settings.ALPACA_SQLITE_MANUAL_TRADING_ENABLED,
            control_secret=settings.DATA_PLANE_CONTROL_SECRET,
            allow_unauthenticated_control=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
            account_id=self.account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            legs=legs,
            preview_token=preview_token,
            continuation=continuation,
        )

    def manual_order_ticket(self, ticket_id: str) -> ManualOrderTicketResource | None:
        """Read one durable ticket without presenting repository mutation access."""
        return get_manual_ticket(self._repo, ticket_id)

    async def cancel_manual_order(
        self,
        *,
        operator_id: str,
        order_ref: str,
        cancel_request_id: str,
    ) -> ManualOrderCancellationSubmission:
        """Accept and reconcile one durable manual-order cancellation."""
        return await submit_manual_order_cancellation(
            self._repo,
            account_id=self.account_id,
            operator_id=operator_id,
            order_ref=order_ref,
            cancel_request_id=cancel_request_id,
            trade=self._trade,
        )

    async def cancel_manual_ticket(
        self,
        *,
        operator_id: str,
        ticket_id: str,
        cancel_request_id: str,
    ) -> ManualOrderTicketResource:
        """Cancel the ticket's owned working orders with one replayable request."""
        await submit_manual_ticket_cancellation(
            self._repo,
            account_id=self.account_id,
            operator_id=operator_id,
            ticket_id=ticket_id,
            cancel_request_id=cancel_request_id,
            trade=self._trade,
        )
        ticket = get_manual_ticket(self._repo, ticket_id)
        if ticket is None:
            raise RuntimeError("manual ticket disappeared after cancellation")
        return ticket

    async def register_strategy_run(
        self,
        binding: BrokerBotBinding,
        *,
        admission_snapshot: ClerkCustodySnapshot | None = None,
        run_owner: RunOwner | None = None,
    ) -> None:
        """Durably register immutable strategy + run before order capability.

        A Deploy names its budget (``binding.budget_consent``); a binding with
        none is refused as ``DeployBudgetRequiredError`` before anything is
        written (#3216). Registering a bot is append-only, so every refusal
        that can be judged from what is already stored -- the sealed terms,
        the world, the entry price and the account's cash and risk rules --
        is judged before the bot is registered: a refused Deploy leaves no
        registered, never-started bot behind.

        ``run_owner`` is what holds the run in this process -- its ``done()``
        turns true once the runner has ended (#2369). It is recorded in the
        same intake critical section that admits the run, so the sweep can
        never see an owned run as unowned. A run registered without one is
        unowned: the sweep retires it after one pass's grace (see
        :mod:`app.broker.alpaca.clerk.sqlite.run_ownership`).
        """
        from app.services.bot_carryover import configuration_hash, immutable_configuration_payload

        async with self._intake:
            if binding.sealed_account_id != self.account_id:
                raise SealedAccountMismatchError(
                    f"{SealedAccountMismatchError.reason_code}: sealed account "
                    f"{binding.sealed_account_id!r} does not match "
                    f"authority account {self.account_id!r}"
                )
            if admission_snapshot is not None:
                self._require_current_admission_snapshot(binding, admission_snapshot)
            config_hash = configuration_hash(binding)
            config_json = canonicalize(immutable_configuration_payload(binding))
            display_name = _strategy_display_name(binding.strategy_key)
            # The Clerk keeps custody, so the symbol it registers a bot under is
            # the one the bot's orders are in: the Action Plan's traded
            # instrument, which is the Signal stream's symbol only when the
            # bot trades what it listens to (#2912). A bot that trades option
            # contracts is registered under their underlying (ADR 0076).
            traded_symbol = self._symbol_to_register(binding.action_plan)
            existing = self._repo.strategy_instance(binding.strategy_instance_id)
            if existing is None:
                if binding.exit_terms is None or binding.exit_terms.provenance != "deployed":
                    raise StrategyRegistrationConflictError("Explicit exit terms are required for a new Start.")
            elif existing["symbol"].upper() != traded_symbol.upper() or existing["config_hash"] != config_hash:
                raise StrategyRegistrationConflictError(
                    f"strategy instance {binding.strategy_instance_id!r} conflicts with "
                    "its SQLite authority registration"
                )
            else:
                persisted_config = self._repo.bot_config(binding.strategy_instance_id)
                if (
                    persisted_config is None
                    or persisted_config.strategy_key != binding.strategy_key
                    or persisted_config.display_name != display_name
                    or persisted_config.config_json != config_json
                    or persisted_config.config_hash != config_hash
                ):
                    raise StrategyRegistrationConflictError(
                        f"strategy instance {binding.strategy_instance_id!r} conflicts with "
                        "its SQLite authority configuration"
                    )
                self._sealed_exit_terms(binding)

            consent = binding.budget_consent
            if consent is None:
                raise DeployBudgetRequiredError("A Deploy needs a budget. Review one for this bot, then deploy again.")
            world = authority_kind_for_account(self.account_id, account_mode=self._account_mode)
            if consent.world != world or self._live_envelope is None:
                raise BudgetUnavailable("The reviewed deployment world or cash authority changed. Review Deploy again.")
            if is_option_plan(binding.action_plan):
                # One option position's cost is known only from its
                # contract's quotes. No minimum is asked of the budget
                # here; each entry is admitted on its own commitment.
                required = Decimal(0)
            else:
                # One estimated position is priced from the asset the bot
                # buys, as the Deploy review priced it (#2912).
                quote = self._quote_source(traded_symbol, self._repo.clock())
                if quote is None:
                    raise BudgetUnavailable("Wait for a fresh IBKR price, then review Deploy again.")
                required, _ = entry_requirement(quantity=binding.quantity, price=quote.ask, at_ms=self._repo.clock())
            if existing is None:
                # Registering is append-only: judge the budget before it, so a
                # Deploy the commit would refuse leaves no bot behind.
                require_deploy_admissible(
                    self._repo, strategy_instance_id=binding.strategy_instance_id, world=world,
                    committed_cents=consent.committed_cents, risk_revision=consent.risk_revision,
                    envelope=self._live_envelope, minimum_position_cost=required,
                )
                self._repo.register_strategy_instance(
                    strategy_instance_id=binding.strategy_instance_id,
                    symbol=traded_symbol,
                    config_hash=config_hash,
                    strategy_key=binding.strategy_key,
                    display_name=display_name,
                    config_json=config_json,
                    exit_terms=binding.exit_terms,
                )
            terms = self._sealed_exit_terms(binding)
            self._exit_terms[binding.strategy_instance_id] = terms
            submission = submit_budgeted_deploy(
                self._repo, strategy_instance_id=binding.strategy_instance_id, lifecycle_run_id=binding.run_id,
                world=world, committed_cents=consent.committed_cents, configuration_hash=config_hash,
                exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
                risk_revision=consent.risk_revision, actor=consent.actor, request_fingerprint=consent.request_fingerprint,
                envelope=self._live_envelope, minimum_position_cost=required,
            )
            if not submission.created:
                raise BudgetUnavailable("This Deploy command already exists. Recover its stored result; it cannot launch again.")
            self._hold_run(binding, run_owner)

    def _sealed_exit_terms(self, binding: BrokerBotBinding) -> ExitTerms:
        terms = self._repo.exit_terms(binding.strategy_instance_id)
        if terms is None or (binding.exit_terms is not None and binding.exit_terms != terms):
            raise StrategyRegistrationConflictError("The bot must use its immutable custody-sealed exit terms.")
        return terms

    def _symbol_to_register(self, action_plan: ActionPlan) -> str:
        """The symbol a bot is registered under, or a typed refusal of a plan this account cannot take.

        A bot in shares: the symbol its orders are in. A bot in option
        contracts: their underlying, on an account that takes an option plan
        and only a plan the one rule takes (``option_plan_taken``;
        ``OptionPlanRefused`` otherwise).
        """
        if not is_option_plan(action_plan):
            return _entry_leg(action_plan).instrument.underlying
        refusal = self._option_plan_account_refusal()
        if refusal is not None:
            raise OptionPlanRefused(OptionEffectRefusal.ACCOUNT_NOT_OPEN, refusal)
        return option_plan_taken(action_plan, real_account=self._is_a_real_account).underlying

    def require_option_plan_taken(self, action_plan: ActionPlan) -> None:
        """Raise the typed refusal of an option plan this account does not take (ADR 0076).

        What a runner asks before it opens a stream or asks for a quote
        line: the rule a bot is registered under, so the two cannot differ.
        A plan in shares, and an option plan this account takes, raise
        nothing.
        """
        self._symbol_to_register(action_plan)

    def _option_plan_account_refusal(self, *, only_closes: bool = False) -> str | None:
        """Why this account takes no option plan, or ``None`` when it does: a Dry Run's, a Live Shadow and a Paper account do.

        A Live Shadow fills from quotes (ADR 0076 decision 25). A Live account is
        refused by name while ``ALPACA_LIVE_OPTION_BOTS_ENABLED`` is off,
        which it is unless a deployment turns it on: nothing about an option
        order has been sent to Alpaca yet, so real money waits for what
        Paper shows (ADR 0076 decision 24). The setting is read at every
        ask, so no registration and no effect outlives a change of it.

        ``only_closes`` is said of an EXIT, which the Clerk builds from the
        position its own books hold and which can open nothing. A fence
        against opening never stops a close: a Live account that holds a
        contract bought while the setting was on closes it with the setting
        off. A registration and an ENTER open, and are refused.
        """
        if self.authority_kind in _OPTION_PLAN_SIMULATIONS:
            return None
        if self.authority_kind == "sqlite" and self._account_mode == "paper":
            return None
        if only_closes or live_option_bots_enabled():
            return None
        return (
            "This bot trades option contracts. A Live account does not take an option bot yet: option bots are "
            "switched off on Live accounts, because option orders have not been checked against Alpaca with "
            "real money at stake. Check the first Paper option orders at Alpaca, then set "
            "ALPACA_LIVE_OPTION_BOTS_ENABLED=true for this Clerk."
        )

    @property
    def _is_a_real_account(self) -> bool:
        """Whether this is a Paper or a Live account: one whose orders are sent to Alpaca.

        What the one rule for option plans is told (``option_plan_taken``): a
        real account takes a plan of several legs only once its deployment
        says so. A Dry Run and a Live Shadow send nothing.
        """
        return self.authority_kind == "sqlite"

    @property
    def _sends_to_a_simulation(self) -> bool:
        """Whether this authority's orders are filled by a simulation, never sent to a broker."""
        return trade_port_folds_simulated_evidence(self._trade)

    @property
    def _asks_the_brokers_listing(self) -> bool:
        """Whether an entry's contract is asked of Alpaca's own listing before the entry is judged.

        A real account asks. So does a Live Shadow, of the live account it
        rehearses, through its read port: it applies the refusals the Live
        Clerk would, and a contract Alpaca does not list is one of them
        (ADR 0076 decision 25). It is a read; a Shadow sends nothing. A Dry
        Run has no broker to ask.
        """
        return self.authority_kind == "shadow" or not self._sends_to_a_simulation

    async def _contract_reference_refusal(
        self,
        *,
        strategy_instance_id: str,
        durable_decision_id: str,
        action_plan: ActionPlan,
        option_entry: OptionEntryEvidence | None,
    ) -> OptionEntryRefused | None:
        """Why Alpaca's own listing refuses the contract picked for an entry, or ``None``.

        Asked of the broker's contract reference, outside the intake lock,
        before the entry is judged: the contract must be listed, be the one
        asked about, be active and tradable, be American, and be a standard
        contract of a hundred shares, which is also the only multiplier an
        entry is taken with. A listing that could not be read refuses the
        entry too: nothing is bought on a caller's word that the broker
        could not confirm.

        The read has a bound of its own, well inside the budget the entry's
        quotes are held to (``CONTRACT_REFERENCE_READ_BOUND_MS``): the quotes
        age while it is out, and a listing that takes longer refuses the
        entry by name instead of spending the quotes.

        The broker is not asked about an entry that is refused here without
        it. A Dry Run's simulation is not asked: it has no broker. A Live
        Shadow's is, of the live account (``_asks_the_brokers_listing``). Nor is
        a decision sent again, which is answered from its own entry, or
        evidence that does not name a contract for the plan's leg, which is
        refused by name where the entry is worked out. And nor is an entry
        that arrives with the session closed or a channel unhealthy: it is
        refused for that, where it always was, and ``_LISTING_NOT_ASKED``
        comes back so that it is never taken without its listing if those
        answers have changed by then.
        """
        if not self._asks_the_brokers_listing or option_entry is None:
            return None
        try:
            taken = option_plan_taken(action_plan, real_account=self._is_a_real_account)
        except OptionPlanRefused:
            return None
        picked = option_entry.picked
        contracts = tuple(picked.get(leg.leg_id) for leg in taken.legs) if isinstance(picked, Mapping) else ()
        if len(contracts) != len(taken.legs) or any(not isinstance(contract, OptionContract) for contract in contracts):
            return None
        named = ", ".join(each.occ_symbol for each in contracts)
        if self._repo.get_command(enter_command_id(strategy_instance_id, durable_decision_id)) is not None:
            return None
        if (
            stream_health_refusal(self._stream_health, symbol=taken.underlying) is not None
            or not market_leg_sendable(self._repo.clock())
        ):
            return _LISTING_NOT_ASKED
        try:
            listings = await self._listings_beside_the_first_reading(
                contracts, entry=(strategy_instance_id, durable_decision_id)
            )
        except TimeoutError:
            return OptionEntryRefused(
                OptionEffectRefusal.CONTRACT_REFERENCE_UNREAD.value,
                f"Alpaca's listing of {named} did not answer within "
                f"{CONTRACT_REFERENCE_READ_BOUND_MS:,} ms, so the contract was not confirmed and the entry was not taken.",
            )
        except BrokerError as exc:
            return _listing_unread(named, exc)
        for contract, listing in zip(contracts, listings, strict=True):
            if isinstance(listing, BrokerError):
                return _listing_unread(contract.occ_symbol, listing)
            if isinstance(listing, BaseException):
                raise listing
            refused = option_contract_refusal(contract, listing)
            if refused is not None:
                return OptionEntryRefused(refused.reason.value, str(refused))
        return None

    async def _listings_beside_the_first_reading(
        self, contracts: tuple[OptionContract, ...], *, entry: tuple[str, str]
    ) -> tuple[BrokerOptionContract | BaseException | None, ...]:
        """Alpaca's listing of each contract, read while the account's option buying power is read too.

        The reads are made at once, so the reading of the account adds no
        step to an entry (ADR 0076 decision 26). Each has its own bound, and
        the reading's is the shorter. A listing's answer, or what its read
        raised, is the caller's; the reading never raises: what it could not
        read it keeps as that.

        One contract's failure is raised and the entry is refused for it, its
        reading cancelled. Of several, each answers for itself, so the
        refusal names the first one that could not be read, in leg order.
        """
        reading = None if self._buying_power is None else self._buying_power.begin_first()
        try:
            if len(contracts) == 1:
                answers = (
                    await asyncio.wait_for(
                        self._read.get_option_contract(contracts[0]),
                        timeout=CONTRACT_REFERENCE_READ_BOUND_MS / 1000,
                    ),
                )
            else:
                answers = await asyncio.wait_for(
                    asyncio.gather(
                        *(self._read.get_option_contract(contract) for contract in contracts), return_exceptions=True
                    ),
                    timeout=CONTRACT_REFERENCE_READ_BOUND_MS / 1000,
                )
        except BaseException:
            if reading is not None:
                reading.cancel()
            raise
        if self._buying_power is not None and reading is not None:
            await self._buying_power.finish_first(entry, reading)
        return tuple(answers)

    def synthesized_option_fills(self) -> Mapping[str, SynthesizedOptionFill]:
        """How a simulation priced each option order it answered, by client order id."""
        return self._trade.synthesized_option_fills()

    def option_fills_of_decision(self, strategy_instance_id: str, decision_id: str) -> tuple[OptionDecisionFill, ...]:
        """What the order one decision made has filled in option contracts, oldest first (ADR 0076).

        Asked by the decision's own id, as it was handed to
        ``execute_for_instance``. The order is the decision's own: an ENTER's
        opening order, an EXIT's closing order. Empty for a decision the Clerk
        refused, for an EXIT whose closing order has not been made yet, and
        for an order that has not filled.
        """
        return option_fills_of_decision(
            self._repo,
            strategy_instance_id=strategy_instance_id,
            durable_decision_id=_durable_decision_id(decision_id),
            how_simulated=self._trade.synthesized_option_fills(),
        )

    def option_contracts_for_run_quotes(self) -> tuple[str, ...]:
        """The option contracts a bot's run on this account must ask to have quoted, by OCC symbol (ADR 0076).

        One a bot holds, or one an order is working in: the books' own
        answer, in one read whatever their length. None on a Dry Run's
        account or a Live Shadow's. There the Clerk's own keeping asks for
        those lines, from the same read of the books, whether a run is alive
        or not (``held_option_quotes``), so a run that named them too would
        ask twice. No keeping follows a real account's books, so its bot's
        run asks here for the lines to have held.
        """
        if self.authority_kind in _OPTION_PLAN_SIMULATIONS:
            return ()
        return self._repo.option_contracts_requiring_quotes()

    def option_entry_of_decision(self, strategy_instance_id: str, decision_id: str) -> EntryOnTheBooks | None:
        """Where the option entry one decision made stands on the books now, or ``None`` when it made none (ADR 0076).

        Reads only: nothing is waited for and nothing is recorded. What a
        runner asks of an ENTER whose receipt was a refusal. One the Clerk
        refused before it took the entry made none. One it took and then
        did not send made one: it stands never sent, and the code is the
        one the books last recorded of its order, which says why.
        """
        command_id = enter_command_id(strategy_instance_id, _durable_decision_id(decision_id))
        with self._repo.write_fence():
            found = self._option_entry_of_command(command_id)
            if found is None:
                return None
            _command, order = found
            outcome = entry_outcome(self._repo, order.order_ref)
            last = self._repo.last_order_transition(order_ref=order.order_ref)
        assert last is not None, "an entry's order has its acceptance on record"
        return EntryOnTheBooks(
            standing=outcome.standing, order_ref=order.order_ref, last_recorded_code=last["summary_code"]
        )

    def held_option_contracts(self, strategy_instance_id: str) -> tuple[str, ...]:
        """The option contracts one bot holds now, by OCC symbol, its newest entry's first (ADR 0076).

        The books' answer, among the contracts the bot's own entries opened:
        what its EXIT closes. The plan's rules are not asked.
        """
        return tuple(
            symbol
            for symbol in option_entry_contracts(self._repo, strategy_instance_id)
            if position_quantity_is_nonzero(self._repo.position(strategy_instance_id, symbol))
        )

    async def record_deployment_launch(self, binding: BrokerBotBinding) -> None:
        async with self._intake:
            self._repo.record_deploy_launched(strategy_instance_id=binding.strategy_instance_id, lifecycle_run_id=binding.run_id)

    def _hold_run(self, binding: BrokerBotBinding, run_owner: RunOwner | None) -> None:
        if run_owner is not None:
            self._run_ownership.hold(
                strategy_instance_id=binding.strategy_instance_id,
                lifecycle_run_id=binding.run_id,
                owner=run_owner,
            )

    def _require_current_admission_snapshot(
        self,
        binding: BrokerBotBinding,
        snapshot: ClerkCustodySnapshot,
    ) -> None:
        """Validate the explicit two-phase Start/Resume token at activation."""
        meta = self._repo.control_meta_snapshot()
        if (
            snapshot.account_id != self.account_id
            or snapshot.strategy_instance_id != binding.strategy_instance_id
            or snapshot.clerk_generation != _clerk_generation(meta)
            or snapshot.journal_sequence != meta.control_revision
            or not snapshot.reconciliation_fresh
            or snapshot.reconciliation_state != "clean"
        ):
            raise StrategyAdmissionStaleError(
                "SQLite Clerk admission changed after preparation; refresh Start or Resume before activation"
            )

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> CommandSubmission:
        """Commit STOP before the process owner cancels strategy evaluation."""
        async with self._intake:
            return submit_stop_run(
                self._repo,
                account_id=self.account_id,
                strategy_instance_id=strategy_instance_id,
                lifecycle_run_id=run_id,
                operator_reason=reason,
                clock=self._repo.clock,
            )

    def flatten_send_verdict(self) -> SessionAuthorityState | LegRefusal:
        """The session an operator's flatten sent now goes out in, or the refusal this Clerk gives it (#2007).

        The canonical calendar's regular session, widened by the window this
        authority's broker declares -- the same window an extended-session
        program leg is shaped against -- judged at the send instant, exactly
        as every pricing entry point judges it (#2440 review).
        """
        return flatten_send_verdict(now_ms=self._repo.clock(), policy=self.program_leg_policy)

    def price_safe_flatten(
        self, plan: SafeFlattenPlan
    ) -> RecoveryReductionPricing | LegRefusal | None:
        """How the plan's one reduction would go out now, for the operator to confirm.

        ``None`` for a plan that is not a single leg: those are prepare-only,
        and one confirmed price can only ever be for one leg. A refusal is
        returned, not raised -- it is what the operator is shown.
        """
        if len(plan.legs) != 1:
            return None
        (leg,) = plan.legs
        if is_option_contract_symbol(leg.symbol.upper()):
            # Never priced from the stock quote source, which is not asked about a contract (ADR 0076).
            return self._option_flatten_refusal(leg, confirmed_limit=None) or RegularSessionReduction()
        if not self._trade.recovery_price_available(leg.symbol):
            return SIMULATED_RECOVERY_PRICE_UNAVAILABLE
        now_ms = self._repo.clock()
        try:
            return price_recovery_reduction(
                side=OrderSide(leg.side),
                now_ms=now_ms,
                policy=self.exit_policy_for_instance(leg.strategy_instance_id),
                quote=self._quote_source(leg.symbol, now_ms),
            )
        except ProgramLegRefused as exc:
            return exc.refusal

    async def execute_safe_flatten(
        self,
        *,
        plan: SafeFlattenPlan,
        reason: str | None = None,
        confirmed_limit: ConfirmedRecoveryLimit | None = None,
    ) -> SafeFlattenResult:
        """Execute the presented SafeFlattenPlan as recovery EXIT custody (F18).

        The reduction's shape is decided here, from now, before any EXIT is
        accepted (#2007): market DAY inside the regular session, the
        operator's confirmed limit in PRE/POST, and a refusal otherwise.
        """
        result = await execute_safe_flatten_plan(
            self._repo,
            plan=plan,
            read=self._read,
            trade=self._trade,
            intake=self._intake,
            account_id=self.account_id,
            pricing=self.recovery_pricing,
            confirmed_shape=self._safe_flatten_shape(plan, confirmed_limit),
        )
        logger.info(
            "operator safe flatten executed",
            extra={
                "action": "safe_flatten_executed",
                "account_id": self.account_id,
                "reason": reason,
                "order_count": len(result.orders),
                "accepted_effect_count": len(result.accepted_effect_operation_ids),
            },
        )
        return result

    def _safe_flatten_shape(
        self, plan: SafeFlattenPlan, confirmed_limit: ConfirmedRecoveryLimit | None
    ) -> ConfirmedRecoveryShape | None:
        """The one leg's reducing shape, or a ``SafeFlattenExecutionError`` naming why not.

        A multi-leg plan is prepare-only (the recovery policy never presents it
        for execution); with no one leg to price, it keeps the unshaped
        default, which waits for the regular session downstream.
        """
        if len(plan.legs) != 1:
            if confirmed_limit is not None:
                raise SafeFlattenExecutionError(
                    "A confirmed limit prices exactly one reduction; this plan has several."
                )
            return None
        (leg,) = plan.legs
        if is_option_contract_symbol(leg.symbol.upper()):
            option_refusal = self._option_flatten_refusal(leg, confirmed_limit=confirmed_limit)
            if option_refusal is not None:
                logger.info(
                    "operator safe flatten of an option position refused before any EXIT was accepted",
                    extra={
                        "action": "safe_flatten_option_refused",
                        "account_id": self.account_id,
                        "strategy_instance_id": leg.strategy_instance_id,
                        "symbol": leg.symbol,
                        "reason_code": option_refusal.reason_code,
                    },
                )
                raise SafeFlattenExecutionError(
                    f"{option_refusal.explanation} {option_refusal.next_step}", refusal=option_refusal
                )
            # The market DAY leg: the one order that closes an option position.
            return None
        if not self._trade.recovery_price_available(leg.symbol):
            # A no-submit world with nothing to fill at refuses here, before
            # an EXIT is accepted that could never be sent.
            refusal = SIMULATED_RECOVERY_PRICE_UNAVAILABLE
            raise SafeFlattenExecutionError(f"{refusal.explanation} {refusal.next_step}", refusal=refusal)
        now_ms = self._repo.clock()
        try:
            return recovery_reduction_shape(
                side=OrderSide(leg.side),
                symbol=leg.symbol,
                quantity=leg.quantity,
                now_ms=now_ms,
                policy=self.exit_policy_for_instance(leg.strategy_instance_id),
                confirmed=confirmed_limit,
                # Read at send, never taken from the client: the band and the
                # quote's freshness are judged against what the Clerk sees now.
                current_quote=(
                    None if confirmed_limit is None else self._quote_source(leg.symbol, now_ms)
                ),
            )
        except ProgramLegRefused as exc:
            logger.info(
                "operator safe flatten refused before any EXIT was accepted",
                extra={
                    "action": "safe_flatten_shape_refused",
                    "account_id": self.account_id,
                    "strategy_instance_id": leg.strategy_instance_id,
                    "reason_code": exc.reason_code,
                },
            )
            raise SafeFlattenExecutionError(
                f"{exc.explanation} {exc.next_step}", refusal=exc.refusal
            ) from exc

    def _option_flatten_refusal(
        self, leg: SafeFlattenPlanLeg, *, confirmed_limit: ConfirmedRecoveryLimit | None
    ) -> LegRefusal | None:
        """Why an operator's Flatten of a position in an option contract cannot go out now, or ``None`` (ADR 0076).

        Asked before any EXIT is accepted, so a Flatten that cannot be sent
        leaves nothing on the books to be sent later. In order: the contract
        has expired and no order can close it; the regular session is not
        open, and an option position is closed nowhere else; a limit price
        came with it, and it is closed by a market order only; nothing kept
        for the contract can price its close. That last is asked of the
        trade port: a simulation answers from the quotes kept for the
        contract, never from the stock quote source.
        """
        now_ms = self._repo.clock()
        expired = expired_option_close(leg.symbol, now_ms)
        if expired is not None:
            return expired
        if not market_leg_sendable(now_ms):
            return option_flatten_waits_for_open(now_ms)
        if confirmed_limit is not None:
            return OPTION_CLOSE_TAKES_NO_LIMIT
        if not self._trade.recovery_price_available(leg.symbol, side=leg.side):
            return option_close_unpriced(leg.symbol, side=leg.side)
        return None

    async def execute_for_instance(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
        use_rth: bool = True,
        retained_source_bar: RetainedSourceBar | None = None,
        pricing_bar: RetainedSourceBar | None = None,
        decision_evidence: EffectDecisionEvidence | None = None,
        option_entry: OptionEntryEvidence | None = None,
        option_exit: OptionExitEvidence | None = None,
    ) -> EffectOperationReceipt:
        """Route one semantic decision through SQLite ENTER/EXIT custody.

        The task is shielded after creation. Cancellation of the strategy
        caller therefore cannot cancel a broker request whose durable command
        and effect were already accepted; the task finishes or the periodic
        reconciliation sweep recovers its exact identity.

        ``retained_source_bar`` is the Signal stream's decision bar: it says
        when the decision closed. ``pricing_bar`` is the traded instrument's
        own bar for that minute, and prices the order. A bot that trades the
        symbol it listens to passes none: its decision bar is its pricing bar
        (``_order_pricing_bar``).

        A bot whose plan trades option contracts hands over ``option_entry``
        with an ENTER and may hand ``option_exit`` with an EXIT (ADR 0076,
        ``option_effects``): the contract its rules picked and the quotes it
        read. A bot in shares hands neither.
        """
        key = (strategy_instance_id, decision_id)
        if decision_evidence is not None and decision_evidence.evaluation_id != decision_id:
            raise ValueError("decision_id must equal the Signal Program evaluation_id")
        if (option_entry is not None or option_exit is not None) and not is_option_plan(action_plan):
            raise ValueError("option contracts and quotes were handed over with a plan that trades shares")
        task = self._effect_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._execute_effect(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    decision_id=decision_id,
                    purpose=purpose,
                    action_plan=action_plan,
                    quantity=quantity,
                    use_rth=use_rth,
                    retained_source_bar=retained_source_bar,
                    pricing_bar=pricing_bar,
                    decision_evidence=decision_evidence,
                    option_entry=option_entry,
                    option_exit=option_exit,
                ),
                name=f"alpaca-sqlite-effect:{strategy_instance_id}:{decision_id}",
            )
            self._effect_tasks[key] = task
            task.add_done_callback(lambda completed: self._clear_effect_task(key, completed))
        return await asyncio.shield(task)

    def _clear_effect_task(
        self,
        key: tuple[str, str],
        completed: asyncio.Task[EffectOperationReceipt],
    ) -> None:
        if self._effect_tasks.get(key) is completed:
            self._effect_tasks.pop(key)
        if completed.cancelled():
            logger.error(
                "A shielded SQLite Clerk effect task was unexpectedly cancelled",
                extra={"strategy_instance_id": key[0], "decision_id": key[1]},
            )
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "A SQLite Clerk effect task finished with an error",
                extra={"strategy_instance_id": key[0], "decision_id": key[1]},
                exc_info=(type(error), error, error.__traceback__),
            )

    async def begin_closing(self) -> None:
        """Say that this Clerk is closing: from here on it takes no scheduled close (ADR 0076).

        Said under the intake fence, the lock a scheduled close is accepted
        under. So a close is either accepted before this, and is finished or
        left for the next start as every unfinished EXIT is, or it is asked
        after this and not taken. The keeper's task and the account's
        reconciliation loop both go on while the Clerk closes, and each asks.
        """
        try:
            async with self._intake:
                self._repo.begin_closing()
        except IntakeFencePoisonedError:
            # A poisoned fence takes no run again, so nothing can be accepted under it: said without it.
            self._repo.begin_closing()

    async def drain_effects(self) -> None:
        """Finish accepted broker custody before the repository is closed."""
        pending = tuple(self._effect_tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._buying_power_round is not None:
            await asyncio.gather(self._buying_power_round, return_exceptions=True)

    async def _execute_effect(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
        use_rth: bool = True,
        retained_source_bar: RetainedSourceBar | None = None,
        pricing_bar: RetainedSourceBar | None = None,
        decision_evidence: EffectDecisionEvidence | None = None,
        option_entry: OptionEntryEvidence | None = None,
        option_exit: OptionExitEvidence | None = None,
    ) -> EffectOperationReceipt:
        """Judge one decision; an ENTER behind the account reading waits for a new one (#2623).

        An ENTER refused because executions postdate the last account reading
        is kept: the envelope's sync reads the account now, outside the intake
        fence, and the whole decision is judged again, so every other refusal
        -- the market-closed one included -- applies afresh. Nothing durable
        is written until an attempt is accepted, so the decision is accepted
        at most once however many attempts it takes. The wait ends, recorded
        as a refusal, when the run stops, the account cannot be read, no
        covering reading lands in the decision's time limit, or the Clerk is
        shutting down (``_ENTRY_WAIT_ENDINGS``).
        """
        reading_outcome: EntryReadingOutcome | None = None
        while True:
            outcome = await self._execute_effect_once(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_id=decision_id,
                purpose=purpose,
                action_plan=action_plan,
                quantity=quantity,
                use_rth=use_rth,
                retained_source_bar=retained_source_bar,
                pricing_bar=pricing_bar,
                decision_evidence=decision_evidence,
                reading_outcome=reading_outcome,
                option_entry=option_entry,
                option_exit=option_exit,
            )
            if isinstance(outcome, EffectOperationReceipt):
                return outcome
            # ``_execute_effect_once`` waits only where a reader is composed.
            assert self._entry_reading is not None
            logger.info(
                "an ENTER waits for an account reading newer than the account's executions",
                extra={
                    "action": "enter_awaits_account_reading",
                    "account_id": self._repo.account_id,
                    "strategy_instance_id": strategy_instance_id,
                    "decision_id": decision_id,
                    "until_ms": outcome.until_ms,
                },
            )
            reading_outcome = await self._entry_reading(
                until_ms=outcome.until_ms,
                wanted=lambda: self._run_active(strategy_instance_id, run_id),
            )

    def _run_active(self, strategy_instance_id: str, run_id: str) -> bool:
        active = self._repo.active_run(strategy_instance_id)
        return active is not None and active.lifecycle_run_id == run_id

    def _rejected_effect(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
        decision_evidence: EffectDecisionEvidence | None,
        reason_code: str,
        explanation: str,
        next_step: str | None = None,
        order_refs: tuple[str, ...] = (),
    ) -> EffectOperationReceipt:
        """Refuse one effect before custody: its durable refusal receipt, then the caller's."""
        if decision_evidence is not None:
            _append_pre_custody_refusal(
                self._repo,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                evidence=decision_evidence,
                reason_code=reason_code,
                explanation=explanation,
            )
        return _effect_receipt(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
            state="rejected",
            order_refs=order_refs,
            explanation=f"{reason_code}: {explanation}",
            next_step=next_step,
        )

    def _entry_wait_dropped(
        self,
        reading_outcome: EntryReadingOutcome | None,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
    ) -> str | None:
        """Why an ENTER's wait for an account reading ended without it, or ``None`` to judge the decision (#2623)."""
        ending = (
            None
            if reading_outcome is None
            else _entry_wait_ending(reading_outcome, run_active=self._run_active(strategy_instance_id, run_id))
        )
        if ending is None:
            return None
        logger.warning(
            "an ENTER waiting for an account reading was dropped",
            extra={
                "action": "enter_account_reading_wait_ended",
                "account_id": self._repo.account_id,
                "strategy_instance_id": strategy_instance_id,
                "decision_id": decision_id,
                "why": ending,
            },
        )
        return _ENTRY_WAIT_ENDINGS[ending]

    def _entry_admission_blocked(
        self,
        exc: AdmissionBlockedError,
        *,
        decision_evidence: EffectDecisionEvidence | None,
        rejected: Callable[..., EffectOperationReceipt],
    ) -> EffectOperationReceipt | _AwaitReading:
        """What an ENTER the Clerk would not admit comes to: a wait for a newer reading, or a refusal.

        #2623: the one refusal a newer account reading can lift keeps the
        decision; the reader judges its time limit.
        """
        until_ms = None if decision_evidence is None else decision_evidence.decision_valid_until_ms
        if (
            isinstance(exc, AccountReadingBehindExecutions)
            and self._entry_reading is not None
            and until_ms is not None
        ):
            return _AwaitReading(until_ms)
        return rejected(
            reason_code=exc.decision.reason_code or "ENTER_ADMISSION_BLOCKED",
            explanation=exc.decision.why or "The Clerk refused new exposure.",
        )

    def _answer_from_active_exit(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
        atomic_receipt: AtomicDecisionReceipt | None,
    ) -> EffectOperationReceipt | None:
        """The answer to an EXIT decided while another EXIT owns the bot's reduction, or ``None`` when none does.

        FR-019/FR-023: this evaluation reached the custody seam and was
        answered by an effect that already exists. It is still a decision
        that happened, so it leaves durable evidence linked to the effect
        that resolved it -- without that link the causal read (FR-030) cannot
        later explain why this evaluation produced no effect of its own.
        """
        active_exit = self._repo.active_exit_for_strategy(strategy_instance_id)
        if active_exit is None:
            return None
        if atomic_receipt is not None:
            self._repo.capture_decision_against_active_exit(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_receipt=atomic_receipt,
            )
        return _effect_receipt(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
            state=active_exit.state,
            order_refs=tuple(
                order.order_ref
                for order in self._repo.orders_for_effect_operation(
                    active_exit.effect_operation_id
                )
            ),
            explanation=(
                f"EXIT_IN_PROGRESS: existing EXIT {active_exit.effect_operation_id} "
                "already owns this strategy's reduction custody."
            ),
            next_step="Await the existing EXIT custody outcome; do not submit another EXIT.",
        )

    async def _execute_option_effect_once(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
        decision_evidence: EffectDecisionEvidence | None,
        reading_outcome: EntryReadingOutcome | None,
        option_entry: OptionEntryEvidence | None,
        option_exit: OptionExitEvidence | None,
    ) -> EffectOperationReceipt | _AwaitReading:
        """One decision of a bot that trades option contracts (ADR 0076).

        An ENTER is worked out by the Clerk from the contract and quotes the
        caller hands over (``option_admission``), admitted on its entry
        commitment, and sent as one option order. An EXIT closes the contract
        the bot's own entry opened, read from the books and never from the
        plan's rules, in one order sized from the position confirmed when it
        is made. The fences are the ones an effect in shares passes, in the
        same order.

        A Dry Run's account, a Live Shadow and a Paper account take either, and so does a
        Live account while its setting is on. With the setting off a Live
        account still takes an EXIT, which closes what its books hold, and
        refuses an ENTER (ADR 0076 decision 24). Every other plan and every
        other account is refused here by name, before anything is written
        but the refusal. On a Paper account or a Live Shadow the picked
        contract is first asked of Alpaca's own contract reference
        (:meth:`_contract_reference_refusal`), and the entry then rests at
        Alpaca as a limit order: this call returns once it is sent, and what
        it came to is asked for with :meth:`await_entry_outcome`.
        A Dry Run and a Live Shadow fill inside the send, or end it there unfilled.
        """

        def receipt(state: str, order_refs: tuple[str, ...]) -> EffectOperationReceipt:
            return _effect_receipt(
                strategy_instance_id=strategy_instance_id, run_id=run_id, decision_id=decision_id, purpose=purpose,
                action_plan=action_plan, quantity=quantity, state=state, order_refs=order_refs,
            )

        def rejected(*, reason_code: str, explanation: str, next_step: str | None = None) -> EffectOperationReceipt:
            return self._rejected_effect(
                strategy_instance_id=strategy_instance_id, run_id=run_id, decision_id=decision_id, purpose=purpose,
                action_plan=action_plan, quantity=quantity, decision_evidence=decision_evidence,
                reason_code=reason_code, explanation=explanation, next_step=next_step,
            )

        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            # Checked before anything is written: a receipt cannot carry a quantity outside these,
            # and a contract is not traded in parts.
            raise ValueError(f"an option effect is for a whole number of structures, 1 to 100; got {quantity!r}")
        accepted_enter: EnterSubmission | None = None
        accepted_exit: ExitSubmission | None = None
        entry: OptionEntry | None = None
        durable_decision_id = _durable_decision_id(decision_id)
        not_listed = (
            None
            if purpose is not EffectPurpose.ENTER or self._option_plan_account_refusal() is not None
            else await self._contract_reference_refusal(
                strategy_instance_id=strategy_instance_id, durable_decision_id=durable_decision_id,
                action_plan=action_plan, option_entry=option_entry,
            )
        )
        async with self._intake:
            dropped = self._entry_wait_dropped(
                reading_outcome, strategy_instance_id=strategy_instance_id, run_id=run_id, decision_id=decision_id
            )
            if dropped is not None:
                return rejected(
                    reason_code=LIVE_ENVELOPE_UNOBSERVED, explanation=dropped, next_step=_ENTRY_NOT_RETRIED
                )
            not_this_account = self._option_plan_account_refusal(only_closes=purpose is EffectPurpose.EXIT)
            if not_this_account is not None:
                return rejected(
                    reason_code=OptionEffectRefusal.ACCOUNT_NOT_OPEN.value,
                    explanation=not_this_account,
                    next_step="No order was sent.",
                )
            atomic_receipt = _atomic_decision_receipt(decision_evidence)
            if purpose is EffectPurpose.ENTER:
                # A decision sent again is answered from its own entry, before
                # anything is judged again: its quotes have aged since, and a
                # refusal now would deny an entry the bot may already hold.
                already = self._repo.get_command(enter_command_id(strategy_instance_id, durable_decision_id))
                if already is not None and already.action == "ENTER" and already.effect_operation_id is not None:
                    existing = self._repo.order_for_effect_operation(already.effect_operation_id)
                    first = None if existing is None else option_entry_of_order(self._repo, existing.order_ref)
                    if first is not None and _asks_for_another_entry(first, units=quantity, evidence=option_entry):
                        # The same decision asking for another contract or another size is a second
                        # request under the first one's name, as it is for an entry in shares. Only
                        # the price may differ on an honest retry: its quotes are newer.
                        raise DurableConflictError(already)
                    return receipt(already.state, () if existing is None else (existing.order_ref,))
                if not_listed is not None and not_listed is not _LISTING_NOT_ASKED:
                    logger.info(
                        "an option entry was refused on the broker's contract reference",
                        extra={
                            "action": "option_entry_contract_refused",
                            "account_id": self._repo.account_id,
                            "strategy_instance_id": strategy_instance_id,
                            "decision_id": decision_id,
                            "reason_code": not_listed.reason_code,
                        },
                    )
                    return rejected(
                        reason_code=not_listed.reason_code,
                        explanation=not_listed.explanation,
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                worked_out = work_out_option_entry(
                    action_plan, evidence=option_entry, units=quantity, now_ms=self._repo.clock(),
                    real_account=self._is_a_real_account,
                )
                if isinstance(worked_out, OptionEntryRefused):
                    logger.info(
                        "an option entry was refused before custody",
                        extra={
                            "action": "option_entry_refused",
                            "account_id": self._repo.account_id,
                            "strategy_instance_id": strategy_instance_id,
                            "decision_id": decision_id,
                            "reason_code": worked_out.reason_code,
                        },
                    )
                    return rejected(
                        reason_code=worked_out.reason_code,
                        explanation=worked_out.explanation,
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                entry = worked_out
                other_exposure = [
                    HeldByAnother(symbol, holder, held)
                    for symbol, holder, held in self._repo.option_contracts_held_by_others(
                        strategy_instance_id=strategy_instance_id,
                        symbols=[terms.symbol for terms in entry.leg_terms],
                    )
                ]
                for leg in entry.legs:
                    side = OrderSide.BUY if leg.position == "long" else OrderSide.SELL
                    for pending in self._repo.open_opposite_side_orders(symbol=leg.contract.occ_symbol, side=side):
                        owner = self._repo.order(pending.order_ref)
                        assert owner is not None
                        effect = self._repo.effect_operation(owner.effect_operation_id)
                        assert effect is not None
                        if effect.strategy_instance_id != strategy_instance_id:
                            other_exposure.append(HeldByAnother(
                                leg.contract.occ_symbol, effect.strategy_instance_id,
                                (pending.quantity or 1) * (1 if pending.side == "buy" else -1), working_order=True,
                            ))
                against_another = held_on_the_other_side(entry, other_exposure)
                if against_another is not None:
                    logger.info(
                        "an option entry was refused: another subject holds or has ordered one of its contracts on the other side",
                        extra={
                            "action": "option_entry_refused",
                            "account_id": self._repo.account_id,
                            "strategy_instance_id": strategy_instance_id,
                            "decision_id": decision_id,
                            "reason_code": against_another.reason_code,
                        },
                    )
                    return rejected(
                        reason_code=against_another.reason_code,
                        explanation=against_another.explanation,
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                underlying = option_plan_taken(action_plan, real_account=self._is_a_real_account).underlying
                if stream_health_refusal(self._stream_health, symbol=underlying) is not None:
                    return rejected(
                        reason_code=STREAM_HEALTH_REASON_CODE,
                        explanation="The required market-data or execution channel is unhealthy.",
                        next_step="Restore both channels and let the Clerk observe fresh health evidence.",
                    )
                if not market_leg_sendable(self._repo.clock()):
                    return rejected(
                        reason_code=MARKET_CLOSED,
                        explanation=_OPTION_ORDER_OUTSIDE_THE_SESSION,
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                if not_listed is _LISTING_NOT_ASKED:
                    # The session opened, or the channel recovered, between the two looks: nothing
                    # is bought in a contract whose listing was never read.
                    return rejected(
                        reason_code=_LISTING_NOT_ASKED.reason_code,
                        explanation=_LISTING_NOT_ASKED.explanation,
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                try:
                    accepted_enter = accept_option_enter(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=durable_decision_id,
                        lifecycle_run_id=run_id,
                        entry=entry,
                        proven_by_ms=option_entry.proven_by_ms,
                        decision_receipt=atomic_receipt,
                        envelope=self._live_envelope,
                    )
                except OptionEntryProofWindowClosed:
                    return rejected(
                        reason_code=OptionEffectRefusal.TAKEN_TOO_LATE.value,
                        explanation=(
                            "The entry's cancel time leaves too little room to prove its outcome "
                            "before the bot's deadline. No order was sent."
                        ),
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                except AdmissionBlockedError as exc:
                    return self._entry_admission_blocked(exc, decision_evidence=decision_evidence, rejected=rejected)
                except ContractTermsConflict as exc:
                    # The books hold this contract with another multiplier. Nothing was written:
                    # the entry's acceptance and its contract's terms are one transaction.
                    return rejected(
                        reason_code=OptionEffectRefusal.CONTRACT_TERMS_CONFLICT.value,
                        explanation=f"{exc}. An entry is not counted with a multiplier its contract is not recorded with.",
                        next_step=_ENTRY_NOT_RETRIED,
                    )
            else:
                in_progress = self._answer_from_active_exit(
                    strategy_instance_id=strategy_instance_id, run_id=run_id, decision_id=decision_id,
                    purpose=purpose, action_plan=action_plan, quantity=quantity, atomic_receipt=atomic_receipt,
                )
                if in_progress is not None:
                    return in_progress
                # The contract to close is the one the bot's own entry opened:
                # the one it holds, or with nothing held its newest entry's,
                # whose EXIT then proves flat. The plan's rules are not asked.
                opened = option_entry_contracts(self._repo, strategy_instance_id)
                held = [
                    symbol for symbol in opened
                    if position_quantity_is_nonzero(self._repo.position(strategy_instance_id, symbol))
                ]
                closing = held[0] if held else (opened[0] if opened else None)
                target = None if closing is None else newest_reducible_entry(self._repo, strategy_instance_id, closing)
                entry = None if target is None else option_entry_of_order(self._repo, target.order_ref)
                contracts = () if entry is None else tuple(terms.symbol for terms in entry.leg_terms)
                if len(held) > 1 and (not isinstance(entry, MultiLegOptionEntryFacts) or set(held) - set(contracts)):
                    # A receipt must cover every held contract. Disjoint one-leg entries have no recorded structure.
                    logger.error(
                        "an option bot holds several contracts; its EXIT was refused",
                        extra={
                            "action": "option_exit_several_contracts_held",
                            "account_id": self._repo.account_id,
                            "strategy_instance_id": strategy_instance_id,
                            "decision_id": decision_id,
                            "held": held,
                        },
                    )
                    return rejected(
                        reason_code=OptionEffectRefusal.SEVERAL_CONTRACTS_HELD.value,
                        explanation=(
                            f"This bot holds {len(held)} option contracts ({', '.join(held)}). One EXIT closes "
                            "one contract, so answering it would leave the others open. Nothing was sent."
                        ),
                        next_step="Stop the bot and flatten each contract from its page.",
                    )
                not_closed = () if option_exit is None else tuple(sorted(set(option_exit.quotes) - set(contracts)))
                if not_closed:
                    # A quote is taken only for the contract this EXIT closes. One of another
                    # contract could price nothing here, and is the caller's mistake to hear of.
                    return rejected(
                        reason_code=OptionEffectRefusal.EXIT_QUOTES_FOR_ANOTHER_CONTRACT.value,
                        explanation=(
                            f"This EXIT closes {closing}. A quote was handed with it for {', '.join(not_closed)}, "
                            "which this bot's EXIT does not close. Nothing was sent."
                        ),
                        next_step="Hand over the quote of the contract the bot holds, named by that contract's symbol.",
                    )
                expired = None if not held else expired_option_close(held[0], self._repo.clock())
                if expired is not None and target is not None:
                    # Refused by name before any EXIT is accepted: no order can close an expired
                    # contract. The notice goes up with it, once, so the bot's page says so too.
                    note_expired_option_position(
                        self._repo, strategy_instance_id=strategy_instance_id, entry_order_ref=target.order_ref,
                        symbol=held[0], attributed_qty=self._repo.position(strategy_instance_id, held[0]),
                        expired=expired,
                    )
                    return rejected(
                        reason_code=expired.reason_code, explanation=expired.explanation, next_step=expired.next_step
                    )
                if target is None:
                    return rejected(
                        reason_code="EXIT_CUSTODY_UNPROVEN",
                        explanation=(
                            "No entry in an option contract is on this bot's books; the Clerk cannot prove "
                            "what there is to close."
                        ),
                        next_step="Reconcile the instance custody before attempting another EXIT.",
                    )
                try:
                    accepted_exit = accept_exit(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=durable_decision_id,
                        lifecycle_run_id=run_id,
                        entry_order_ref=target.order_ref,
                        decision_receipt=atomic_receipt,
                        option_exit=option_exit,
                    )
                except UnknownEntryOrderError:
                    return rejected(
                        reason_code="EXIT_CUSTODY_UNPROVEN",
                        explanation="The Clerk could not prove that the selected entry still belongs to this EXIT.",
                        next_step="Reconcile the instance custody before attempting another EXIT.",
                    )

        if accepted_enter is not None:
            assert entry is not None
            priced_from, quote_generation = entry.legs, entry.quote_generation
            clock = self._repo.clock
            opened = entry.structure
            # The entry's own terms. One accepted before entries kept theirs is held to the defaults.
            entry_terms = entry.terms.close_terms if entry.terms is not None else ENTRY_CLOSE_TERMS

            handed_over_at_ms: int | None = None

            def before_submit() -> EntrySubmissionRefusal | None:
                nonlocal handed_over_at_ms
                now_ms = clock()
                if not market_leg_sendable(now_ms):
                    return EntrySubmissionRefusal(summary_code=MARKET_CLOSED, why=_OPTION_ORDER_OUTSIDE_THE_SESSION)
                # Asked again at the clock the order is sent at: the entry was worked out under
                # the intake lock, and the cutoff may have passed since (ADR 0076).
                too_late = entry_past_cutoff(opened, now_ms=now_ms, terms=entry_terms)
                if too_late is not None:
                    return EntrySubmissionRefusal(summary_code=too_late.reason_code, why=too_late.explanation)
                # The quote was tested as the entry was worked out. The order goes out later: a
                # simulation fills it from that quote, and a real account's limit rests at the price
                # that quote gave. So it is tested again by the same rule, at this clock: every leg's.
                for leg in priced_from:
                    unusable = entry_quote_refusal(
                        leg.contract, leg.quote.held, position=leg.position,
                        generation=quote_generation, now_ms=now_ms, at_the_send=True,
                    )
                    if unusable is not None:
                        return EntrySubmissionRefusal(summary_code=unusable.reason_code, why=unusable.explanation)
                handed_over_at_ms = now_ms
                return None

            if accepted_enter.created and self._entry_deadline_nudge is not None:
                # The entry's cancel time is on the books: the keeper sleeps no longer than to it.
                self._entry_deadline_nudge()
            if self._buying_power is not None:
                # The reads of the first reading may have finished in worker threads, and the work
                # above made no turn of the loop to note them. Without one a finished read is noted
                # after the send and reads as ending after it (#3136).
                await self._buying_power.land_finished_reads((strategy_instance_id, durable_decision_id))
            submitted_enter = await submit_accepted_enter(
                self._repo,
                accepted=accepted_enter,
                leg=entry.order,
                # A simulation fills from the quote the entry was admitted on. A real
                # account's order rests at the broker, which needs no quote of ours.
                trade=(
                    _OptionQuoteBoundTradePort.of_entry(self._trade, entry)
                    if self._sends_to_a_simulation
                    else self._trade
                ),
                before_submit=before_submit,
            )
            if self._buying_power is not None:
                # From here the entry's comparison with the account's option buying power is the
                # Clerk's own round's to make (ADR 0076 decision 26).
                self._buying_power.sent(
                    (strategy_instance_id, durable_decision_id), order_ref=submitted_enter.order_ref,
                    sent_at_ms=handed_over_at_ms,
                )
            return receipt(
                submitted_enter.command.state,
                (submitted_enter.order_ref,) if submitted_enter.order_ref is not None else (),
            )

        assert accepted_exit is not None
        submitted_exit = await resolve_accepted_exit(
            self._repo,
            accepted=accepted_exit,
            trade=self._trade,
            pricing=self.recovery_pricing,
            # A deciding program's EXIT is sent with no account check (#2839).
            read=None,
        )
        return receipt(
            submitted_exit.command.state,
            tuple(
                ref for ref in (submitted_exit.entry_order_ref, submitted_exit.reducing_order_ref) if ref is not None
            ),
        )

    async def await_entry_outcome(
        self, *, strategy_instance_id: str, decision_id: str, until_ms: int
    ) -> SettledEntry:
        """Wait for the option entry of one decision to be proven, and say how the strategy is settled on it (ADR 0076).

        ``execute_for_instance`` returns once the order is sent. This is the
        second question a runner asks of an option entry: what did it come
        to? The books are read every 200 ms until one of these, and the
        first that holds answers:

        - the entry was settled before: the answer is that record's;
        - the outcome is proven: filled, filled in part and ended, ended
          with nothing filled, or never sent;
        - the bot's run is no longer active: the answer is the books' as
          they stand, and nothing is recorded, so a later replay settles
          from the books as they then are;
        - the Clerk's clock has reached ``until_ms``, the end of the
          caller's own wait: still unproven, the strategy is in.

        The settlement is recorded once, as ``ENTRY_SETTLED`` on the ENTER's
        own timeline, in the same locked read it was worked out in: what is
        recorded is what was answered, and asked again the record answers.

        No lock is held between reads, so no other bot waits behind this
        one, and the caller may cancel the wait at any moment: the cancel at
        the entry's deadline is the keeper's, whoever is or is not waiting.

        Raises ``LookupError`` when the decision has no option entry on the
        books: it was refused before the Clerk accepted it. An entry the
        Clerk accepted and the broker then refused is on the books, and is
        answered: never sent, the decision dropped.

        Raises ``EntryOutcomeWaitRefused`` for a limit more than a week
        ahead. A past limit judges the books immediately.
        """
        if isinstance(until_ms, bool) or not isinstance(until_ms, int) or not 0 <= until_ms <= MAX_TIMESTAMP_MS:
            raise ValueError(f"until_ms is a time in milliseconds; got {until_ms!r}")
        asked_at_ms = self._repo.clock()
        command_id = enter_command_id(strategy_instance_id, _durable_decision_id(decision_id))
        waited = False
        while True:
            async with self._intake:
                settled = self._settle_entry_if_answerable(
                    command_id, until_ms=until_ms,
                    limit_was_too_far_when_asked=not waited and until_ms - asked_at_ms > MAX_ENTRY_OUTCOME_WAIT_MS,
                )
            if settled is not None:
                return settled
            await self._outcome_pause(_ENTRY_OUTCOME_POLL_S)
            waited = True

    def _settle_entry_if_answerable(
        self, command_id: str, *, until_ms: int, limit_was_too_far_when_asked: bool = False,
    ) -> SettledEntry | None:
        """One read of an option entry's outcome: its settlement when one can be given now, else ``None``.

        The recorded answer and a proven outcome are read before the limit
        is judged: a limit is refused only for an entry that would have to
        be waited for (ADR 0076 decision 19).
        """
        with self._repo.write_fence():
            found = self._option_entry_of_command(command_id)
            if found is None:
                raise LookupError(f"no entry in an option contract is on the books for {command_id!r}")
            command, order = found
            assert command.effect_operation_id is not None
            recorded = recorded_settlement(self._repo, order.order_ref)
            if recorded is not None:
                return recorded
            outcome = entry_outcome(self._repo, order.order_ref)
            now_ms = self._repo.clock()
            if not outcome.proven and limit_was_too_far_when_asked:
                raise EntryOutcomeWaitRefused(
                    ENTRY_OUTCOME_LIMIT_TOO_FAR,
                    f"The wait for this entry's outcome was asked to run {until_ms - now_ms:,} ms, and the entry is "
                    f"not proven yet. No wait is longer than {MAX_ENTRY_OUTCOME_WAIT_MS:,} ms.",
                )
            run = None if command.run_id is None else self._repo.active_run(command.strategy_instance_id or "")
            run_active = run is not None and run.run_id == command.run_id
            if not outcome.proven and not run_active:
                return SettledEntry.of(
                    settled_now(outcome, until_ms=until_ms, now_ms=now_ms), order_ref=order.order_ref, recorded=False
                )
            if not outcome.proven and now_ms < until_ms:
                return None
            facts = settled_now(outcome, until_ms=until_ms, now_ms=now_ms)
            effect = self._repo.effect_operation(command.effect_operation_id)
            assert effect is not None
            self._repo.append_transition(
                TransitionInput(
                    strategy_instance_id=effect.strategy_instance_id,
                    run_id=effect.run_id,
                    command_id=effect.command_id,
                    effect_operation_id=effect.effect_operation_id,
                    transition_kind="ENTRY_SETTLED",
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state=effect.state,
                    clerk_observed_at_ms=now_ms,
                    summary_code="ENTRY_SETTLED",
                    facts_json=facts.to_facts_json(),
                )
            )
            logger.info(
                "an option entry was settled",
                extra={
                    "action": "option_entry_settled",
                    "account_id": self._repo.account_id,
                    "strategy_instance_id": effect.strategy_instance_id,
                    "order_ref": order.order_ref,
                    "settlement": facts.settlement,
                    "standing": facts.standing,
                    "units_filled": facts.units_filled,
                },
            )
            return SettledEntry.of(facts, order_ref=order.order_ref, recorded=True)

    def _option_entry_of_command(self, command_id: str) -> tuple[CommandResource, OrderResource] | None:
        """The ENTER ``command_id`` and the order of the option entry it made, or ``None`` when it made none."""
        command = self._repo.get_command(command_id)
        order = (
            None
            if command is None or command.action != "ENTER" or command.effect_operation_id is None
            else self._repo.order_for_effect_operation(command.effect_operation_id)
        )
        if command is None or order is None or option_entry_of_order(self._repo, order.order_ref) is None:
            return None
        return command, order

    async def _execute_effect_once(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
        use_rth: bool,
        retained_source_bar: RetainedSourceBar | None,
        pricing_bar: RetainedSourceBar | None,
        decision_evidence: EffectDecisionEvidence | None,
        reading_outcome: EntryReadingOutcome | None,
        option_entry: OptionEntryEvidence | None = None,
        option_exit: OptionExitEvidence | None = None,
    ) -> EffectOperationReceipt | _AwaitReading:
        if is_option_plan(action_plan):
            # An entry in option contracts is worked out from quotes, not from
            # a bar, and is its own path from here (ADR 0076).
            try:
                return await self._execute_option_effect_once(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    decision_id=decision_id,
                    purpose=purpose,
                    action_plan=action_plan,
                    quantity=quantity,
                    decision_evidence=decision_evidence,
                    reading_outcome=reading_outcome,
                    option_entry=option_entry,
                    option_exit=option_exit,
                )
            finally:
                if self._buying_power is not None:
                    # A reading of the account taken for an entry that was then not taken is dropped.
                    self._buying_power.forget((strategy_instance_id, _durable_decision_id(decision_id)))

        def rejected(
            *,
            reason_code: str,
            explanation: str,
            next_step: str | None = None,
            order_refs: tuple[str, ...] = (),
        ) -> EffectOperationReceipt:
            return self._rejected_effect(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_id=decision_id,
                purpose=purpose,
                action_plan=action_plan,
                quantity=quantity,
                decision_evidence=decision_evidence,
                reason_code=reason_code,
                explanation=explanation,
                next_step=next_step,
                order_refs=order_refs,
            )

        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            # Checked before anything is written, as the option path checks its
            # own quantity: a receipt cannot carry a quantity outside these, and
            # the bound must not first bite after the order was sent (#2956).
            raise ValueError(f"a stock effect is for a whole number of shares, 1 to 100; got {quantity!r}")

        async with self._intake:
            # The run is judged under the fence Stop commits under, so an
            # entry the run no longer owns is never admitted after its reading.
            dropped = self._entry_wait_dropped(
                reading_outcome, strategy_instance_id=strategy_instance_id, run_id=run_id, decision_id=decision_id
            )
            if dropped is not None:
                return rejected(
                    reason_code=LIVE_ENVELOPE_UNOBSERVED,
                    explanation=dropped,
                    next_step=_ENTRY_NOT_RETRIED,
                )
            if self.simulates_fills and retained_source_bar is None:
                return rejected(
                    reason_code="SIMULATED_SOURCE_BAR_UNPROVEN",
                    explanation=(
                        "Synthesized custody received no exact retained source bar for this decision."
                    ),
                    next_step="Replay or ingest the decision bar, then retry through the sealed program.",
                )
            entry = _entry_leg(action_plan)
            priced_by = _order_pricing_bar(
                entry.instrument.underlying,
                purpose=purpose,
                synthesized=self.simulates_fills,
                exit_sells=entry.position == "long",
                decision_bar=retained_source_bar,
                pricing_bar=pricing_bar,
            )
            if isinstance(priced_by, LegRefusal):
                logger.warning(
                    "an effect was refused: no bar of its traded instrument prices it",
                    extra={
                        "action": "effect_pricing_bar_refused",
                        "account_id": self._repo.account_id,
                        "strategy_instance_id": strategy_instance_id,
                        "decision_id": decision_id,
                        "purpose": purpose.value,
                        "reason_code": priced_by.reason_code,
                        "traded_symbol": entry.instrument.underlying,
                        "decision_bar_symbol": None if retained_source_bar is None else retained_source_bar.symbol,
                        "pricing_bar_symbol": None if pricing_bar is None else pricing_bar.symbol,
                    },
                )
                return rejected(
                    reason_code=priced_by.reason_code,
                    explanation=priced_by.explanation,
                    next_step=priced_by.next_step,
                )
            # A synthesized sell with no bar of its own minute is priced from
            # the newest earlier one the runner found (``_order_pricing_bar``).
            older_price = (
                priced_by is not None
                and retained_source_bar is not None
                and priced_by.end_ms < retained_source_bar.end_ms
            )
            if older_price and not market_leg_sendable(self._repo.clock()):
                # An older price fills a market order only. Once the regular
                # session cannot take one, the sale would be re-priced as a
                # limit to rest, and no limit is anchored to an older price.
                assert priced_by is not None and retained_source_bar is not None
                logger.warning(
                    "a simulated sale priced from an earlier minute arrived after a market order could be sent",
                    extra={
                        "action": "effect_pricing_bar_refused",
                        "account_id": self._repo.account_id,
                        "strategy_instance_id": strategy_instance_id,
                        "decision_id": decision_id,
                        "purpose": purpose.value,
                        "reason_code": PRICING_BAR_UNAVAILABLE.reason_code,
                        "cause": "older_price_after_market_close",
                        "traded_symbol": entry.instrument.underlying,
                        "pricing_bar_end_ms": priced_by.end_ms,
                        "decision_bar_end_ms": retained_source_bar.end_ms,
                    },
                )
                return rejected(
                    reason_code=PRICING_BAR_UNAVAILABLE.reason_code,
                    explanation=(
                        f"{entry.instrument.underlying} has no bar for the decision's minute, and the regular "
                        "session can no longer take a market order. An earlier minute's price fills a market "
                        "sale only, so this sale was not simulated."
                    ),
                    next_step=PRICING_BAR_UNAVAILABLE.next_step,
                )
            if pricing_bar is not None and priced_by is not pricing_bar:
                # The order goes on without it (no decision bar to close with,
                # or a broker-sent EXIT that needs no price), but a bar the
                # caller offered and the Clerk would not use is a caller fault.
                logger.warning(
                    "a pricing bar offered for an effect was not used",
                    extra={
                        "action": "effect_pricing_bar_ignored",
                        "account_id": self._repo.account_id,
                        "strategy_instance_id": strategy_instance_id,
                        "decision_id": decision_id,
                        "purpose": purpose.value,
                        "traded_symbol": entry.instrument.underlying,
                        "decision_bar_end_ms": None if retained_source_bar is None else retained_source_bar.end_ms,
                        "pricing_bar_symbol": pricing_bar.symbol,
                        "pricing_bar_end_ms": pricing_bar.end_ms,
                    },
                )
            durable_decision_id = _durable_decision_id(decision_id)
            atomic_receipt = _atomic_decision_receipt(decision_evidence)
            program_side = OrderSide.BUY if entry.position == "long" else OrderSide.SELL
            leg_side = program_side if purpose is EffectPurpose.ENTER else _REDUCING_SIDE[program_side]
            try:
                program_leg = shape_program_leg(
                    side=leg_side,
                    purpose=purpose,
                    use_rth=use_rth,
                    decision_bar=retained_source_bar,
                    # An older price fills a synthesized sell; it never
                    # anchors a limit an order rests at.
                    pricing_bar=None if older_price else priced_by,
                    policy=(self.exit_policy_for_instance(strategy_instance_id)
                            if purpose is EffectPurpose.EXIT else self.program_leg_policy),
                )
            except ProgramLegRefused as exc:
                return rejected(
                    reason_code=exc.reason_code,
                    explanation=exc.explanation,
                    next_step=exc.next_step,
                )
            if purpose is EffectPurpose.ENTER:
                # The EXIT branch threads ``shape`` into the reducing order's
                # durable facts instead; building a leg it discards would be a
                # second, silent leg construction one refactor away from
                # disagreeing with the one that ships.
                operation_leg = program_leg.shape.apply(
                    symbol=entry.instrument.underlying,
                    quantity=float(quantity * entry.qty_ratio),
                )
                if (
                    stream_health_refusal(
                        self._stream_health,
                        symbol=entry.instrument.underlying,
                    )
                    is not None
                ):
                    # S4 parity (#1262): either channel unhealthy ->
                    # rejected receipt, no broker contact. EXIT/cancel remain
                    # unaffected: exit.py never consults
                    # `active_holds_for_admission`.
                    #
                    # #1777 WP4/S10: this refuses but no longer writes the
                    # durable ACCOUNT_CLERK hold. That hold is account-scoped
                    # and gates admission for *every* bot, so raising it from
                    # one bot's entry on a single sample is what froze the
                    # account. `StreamHealthHoldSync` owns it now, on its own
                    # cadence with debounce; this check stays as the
                    # consumer-side guard so no entry escapes while the
                    # durable record catches up.
                    return rejected(
                        reason_code=STREAM_HEALTH_REASON_CODE,
                        explanation="The required market-data or execution channel is unhealthy.",
                        next_step="Restore both channels and let the Clerk observe fresh health evidence.",
                    )
                # #1671: the caller's own liveness check happens before this
                # sole-writer boundary is even reached, racing whatever
                # queues ahead of it under `self._intake`. Recheck here,
                # immediately before the entry is accepted, so evidence that
                # went stale or turned HALTED while queued cannot still
                # reach the broker — the same shared predicate
                # bot_trade_strategy.py's own gate uses, so the two can
                # never silently diverge.
                liveness_guard: Callable[[], str | None] | None = None
                if self.authority_kind in _LIVE_MARKET_CLOCK_AUTHORITIES:
                    liveness = market_liveness_fact(entry.instrument.underlying, self._repo.clock())
                    policy = MarketEntryPolicy(
                        use_rth=use_rth,
                        extended_window=self._program_leg_policy.window, clock=self._repo.clock,
                        extended_session_live=lambda: self._stream_health is not None
                        and self._stream_health.market_data_live(entry.instrument.underlying),
                    )
                    if policy.refusal(liveness) is not None:
                        return rejected(
                            reason_code=MARKET_LIVENESS_BLOCKED,
                            # The fact's own plain words say which evidence
                            # refused -- e.g. that the session has closed (#2596).
                            explanation=liveness.reason,
                            next_step=_ENTRY_NOT_RETRIED,
                        )
                    liveness_guard = policy.submission_guard(
                        lambda: market_liveness_fact(entry.instrument.underlying, self._repo.clock()),
                        admitted=liveness,
                    )
                # #2596: every authority, a Dry Run's included -- the calendar
                # needs no live evidence, and the sandbox must predict this.
                decision_close_ms = None if retained_source_bar is None else retained_source_bar.end_ms
                session_refusal = _market_enter_session_refusal(
                    operation_leg, self._repo.clock(), decision_close_ms=decision_close_ms
                )
                if session_refusal is not None:
                    return rejected(
                        reason_code=MARKET_CLOSED,
                        explanation=session_refusal,
                        next_step=_ENTRY_NOT_RETRIED,
                    )
                before_submit = _enter_submission_guard(
                    operation_leg, clock=self._repo.clock, liveness=liveness_guard,
                    decision_close_ms=decision_close_ms,
                )
                try:
                    accepted_enter = accept_enter(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=durable_decision_id,
                        lifecycle_run_id=run_id,
                        leg=operation_leg,
                        decision_receipt=atomic_receipt,
                        envelope=self._live_envelope,
                        # Preserve the recorded pricing-bar price through the
                        # canonical Decimal normalization boundary.
                        reference_price=None if priced_by is None else priced_by.close,
                    )
                except AdmissionBlockedError as exc:
                    return self._entry_admission_blocked(exc, decision_evidence=decision_evidence, rejected=rejected)
            else:
                in_progress = self._answer_from_active_exit(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    decision_id=decision_id,
                    purpose=purpose,
                    action_plan=action_plan,
                    quantity=quantity,
                    atomic_receipt=atomic_receipt,
                )
                if in_progress is not None:
                    return in_progress
                # The strategy-wide check above already answered a symbol an
                # EXIT owns, so the target's ``exit_owned`` is always false here.
                target = newest_reducible_entry(self._repo, strategy_instance_id, entry.instrument.underlying)
                if target is None:
                    return rejected(
                        reason_code="EXIT_CUSTODY_UNPROVEN",
                        explanation=(
                            f"No SQLite-owned entry exists for {entry.instrument.underlying!r}; "
                            "the Clerk cannot prove a safe reduction target."
                        ),
                        next_step="Reconcile the instance custody before attempting another EXIT.",
                    )
                try:
                    accepted_exit = accept_exit(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=durable_decision_id,
                        lifecycle_run_id=run_id,
                        entry_order_ref=target.order_ref,
                        decision_receipt=atomic_receipt,
                        # Durable with the acceptance, not with this call:
                        # a deferred cancel-and-prove leaves the reducing
                        # order to a later sweep that knows no decision.
                        program_leg=program_leg,
                        # So is the bar a simulation fills this EXIT at
                        # (#2919): a process death between the send request
                        # and the simulated fill leaves the later pass that
                        # re-drives it with no in-memory binding, and this is
                        # the only durable record of the decision's price.
                        decision_pricing=(
                            None
                            if not self.simulates_fills or priced_by is None or retained_source_bar is None
                            else DecisionPricingFacts(
                                bar_identity=priced_by.bar_identity,
                                decided_at_ms=retained_source_bar.end_ms,
                            )
                        ),
                    )
                except UnknownEntryOrderError:
                    # The lookup and accept both occur under the Clerk intake
                    # lock.  If the target vanished despite that, return a
                    # closed custody result rather than ending the bot task
                    # while economic exposure may still exist.
                    return rejected(
                        reason_code="EXIT_CUSTODY_UNPROVEN",
                        explanation="The Clerk could not prove that the selected entry still belongs to this EXIT.",
                        next_step="Reconcile the instance custody before attempting another EXIT.",
                    )

        trade: BrokerTradePort
        if self.simulates_fills:
            assert priced_by is not None and retained_source_bar is not None
            # The decision's close tells the port an older bar from the
            # decision minute's own (``SynthesizedOrderLedger.bind_evaluated_bar``).
            trade = _PricingBarBoundTradePort(self._trade, priced_by, decided_at_ms=retained_source_bar.end_ms)
        else:
            trade = self._trade

        if purpose is EffectPurpose.ENTER:
            submitted_enter = await submit_accepted_enter(
                self._repo,
                accepted=accepted_enter,
                leg=operation_leg,
                trade=trade,
                before_submit=before_submit,
            )
            order_refs = (
                (submitted_enter.order_ref,) if submitted_enter.order_ref is not None else ()
            )
            return _effect_receipt(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                decision_id=decision_id,
                purpose=purpose,
                action_plan=action_plan,
                quantity=quantity,
                state=submitted_enter.command.state,
                order_refs=order_refs,
            )

        submitted_exit = await resolve_accepted_exit(
            self._repo,
            accepted=accepted_exit,
            trade=trade,
            pricing=self.recovery_pricing,
            # A deciding program's EXIT is sent with no account check (#2839).
            read=None,
        )
        order_refs = tuple(
            ref
            for ref in (submitted_exit.entry_order_ref, submitted_exit.reducing_order_ref)
            if ref is not None
        )
        return _effect_receipt(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
            state=submitted_exit.command.state,
            order_refs=order_refs,
        )

    async def recover(self) -> None:
        """Retire pre-restart runs, then recover broker-facing operations."""
        async with self._intake:
            for instance in self._repo.strategy_instances():
                strategy_instance_id = instance["strategy_instance_id"]
                active = self._repo.active_run(strategy_instance_id)
                if active is not None:
                    submit_stop_run(
                        self._repo,
                        account_id=self.account_id,
                        strategy_instance_id=strategy_instance_id,
                        lifecycle_run_id=active.lifecycle_run_id,
                        operator_reason="service_restart_recovery",
                        clock=self._repo.clock,
                    )
        result = await self._reconcile()
        if result.verdict == "stale":
            raise StartupBrokerTruthUnavailable(result.stale_cause)

    async def reconcile_once(self) -> ReconciliationVerdict:
        return _legacy_verdict((await self._reconcile()).verdict)

    async def _reconcile(self) -> AccountReconciliationResult:
        return self.publish_reconciliation(
            await reconcile_sqlite_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger="AUTOMATIC",
                intake=self._intake,
                # A restart re-drives every EXIT it finds under the same
                # send-time rule the runner and the sweep apply (#2440).
                pricing=self.recovery_pricing,
                run_ownership=self._run_ownership,
            )
        )

    def publish_reconciliation(
        self, result: AccountReconciliationResult
    ) -> AccountReconciliationResult:
        """Record the verdict reads project from, dated when it was reached.

        Public because the periodic :class:`ReconciliationSweep` -- not this
        facade -- is what actually reconciles in production. The sweep calls
        ``reconcile_account`` directly, so without publishing here the
        projection would answer ``stale`` forever however many sweeps
        succeeded. Reads would stay pure and stop telling the truth.
        """
        self._last_published = _PublishedReconciliation(
            result=result,
            observed_at_ms=(
                self._repo.clock() if result.verdict_at_ms is None else result.verdict_at_ms
            ),
            cut=(
                None
                if result.through_sequence is None
                else ReconciliationCut(
                    ledger=_clerk_generation(self._repo.control_meta_snapshot()),
                    after_sequence=result.through_sequence,
                )
            ),
        )
        return result

    def publish_sweep_reconciliation(
        self, result: AccountReconciliationResult
    ) -> AccountReconciliationResult:
        """The periodic sweep's publish seam: verdict plus sweep liveness.

        The reconciliation sweep wires ``on_result`` here instead of
        :meth:`publish_reconciliation` so the #1808 evaluation observation's
        liveness timestamp is attributable to the *periodic* sweep alone. A
        one-off admission or action reconciliation still publishes the
        verdict (reads must project it), but must not masquerade as proof
        the background sweep is alive.
        """
        published = self.publish_reconciliation(result)
        self._last_sweep_pass_completed_at_ms = self._repo.clock()
        self._begin_buying_power_round()
        return published

    def _begin_buying_power_round(self) -> None:
        """Start one round of the comparison with the account's option buying power, unless one is running.

        After each pass of the periodic sweep (ADR 0076 decision 26). Nothing
        is started on an account with no option entry to compare, and
        nothing waits for the round: a pass is published as before.
        """
        watch, running = self._buying_power, self._buying_power_round
        if watch is None or not watch.has_work or (running is not None and not running.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Published from outside an event loop: there is nothing to run a round on. The next pass asks again.
            return
        self._buying_power_round = loop.create_task(self.compare_entries_with_buying_power())
        self._buying_power_round.add_done_callback(self._buying_power_round_done)

    def _buying_power_round_done(self, completed: asyncio.Task[tuple[str, ...]]) -> None:
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "a round of the comparison with the account's option buying power ended with an error",
                extra={"action": "option_buying_power_round_failed", "account_id": self._repo.account_id},
                exc_info=(type(error), error, error.__traceback__),
            )

    async def compare_entries_with_buying_power(self) -> tuple[str, ...]:
        """One round of the comparison of this account's option entries with its option buying power (ADR 0076).

        For each sent option entry not yet compared: once the books prove
        what it came to, the account is read a second time; once a pass of
        the account has reached its verdict after that reading, the pair is
        judged and recorded on the ENTER's own timeline. An
        under-reserved result, judged, raises that bot's notice in the same
        locked write. Answers the order references recorded this round.

        Nothing on an account that is a simulation, and nothing for an
        entry in shares.
        """
        if self._buying_power is None:
            return ()
        published = self._last_published
        return await self._buying_power.round(
            last_pass_at_ms=None if published is None or published.cut is None else published.observed_at_ms,
            last_pass_clean=published is not None and published.result.verdict == "clean",
        )

    @property
    def compares_option_buying_power(self) -> bool:
        """Whether this account's option entries are compared with a broker's option buying power.

        False on a simulation: it has no broker figure to compare with.
        """
        return self._buying_power is not None

    async def review_option_buying_power_hold(self, strategy_instance_id: str, *, operator: str) -> bool:
        """The owner has reviewed one bot's under-reserved notice: end it (ADR 0076 decision 26).

        False when the bot had none open. The bot may enter again from the
        next decision; the comparisons the notice named stay as recorded.
        """
        async with self._intake:
            return review_under_reserved_hold(
                self._repo, strategy_instance_id=strategy_instance_id, operator=operator
            )

    def recovery_evaluation_observation(self) -> RecoveryEvaluationObservation:
        """The sweep's evaluation posture, for Start/Resume admission (#1808).

        Passive by construction: two process-local timestamps, no broker
        contact, no ledger append. The consumer applies the staleness bounds
        (``bot_start_admission``); this facade only stamps the observations.
        """
        return RecoveryEvaluationObservation(
            evaluation_started_at_ms=self._evaluation_started_at_ms,
            last_pass_completed_at_ms=self._last_sweep_pass_completed_at_ms,
        )

    async def reconcile_account(
        self,
        *,
        trigger: Trigger,
    ) -> AccountReconciliationResult:
        """Run one full account reconciliation with caller-authored provenance."""
        return self.publish_reconciliation(
            await reconcile_sqlite_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger=trigger,
                intake=self._intake,
                # The same seam every other EXIT driver names (#2440): the
                # watchdog prices extended-hours re-drive limits from it, and
                # an EXIT this pass creates a reduction for is re-priced from
                # it (#2229).
                pricing=self.recovery_pricing,
                run_ownership=self._run_ownership,
            )
        )

    async def unresolved_effect_count(self, *, subject_id: str | None = None) -> int:
        """Count nonterminal effects, for one custody subject or the account.

        Start admission asks per subject (#1793); a bot may only ever be
        refused by its own unresolved intent. ``None`` answers account-wide,
        which is what the boot-recovery report records.
        """
        return len(self._repo.reconcilable_effect_operations(subject_id=subject_id))

    async def observe_account_quiet(self) -> AccountQuietObservation | None:
        """The account half of this lane's lane-quiet answer (#2154).

        Read-only against the ledger and the broker: it neither reconciles
        nor records an uncertainty when the broker is unreadable, because
        the answer it feeds is a claim about the account and an unreadable
        broker is simply no claim.
        """
        return await observe_account_quiet(self._repo, self._read)

    async def prove_instance_custody(self, strategy_instance_id: str) -> InstanceCustodyProof:
        result = await self._reconcile()
        return self._proof(strategy_instance_id, result)

    async def published_custody(self, strategy_instance_id: str) -> InstanceCustodyProof | None:
        """The latest published pass's custody proof of one bot, with no pass of its own (#2607).

        ``None`` unless that pass's final broker comparison saw every custody
        transition the bot has (``reconciliation_covers`` against the pass's
        own cut): then its verdict proves the bot's custody as a fresh pass
        would. A stop at a bot's end reads this instead of reconciling, so
        every bot ending on one pass is proven by that pass alone.

        A pass older than the sweep's liveness bound is ``None`` too (#2826):
        the account can change at Alpaca with no transition of the bot's (a
        manual sale, a liquidation), and only the next pass would see it. A
        sweep that has stopped publishing must not go on vouching. The bound
        counts from the pass's verdict, the comparison whose age it limits.
        """
        async with self._intake:
            published = self._last_published
            return None if published is None else self._published_proof(published, strategy_instance_id)

    async def published_pass(self, strategy_instance_id: str) -> PublishedPass | None:
        """The latest published pass, as an operator's Flatten gate may read it for one bot (#2839).

        The same pass under the same tests as :meth:`published_custody`:
        ``vouches`` is that proof standing and able to say what the bot holds.
        Its time is the pass's own -- when its verdict was reached -- never
        when it was published, and never the moment this value or that proof
        was built.

        ``None`` before any pass reached a verdict. ``None`` too on an
        authority whose trade port fills inside ``submit`` (a Dry Run's, a
        Shadow's): its Flatten is not checked against an account as it is sent
        (``exit_resolution._send_is_checked_against_the_account``), and only
        that check lets a gate accept the Clerk's own pass, so there the gate
        keeps asking for a Reconcile now.
        """
        if trade_port_folds_simulated_evidence(self._trade):
            return None
        async with self._intake:
            published = self._last_published
            if published is None or published.cut is None:
                return None
            proof = self._published_proof(published, strategy_instance_id)
            return PublishedPass(
                vouches=proof is not None and not proof.unprovable,
                observed_at_ms=published.observed_at_ms,
                through_sequence=published.cut.after_sequence,
            )

    def _published_proof(
        self, published: _PublishedReconciliation, strategy_instance_id: str
    ) -> InstanceCustodyProof | None:
        """``published``'s proof of one bot, or ``None`` when that pass cannot stand for it.

        The tests :meth:`published_custody` and :meth:`published_pass` share.
        Read under the intake lock.
        """
        if (
            published.cut is None
            or self._repo.clock() - published.observed_at_ms > SWEEP_LIVENESS_BOUND_MS
            or not self.reconciliation_covers(published.cut, strategy_instance_id)
        ):
            return None
        return self._proof(strategy_instance_id, published.result)

    async def custody_snapshot(self, strategy_instance_id: str) -> ClerkCustodySnapshot:
        await self._reconcile()
        return await self._custody_from_result(strategy_instance_id, self._published())

    async def custody_snapshot_projection(
        self, strategy_instance_id: str
    ) -> ClerkCustodySnapshot:
        """Project the sweep's latest verdict without reconciling (#1776).

        Reads are pure: the 15 s sweep is the sole automatic reconciler, so a
        read answers from its last verdict.

        Before this process has completed a sweep there is nothing to project,
        which is exactly the existing ``stale`` verdict -- custody is unproven
        until something proves it. Saying so routes through the policy that
        already handles unprovable custody instead of inventing an absence
        case, and the sweep cadence closes the window.
        """
        return await self._custody_from_result(strategy_instance_id, self._published())

    def last_clean_pass_at_ms(self) -> int | None:
        """When the latest published pass found the account clean against Alpaca.

        The instant of that pass's verdict. ``None`` before this process has
        published a pass, or when the latest one was not clean. The 15 s sweep
        writes no durable receipt, so a status line that asks "when was this
        account last checked?" reads this beside the durable rows (#2826).
        """
        published = self._last_published
        if published is None or published.result.verdict != "clean":
            return None
        return published.observed_at_ms

    def _published(self) -> AccountReconciliationResult:
        """The last published verdict.

        Nothing published yet is the existing ``stale`` verdict: custody is
        unproven until something proves it.
        """
        published = self._last_published
        if published is None:
            return AccountReconciliationResult(verdict="stale")
        return published.result

    async def _custody_from_result(
        self,
        strategy_instance_id: str,
        result: AccountReconciliationResult,
    ) -> ClerkCustodySnapshot:
        """Build the custody snapshot a verdict implies.

        Shared by the reconciling path and the read projection so the two can
        never disagree about what a verdict means. Broker contact here would
        raise ``BrokerCallUnderIntakeError`` by construction.
        """
        async with self._intake:
            proof = self._proof(strategy_instance_id, result)
            meta = self._repo.control_meta_snapshot()
            exposure = {
                symbol: qty
                for symbol, qty in proof.exposure.items()
                if position_quantity_is_nonzero(qty)
            }
            working = self._working_order_refs_for_proof(strategy_instance_id)
            unresolved = self._unresolved_order_refs(strategy_instance_id)
            trusted = proof.reconciliation_verdict == "clean"
            # A stale pass that could not read the broker is a different
            # answer from one that read it and could not prove it: nothing
            # observed contradicts the account, so the recovery the snapshot
            # offers is a recheck, not an operator reconciliation (#2901).
            # ``stale_cause`` is in-process and set only when the read
            # itself failed, which is exactly that distinction.
            broker_unreachable = not trusted and result.stale_cause is not None
            if trusted:
                reason_code, next_step = "CLERK_CUSTODY_PROVEN", None
            elif broker_unreachable:
                reason_code = CLERK_CUSTODY_BROKER_UNREACHABLE
                next_step = "Check again when the connection to Alpaca recovers."
            else:
                reason_code, next_step = (
                    "CLERK_CUSTODY_UNPROVABLE",
                    "Reconcile the SQLite Account Clerk before starting new exposure.",
                )
            return ClerkCustodySnapshot(
                broker=self.broker_id,
                account_id=self.account_id,
                account_mode=self._account_mode,
                strategy_instance_id=strategy_instance_id,
                clerk_generation=_clerk_generation(meta),
                journal_sequence=meta.control_revision,
                reconciliation_state=proof.reconciliation_verdict,
                reconciliation_fresh=proof.reconciliation_verdict != "stale",
                exposure=(
                    CustodyExposureFact(
                        state="non_zero" if exposure else "zero",
                        positions=exposure,
                    )
                    if trusted
                    else CustodyExposureFact(state="unknown")
                ),
                working_orders=_count_fact(len(working), trusted=trusted),
                pending_orders=_count_fact(len(unresolved), trusted=trusted),
                unresolved_effects=_count_fact(len(unresolved), trusted=trusted),
                hold=_hold_state(self._repo, strategy_instance_id),
                freeze=proof.freeze,
                reason_code=reason_code,
                evidence_refs=(
                    f"alpaca-clerk-sqlite:{self.account_id}:{meta.authority_generation}:{meta.control_revision}",
                ),
                next_step=next_step,
                observed_at_ms=self._repo.clock(),
            )

    @asynccontextmanager
    async def start_admission_snapshot(self, strategy_instance_id: str) -> AsyncIterator[ClerkCustodySnapshot]:
        snapshot = await self.custody_snapshot(strategy_instance_id)
        yield snapshot

    @asynccontextmanager
    async def start_admission_projection(
        self, strategy_instance_id: str
    ) -> AsyncIterator[ClerkCustodySnapshot]:
        """Read-only twin of :meth:`start_admission_snapshot` (#1776 WP2)."""
        yield await self.custody_snapshot_projection(strategy_instance_id)

    async def reconcile_through(self) -> ReconciliationCut:
        """Reconcile the account once and name the ledger point the pass began after.

        The same pass :meth:`custody_snapshot` runs, so a caller acting on
        many bots can reconcile once for all of them instead of once per bot
        (clearing finished bots, #2567). The point is read before the pass
        waits for the reconciliation lock: anything written after it --
        including by this pass -- is newer than the cut.
        """
        after_sequence = self._repo.last_custody_sequence()
        await self._reconcile()
        return ReconciliationCut(
            ledger=_clerk_generation(self._repo.control_meta_snapshot()),
            after_sequence=after_sequence,
        )

    def reconciliation_covers(self, cut: ReconciliationCut, strategy_instance_id: str) -> bool:
        """Whether ``cut``'s pass still proves this bot's custody, with no reconcile of its own.

        True only on this same ledger, when the bot's newest custody
        transition is no later than the point the pass began after: the bot's
        orders, effects and fills are then exactly what the pass compared
        against the broker. A bot order can only reach the broker after its
        effect's transition is written, so nothing of the bot's could have
        changed at the broker since without a newer transition. Reads project
        the latest published verdict, which is this pass's or a later one:
        passes are serialized and each publishes as it completes.
        """
        meta = self._repo.control_meta_snapshot()
        return (
            cut.ledger == _clerk_generation(meta)
            and self._repo.last_custody_sequence(strategy_instance_id) <= cut.after_sequence
        )

    async def discharge_attributed_residue(
        self,
        *,
        strategy_instance_id: str,
        symbol: str,
        reason: str | None,
    ) -> ResidueDischargeReceipt:
        """Zero one bot's stranded residue once a fresh broker read proves it (#2381)."""
        return await discharge_attributed_residue(
            self._repo,
            read=self._read,
            intake=self._intake,
            strategy_instance_id=strategy_instance_id,
            symbol=symbol,
            operator_reason=reason,
        )

    async def end_expired_option(
        self,
        *,
        strategy_instance_id: str,
        symbol: str,
        operator: str,
        reason: str | None,
    ) -> OwnerEndingReceipt:
        """End one bot's expired option contract at the owner's word, after one look more (ADR 0076 decision 22).

        The look is this Clerk's own poll of option events, made now on its
        own ports: a Dry Run's simulation is asked for its price once more,
        and a real account's option events and positions are read. A
        settlement that has come is booked in the ending's place.
        """
        return await end_option_at_the_owners_word(
            self._repo,
            read=self._read,
            simulation=option_settlement_simulation(self._trade),
            off_loop=self._intake.off_loop,
            request=OwnerEndingRequest(
                strategy_instance_id=strategy_instance_id, symbol=symbol, operator=operator, reason=reason
            ),
        )

    async def acknowledge_option_event_hold(
        self, *, request: HoldAcknowledgementRequest, operator: str
    ) -> HoldAcknowledgementReceipt:
        """Acknowledge one option-event hold, on a reconciliation pass made now for it (ADR 0076 decision 22).

        The pass is this Clerk's own, on its own ports. It reads and books
        the account's option events as every pass does and compares the
        account with the broker. The acknowledgement is refused, and
        nothing written, unless that comparison shows the books agree with
        the broker in everything the hold names
        (``option_event_acknowledgement``).
        """
        return await acknowledge_option_event_hold_after_a_pass(
            self._repo,
            reconcile=lambda: self.reconcile_account(trigger="AUTOMATIC"),
            off_loop=self._intake.off_loop,
            request=request,
            operator=operator,
        )

    async def settle_unknown_exit_order(
        self,
        *,
        strategy_instance_id: str,
        presented: ProjectedUnknownExitOrder,
        attestation: OrderOutcomeAttestation,
        operator: str,
        reason: str | None,
    ) -> UnknownOrderSettlementReceipt:
        """Settle one bot's closing order on an operator's attestation, once this Clerk's own reads agree (#2854)."""
        return await settle_unknown_exit_order(
            self._repo,
            read=self._read,
            trade=self._trade,
            intake=self._intake,
            strategy_instance_id=strategy_instance_id,
            presented=presented,
            attestation=attestation,
            operator=operator,
            operator_reason=reason,
        )

    async def check_unknown_exit_order(
        self,
        *,
        strategy_instance_id: str,
        presented: ProjectedUnknownExitOrder,
        attestation: OrderOutcomeAttestation,
    ) -> AttestationFindings:
        """What this Clerk reads at the broker for an attestation, settling nothing (#2854)."""
        return await check_unknown_exit_order(
            self._repo,
            read=self._read,
            trade=self._trade,
            intake=self._intake,
            strategy_instance_id=strategy_instance_id,
            presented=presented,
            attestation=attestation,
        )

    async def cancel_verified_working_orders(
        self,
        *,
        strategy_instance_id: str | None,
        order_refs: tuple[str, ...],
    ) -> tuple[OrderResource, ...]:
        """Cancel and exact-poll only policy-authorized owned ENTRY refs."""
        if len(order_refs) != len(set(order_refs)):
            raise ValueError("order_refs must not contain duplicates")
        async with self._intake:
            requested: list[OrderResource] = []
            for order_ref in order_refs:
                order = self._repo.order(order_ref)
                if order is None or order.role != "ENTRY":
                    raise ValueError(f"{order_ref!r} is not an owned ENTRY order")
                owner = self._repo.effect_operation(order.effect_operation_id)
                if owner is None:
                    raise RuntimeError(f"{order_ref!r} has no durable owning effect")
                if strategy_instance_id is not None and owner.strategy_instance_id != strategy_instance_id:
                    raise ValueError(f"{order_ref!r} is not owned by strategy {strategy_instance_id!r}")
                requested.append(order)

        resolved: list[OrderResource] = []
        for order in requested:
            if is_working_order(order):
                order = await cancel_and_prove_owned_entry(
                    self._repo,
                    entry_order_ref=order.order_ref,
                    trade=self._trade,
                )
            resolved.append(order)
        return tuple(resolved)

    def _proof(
        self,
        strategy_instance_id: str,
        result: AccountReconciliationResult,
    ) -> InstanceCustodyProof:
        verdict = _legacy_verdict(result.verdict)
        working = self._working_order_refs_for_proof(strategy_instance_id)
        unresolved = self._unresolved_order_refs(strategy_instance_id)
        freeze = _freeze_state(result, observed_at_ms=self._repo.clock())
        if not freeze.active:
            freeze = _failed_enter_filled_freeze(
                self._repo, strategy_instance_id, observed_at_ms=self._repo.clock()
            )
        exposure = {
            symbol: quantity
            for symbol, quantity in self._repo.attributed_positions_for_strategy(
                strategy_instance_id
            ).items()
            if position_quantity_is_nonzero(quantity)
        }
        return InstanceCustodyProof(
            account_id=self.account_id,
            strategy_instance_id=strategy_instance_id,
            reconciliation_verdict=verdict,
            freeze=freeze,
            exposure=exposure,
            working_order_refs=working,
            unresolved_intent_refs=unresolved,
            observed_at_ms=self._repo.clock(),
        )

    def _working_order_refs_for_proof(self, strategy_instance_id: str) -> tuple[str, ...]:
        """Every still-working order (ENTRY or REDUCING) for custody proof (``working_order_refs``)."""
        return working_order_refs(self._repo, strategy_instance_id)

    def _unresolved_order_refs(self, strategy_instance_id: str) -> tuple[str, ...]:
        """Order intents whose broker outcome is not yet known (``unresolved_order_refs``)."""
        return unresolved_order_refs(self._repo, strategy_instance_id)


def _entry_leg(action_plan: ActionPlan) -> StockEntryLeg:
    if len(action_plan.on_enter) != 1 or not isinstance(action_plan.on_enter[0], StockEntryLeg):
        raise ValueError("SQLite Alpaca effects require exactly one stock entry leg")
    return action_plan.on_enter[0]


def _order_pricing_bar(
    traded_symbol: str,
    *,
    purpose: EffectPurpose,
    synthesized: bool,
    exit_sells: bool,
    decision_bar: RetainedSourceBar | None,
    pricing_bar: RetainedSourceBar | None,
) -> RetainedSourceBar | LegRefusal | None:
    """The one bar this effect's program order is priced from, or why it has none (#2912).

    The cash reservation, an extended-session limit's anchor and a synthesized
    fill all read this bar, so no caller prices an order from the Signal
    stream by reading the decision bar directly.

    The Clerk checks the bar itself rather than trusting the caller's label: it
    must be the traded instrument's, and it must close with the decision. A
    decision bar of the traded instrument is its own pricing bar, which is
    every bot that trades the symbol it listens to.

    ``None`` means the order is shaped with no pricing bar:

    - With no decision bar nothing says when the decision closed, so no bar
      can be shown to close with it and none is accepted. That case keeps the
      refusals it always had (a synthesized authority's unproven source bar,
      a market ENTER with nothing to bound it against cash).
    - A broker-sent EXIT is not refused here: a regular-session market leg
      needs no price. An extended-session leg does, and its shaper refuses it.

    An ENTER is never priced from another asset or another minute, and a
    synthesized fill of either purpose is a bar's close, so those are refused
    when no bar of the order's own can price them.

    A synthesized EXIT that sells is the one order an earlier minute may
    price. When the traded instrument has no bar for the decision's minute its
    runner offers the newest earlier one, and the fill carries that bar's own
    time beside the decision's. That price fills a market sale only: the
    caller refuses the sale once a market order can no longer be sent. An
    older price never buys, so the EXIT of a short position (``exit_sells``
    false), which buys to cover, is held to its own minute like an ENTER.
    """
    if decision_bar is None:
        return None
    if pricing_bar is None:
        if _same_symbol(decision_bar.symbol, traded_symbol):
            return decision_bar
        unpriced = PRICING_BAR_UNAVAILABLE
    elif not _same_symbol(pricing_bar.symbol, traded_symbol):
        unpriced = PRICING_BAR_MISMATCH
    elif pricing_bar.end_ms == decision_bar.end_ms or (
        purpose is EffectPurpose.EXIT and exit_sells and synthesized and pricing_bar.end_ms < decision_bar.end_ms
    ):
        return pricing_bar
    else:
        unpriced = PRICING_BAR_MISMATCH
    return unpriced if purpose is EffectPurpose.ENTER or synthesized else None


def _same_symbol(left: str, right: str) -> bool:
    return left.upper() == right.upper()


def _append_pre_custody_refusal(
    repository: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    run_id: str,
    evidence: EffectDecisionEvidence,
    reason_code: str,
    explanation: str,
) -> None:
    """Durably close one evaluation before returning a custody refusal."""
    repository.append_decision_receipt(
        strategy_instance_id=strategy_instance_id,
        outcome="blocked",
        symbol=evidence.symbol,
        intent_id=evidence.evaluation_id,
        order_ref=None,
        observed_at_ms=evidence.observed_at_ms,
        facts_json=canonicalize(
            {
                "bar_ref": evidence.bar_ref,
                "decision_id": evidence.evaluation_id,
                "evaluation_id": evidence.evaluation_id,
                "run_id": run_id,
                "reason_code": reason_code,
                "refusal_reason": explanation,
                "retention_class": "protected_refusal",
                "trace_digest": evidence.trace_digest,
                "decision_bar_close_ms": evidence.decision_bar_close_ms,
                **_explanation_facts(evidence),
            }
        ),
    )


def _atomic_decision_receipt(decision_evidence: EffectDecisionEvidence | None) -> AtomicDecisionReceipt | None:
    """The decision receipt an effect commits with its acceptance, or ``None`` for a caller that handed no evidence."""
    if decision_evidence is None:
        return None
    return AtomicDecisionReceipt(
        outcome=decision_evidence.outcome,
        symbol=decision_evidence.symbol,
        decision_id=decision_evidence.evaluation_id,
        observed_at_ms=decision_evidence.observed_at_ms,
        facts_json=canonicalize(
            {
                "bar_ref": decision_evidence.bar_ref,
                "decision_id": decision_evidence.evaluation_id,
                "evaluation_id": decision_evidence.evaluation_id,
                "reason_code": decision_evidence.reason_code,
                "trace_digest": decision_evidence.trace_digest,
                "decision_bar_close_ms": decision_evidence.decision_bar_close_ms,
                # Omitted when on time, so an on-time receipt's
                # facts stay byte-identical to every earlier one.
                **(
                    {}
                    if decision_evidence.decision_lateness_ms is None
                    else {"decision_lateness_ms": decision_evidence.decision_lateness_ms}
                ),
                **_explanation_facts(decision_evidence),
            }
        ),
    )


def _explanation_facts(evidence: EffectDecisionEvidence) -> dict[str, object]:
    """The decision explanation as a receipt fact, omitted when there is none (#2639)."""
    if evidence.explanation is None:
        return {}
    return {"explanation": evidence.explanation.model_dump(mode="json")}


def _market_enter_session_refusal(
    leg: BrokerOrderLeg, now_ms: int, *, decision_close_ms: int | None
) -> str | None:
    """Why a market ENTER leg may not be sent at ``now_ms``, or ``None`` when it may (#2596).

    The rule every reducing market leg already obeys
    (``recovery_reduction.market_leg_sendable``): Alpaca holds a market DAY
    order that arrives after the close until the next open, so the leg must
    reach the broker inside the canonical calendar's regular session, early
    closes included, judged a few seconds ahead. The broker clock's close in
    the liveness fact cannot give that margin: a Clerk clock running behind,
    or a send a tenth of a second before the close, still reads it open. A
    priced leg is an extended-hours limit, bounded where it is shaped.

    An extended-hours run's bar that closes at the open is shaped as a market
    leg, and a Clerk clock running behind still reads pre-market when that
    decision arrives (#2637). The entry is missed, which is safe, and the
    refusal names the lag: the decision came after its bar closed, so the
    clock is at least that far behind.
    """
    if leg.order_type is not OrderType.MARKET or market_leg_sendable(now_ms):
        return None
    if decision_close_ms is not None:
        open_ms = session_open_ms_utc(et_date_at_ms(decision_close_ms))
        if now_ms < open_ms <= decision_close_ms:
            return (
                f"The Clerk's clock is running at least {decision_close_ms - now_ms:,} ms behind: "
                "it still reads before the regular open, though the bar this entry was decided "
                "on closed at or after it. A market order goes out only inside the regular "
                "session, so this entry was missed."
            )
    return _MARKET_ENTRY_AFTER_THE_CLOSE


def _enter_submission_guard(
    leg: BrokerOrderLeg,
    *,
    clock: Callable[[], int],
    liveness: Callable[[], str | None] | None,
    decision_close_ms: int | None,
) -> Callable[[], EntrySubmissionRefusal | None]:
    """The check just before broker contact: the intake's two refusals, asked again under their own codes."""

    def refusal() -> EntrySubmissionRefusal | None:
        if liveness is not None and (refused := liveness()) is not None:
            return EntrySubmissionRefusal(summary_code=MARKET_LIVENESS_BLOCKED, why=refused)
        closed = _market_enter_session_refusal(leg, clock(), decision_close_ms=decision_close_ms)
        return None if closed is None else EntrySubmissionRefusal(summary_code=MARKET_CLOSED, why=closed)

    return refusal


def _durable_decision_id(decision_id: str) -> str:
    """Encode delimiter-bearing legacy decision IDs without losing identity."""
    if ":" not in decision_id and not decision_id.startswith(_ENCODED_DECISION_PREFIX):
        return decision_id
    encoded = base64.urlsafe_b64encode(decision_id.encode("utf-8")).rstrip(b"=")
    return f"{_ENCODED_DECISION_PREFIX}{encoded.decode('ascii')}"


def decision_id_from_durable(durable_decision_id: str) -> str:
    """The decision id ``_durable_decision_id`` stored, as its caller named it.

    A recovery EXIT's colon-free id (``recovery-flatten-…``) was never
    encoded and comes back unchanged.
    """
    if not durable_decision_id.startswith(_ENCODED_DECISION_PREFIX):
        return durable_decision_id
    encoded = durable_decision_id.removeprefix(_ENCODED_DECISION_PREFIX)
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")


def _strategy_display_name(strategy_key: str) -> str:
    """Resolve the engine-owned immutable presentation label for one strategy."""
    from app.services.strategy_validation_manifest import strategy_registry_seeds

    for strategy in strategy_registry_seeds():
        if strategy.strategy_key == strategy_key:
            return strategy.display_name
    raise StrategyRegistrationConflictError(
        f"strategy {strategy_key!r} has no registered immutable display name"
    )


def _asks_for_another_entry(first: OptionEntry, *, units: int, evidence: OptionEntryEvidence | None) -> bool:
    """Whether an option ENTER sent again under one decision asks for something the first did not.

    Another number of structures, or another contract for a leg the first
    entry opened: every leg is compared with its own. The quotes and so the
    price are expected to differ: a retry reads them again. A retry that
    names no contract for a leg asks for none.
    """
    if units != first.units:
        return True
    picked = {} if evidence is None else evidence.picked
    return any(
        (again := picked.get(leg.leg_id)) is not None and again != leg.contract for leg in first.legs
    )


def _effect_receipt(
    *,
    strategy_instance_id: str,
    run_id: str,
    decision_id: str,
    purpose: EffectPurpose,
    action_plan: ActionPlan,
    quantity: int,
    state: str,
    order_refs: tuple[str, ...],
    explanation: str | None = None,
    next_step: str | None = None,
) -> EffectOperationReceipt:
    from app.broker.alpaca.clerk.models import AlpacaEffectOperation

    mapped = {
        "reserved": EffectOperationState.ACCEPTED,
        "accepted": EffectOperationState.ACCEPTED,
        "in_progress": (
            EffectOperationState.SUBMITTED if purpose is EffectPurpose.ENTER else EffectOperationState.EXIT_PENDING
        ),
        "unknown": EffectOperationState.UNCERTAIN,
        "succeeded": (EffectOperationState.SUBMITTED if purpose is EffectPurpose.ENTER else EffectOperationState.FLAT),
        "failed": EffectOperationState.REJECTED,
        "rejected": EffectOperationState.REJECTED,
    }[state]
    return EffectOperationReceipt(
        operation=AlpacaEffectOperation(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
        ),
        state=mapped,
        explanation=explanation or f"The SQLite Account Clerk recorded {purpose.value} as {state}.",
        next_step=next_step
        if next_step is not None
        else (
            "Await fresh broker evidence and automatic reconciliation."
            if mapped
            in {
                EffectOperationState.ACCEPTED,
                EffectOperationState.SUBMITTED,
                EffectOperationState.EXIT_PENDING,
                EffectOperationState.UNCERTAIN,
            }
            else None
        ),
        child_order_refs=order_refs,
    )


def _legacy_verdict(value: str) -> ReconciliationVerdict:
    if value == "position_drift":
        return "missing_intent"
    if value == "clean":
        return "clean"
    if value == "unexplained_order":
        return "unexplained_order"
    if value == "missing_intent":
        return "missing_intent"
    if value == "stale":
        return "stale"
    raise ValueError(f"unsupported SQLite reconciliation verdict {value!r}")


def _freeze_state(
    result: AccountReconciliationResult,
    *,
    observed_at_ms: int,
) -> AccountFreezeState:
    verdict = _legacy_verdict(result.verdict)
    if verdict == "clean":
        return AccountFreezeState()
    category = (
        "ACCOUNT_STATE_UNATTRIBUTABLE"
        if verdict in {"unexplained_order", "missing_intent"}
        else "ACCOUNT_STATE_UNPROVABLE"
    )
    return AccountFreezeState(
        active=True,
        category=category,
        explanation=(
            "The SQLite Account Clerk cannot attribute current broker state."
            if category == "ACCOUNT_STATE_UNATTRIBUTABLE"
            else "The SQLite Account Clerk could not obtain fresh broker proof."
        ),
        next_step="Reconcile the account before allowing new exposure.",
        observed_at_ms=observed_at_ms,
    )


def _failed_enter_filled_freeze(
    repo: ClerkSqliteRepository,
    strategy_instance_id: str,
    *,
    observed_at_ms: int,
) -> AccountFreezeState:
    """This bot's own #2348 fence, read live from its durable episode.

    The account verdict stays ``clean`` -- broker and journal agree on the
    position -- so the proof keeps the exposure known and freezes only the
    fenced bot. Its Start/Resume is refused on that freeze: the strategy
    believes it is flat and would never exit the position it would inherit.
    """
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    )
    if episode is None:
        return AccountFreezeState()
    return AccountFreezeState(
        active=True,
        # The position is real and attributed, but no live decision owns it.
        category="ACCOUNT_STATE_UNATTRIBUTABLE",
        explanation=episode["explanation"],
        next_step=episode["next_step"],
        observed_at_ms=observed_at_ms,
    )


def _hold_state(
    repo: ClerkSqliteRepository,
    strategy_instance_id: str,
) -> HoldState:
    holds = repo.active_holds_for_admission(strategy_instance_id=strategy_instance_id)
    if not holds:
        return HoldState(active=False)
    hold = max(holds, key=lambda row: row["opened_at_ms"])
    return HoldState(
        active=True,
        reason_code=hold["reason_code"],
        reason="The SQLite Account Clerk has an active safety hold.",
        since_ms=hold["opened_at_ms"],
    )


def _count_fact(count: int, *, trusted: bool) -> CustodyCountFact:
    if not trusted:
        return CustodyCountFact(state="unknown")
    return CustodyCountFact(state="non_zero" if count else "zero", count=count)


def _clerk_generation(meta: ControlMetaSnapshot) -> str:
    """This ledger's identity: its authority generation and database token."""
    return f"sqlite:{meta.authority_generation}:{meta.db_identity_token}"


__all__ = [
    "EntryReading",
    "IntakeFencePoisonedError",
    "IntakeFenceYieldError",
    "ReentrantAsyncLock",
    "SqliteAlpacaClerkFacade",
    "StartupBrokerTruthUnavailable",
    "StrategyRegistrationConflictError",
]

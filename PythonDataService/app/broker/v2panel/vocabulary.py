"""Closed operator vocabulary for the broker-v2 bot control panel (S1).

Spec §13 / decision register #7, #10, #18, #19. This module is the **Python
authority** for the ~25-code closed vocabulary the panel renders: phases,
duty-outcome kinds, hold reasons, reconciliation verdicts, channel states,
station ids, station states, and action ids. It is the source of truth the
snapshot/TS-parity mechanism pins (see
``scripts/regenerate_broker_v2_vocabulary_snapshot.py``).

Two invariants this file enforces and the contract tests pin:

- ``PAUSED`` means one live run is held; Continue keeps that run identity.
- Every emitted code carries server-authored copy (``label`` + ``explanation``)
  in :data:`OPERATOR_COPY`; the copy-coverage contract test fails visibly when a
  code is emitted without copy (decision #7 — no open-ended string reaches the
  UI unlabeled).

The vocabulary is *closed*: the frontend renders exactly what it is handed and
executes only the known action ids. A future broker code that is not in this
set is a contract violation, not a silent passthrough.
"""

from __future__ import annotations

from typing import Final, Literal

# ── Phases (bot lifecycle phase, §12) ────────────────────────────────────────
# Narrowed from the shared BotLifecyclePhase; the panel never renders anything
# outside this closed set.
Phase = Literal["OFF_DUTY", "ON_DUTY", "RETIRED"]
PHASES: Final[frozenset[str]] = frozenset({"OFF_DUTY", "ON_DUTY", "RETIRED"})

# ── Desired state (§12) ───────────────────────────────────────────────────────
DesiredState = Literal["RUNNING", "PAUSED", "STOPPED"]
DESIRED_STATES: Final[frozenset[str]] = frozenset({"RUNNING", "PAUSED", "STOPPED"})

# ── Duty-outcome kinds (§7.2) ────────────────────────────────────────────────
# The typed terminal duty facts a bot records on exit (bot_runner exit taxonomy)
# plus the not-yet-exited sentinel the panel shows while a bot is on duty.
DutyOutcomeKind = Literal["ON_DUTY", "STOPPED", "CRASHED", "EXITED_UNVERIFIED"]
DUTY_OUTCOME_KINDS: Final[frozenset[str]] = frozenset(
    {"ON_DUTY", "STOPPED", "CRASHED", "EXITED_UNVERIFIED"}
)

# ── Hold reasons (§7.3) ──────────────────────────────────────────────────────
# The closed set of exposure-hold reason codes the clerk can journal, plus the
# no-hold sentinel.
HoldReason = Literal["NO_HOLD", "UNEXPLAINED_ORDER_HOLD", "STREAM_HEALTH_HOLD"]
HOLD_REASONS: Final[frozenset[str]] = frozenset(
    {"NO_HOLD", "UNEXPLAINED_ORDER_HOLD", "STREAM_HEALTH_HOLD"}
)

# ── Reconciliation verdicts (§7.3) ───────────────────────────────────────────
# Mirrors clerk.models.ReconciliationVerdict exactly (kept in lockstep by the
# contract test test_reconciliation_verdicts_match_clerk_model).
ReconciliationVerdict = Literal["clean", "unexplained_order", "missing_intent", "stale"]
RECONCILIATION_VERDICTS: Final[frozenset[str]] = frozenset(
    {"clean", "unexplained_order", "missing_intent", "stale"}
)

# ── Channel states (§7.3) ────────────────────────────────────────────────────
ChannelState = Literal["healthy", "unhealthy", "unknown"]
CHANNEL_STATES: Final[frozenset[str]] = frozenset({"healthy", "unhealthy", "unknown"})

# ── Station ids (§7.1, the six-station rail) ─────────────────────────────────
StationId = Literal["SIGNAL", "INTENT", "SUBMIT_GATE", "BROKER_ACK", "FILL", "RECONCILED"]
STATION_IDS: Final[tuple[StationId, ...]] = (
    "SIGNAL",
    "INTENT",
    "SUBMIT_GATE",
    "BROKER_ACK",
    "FILL",
    "RECONCILED",
)

# ── Station states (§7, the five states) ─────────────────────────────────────
StationState = Literal["satisfied", "waiting", "blocked", "unknown_stale", "not_applicable"]
STATION_STATES: Final[frozenset[str]] = frozenset(
    {"satisfied", "waiting", "blocked", "unknown_stale", "not_applicable"}
)

# ── Action ids (§11, the closed presented-actions enum) ──────────────────────
ActionId = Literal[
    "deploy",
    "resume",
    "pause",
    "continue",
    "stop",
    "flatten_stop",
    "retire",
    "cancel_order",
    "reconcile_now",
    "recover_exact_execution_evidence",
    "resolve_execution_coverage",
    "cancel_verified_working_orders",
    "prepare_safe_flatten",
    "stop_bot_decisions",
    "open_custody_timeline",
    "rebuild_from_mirror",
    "reset_authority",
]
ACTION_IDS: Final[tuple[ActionId, ...]] = (
    "deploy",
    "resume",
    "pause",
    "continue",
    "stop",
    "flatten_stop",
    "retire",
    "cancel_order",
    "reconcile_now",
    "recover_exact_execution_evidence",
    "resolve_execution_coverage",
    "cancel_verified_working_orders",
    "prepare_safe_flatten",
    "stop_bot_decisions",
    "open_custody_timeline",
    "rebuild_from_mirror",
    "reset_authority",
)


# ── Server-authored copy (decision #7) ───────────────────────────────────────
# One label + explanation per emitted code. The copy-coverage contract test
# fails if any code is missing an entry (or carries a trivial one). This is the
# sole semantic-copy authority; the TS copy map is an emergency fallback.


class OperatorCopy:
    """One code's operator-facing label + explanation (immutable value)."""

    __slots__ = ("explanation", "label")

    def __init__(self, label: str, explanation: str) -> None:
        self.label = label
        self.explanation = explanation


# ``STOPPED`` is both a desired state and a duty-outcome kind. Its duty-outcome
# copy is keyed ``STOPPED_OUTCOME`` so the two senses never collide.
OPERATOR_COPY: Final[dict[str, OperatorCopy]] = {
    # Phases
    "OFF_DUTY": OperatorCopy(
        "Off duty",
        "The bot is not running. It evaluates no bars and places no orders.",
    ),
    "ON_DUTY": OperatorCopy(
        "On duty", "The bot is running and evaluating bars as they close."
    ),
    "RETIRED": OperatorCopy(
        "Retired", "The bot is permanently decommissioned. Its id is never reused."
    ),
    # Desired state
    "RUNNING": OperatorCopy("Running", "The operator wants this bot evaluating bars."),
    "PAUSED": OperatorCopy(
        "Paused",
        "The current run remains alive but bar evaluation is held until Continue.",
    ),
    "STOPPED": OperatorCopy(
        "Stopped", "The operator wants this bot idle. Exposure is left untouched."
    ),
    # Duty outcomes
    "STOPPED_OUTCOME": OperatorCopy(
        "Stopped cleanly", "The bot exited on an operator stop or a service shutdown."
    ),
    "CRASHED": OperatorCopy(
        "Crashed", "The bot exited on an unhandled error. Check the duty reason."
    ),
    "EXITED_UNVERIFIED": OperatorCopy(
        "Exited unverified",
        "The bot's task ended without a clean stop. Its final state is not confirmed.",
    ),
    # Hold reasons
    "NO_HOLD": OperatorCopy(
        "No hold", "No exposure hold is active. Order submission is allowed."
    ),
    "UNEXPLAINED_ORDER_HOLD": OperatorCopy(
        "Unexplained-order hold",
        "An order this account did not submit was seen in the journal. "
        "New submits are paused account-wide.",
    ),
    "STREAM_HEALTH_HOLD": OperatorCopy(
        "Stream-health hold",
        "A market-data or execution channel is unhealthy. "
        "New submits are paused account-wide.",
    ),
    # Reconciliation verdicts
    "clean": OperatorCopy(
        "Clean", "The last sweep found the journal and the broker in agreement."
    ),
    "unexplained_order": OperatorCopy(
        "Unexplained order",
        "The last sweep found a broker order the journal cannot explain.",
    ),
    "missing_intent": OperatorCopy(
        "Missing intent",
        "The last sweep found broker inventory or an owned order that does not "
        "match the durable journal exposure.",
    ),
    "stale": OperatorCopy(
        "Stale",
        "The last sweep could not reach the broker; the verdict is out of date.",
    ),
    # Channel states
    "healthy": OperatorCopy("Healthy", "The channel is connected and current."),
    "unhealthy": OperatorCopy(
        "Unhealthy",
        "The channel is down or lagging. Trading is gated until it recovers.",
    ),
    "unknown": OperatorCopy(
        "Unknown", "The channel's health has not been observed yet."
    ),
    # Station ids
    "SIGNAL": OperatorCopy(
        "Signal", "The bot evaluated a bar and produced (or withheld) a decision."
    ),
    "INTENT": OperatorCopy(
        "Intent", "The bot recorded an order intent before touching the broker."
    ),
    "SUBMIT_GATE": OperatorCopy(
        "Submit gate", "Holds and channel health were checked before submission."
    ),
    "BROKER_ACK": OperatorCopy(
        "Broker ack", "The broker acknowledged (or rejected) the submitted order."
    ),
    "FILL": OperatorCopy("Fill", "The order executed, in full or in part, at the broker."),
    "RECONCILED": OperatorCopy(
        "Reconciled", "A sweep confirmed the journal and the broker agree on this order."
    ),
    # Station states
    "satisfied": OperatorCopy(
        "Satisfied", "This station completed with recorded evidence."
    ),
    "waiting": OperatorCopy(
        "Waiting", "This station is expected to progress. Nothing is wrong."
    ),
    "blocked": OperatorCopy(
        "Blocked",
        "An identified condition is preventing this station from progressing.",
    ),
    "unknown_stale": OperatorCopy(
        "Unknown (stale)", "Evidence for this station exists but is too old to trust."
    ),
    "not_applicable": OperatorCopy(
        "Not applicable", "This broker or mode has no such station."
    ),
    # Action ids
    "deploy": OperatorCopy(
        "Deploy",
        "Create and start a new bot bound to this account. "
        "The bot begins evaluating bars immediately after creation.",
    ),
    "resume": OperatorCopy(
        "Resume",
        "Create a new run of this unchanged strategy instance after backend admission.",
    ),
    "pause": OperatorCopy(
        "Pause",
        "Hold bar evaluation while keeping the current process and run identity alive.",
    ),
    "continue": OperatorCopy(
        "Continue",
        "Let this paused live run evaluate bars again without changing its run ID.",
    ),
    "stop": OperatorCopy(
        "Stop",
        "Stop evaluating bars and cancel this bot's working entry orders. "
        "Exposure is left untouched.",
    ),
    "flatten_stop": OperatorCopy(
        "Flatten & stop",
        "Cancel working orders, submit closing orders to flatten exposure, then stop. "
        "Use this to exit positions before stopping.",
    ),
    "retire": OperatorCopy(
        "Retire",
        "Permanently decommission this bot. Its id is never reused. This is irreversible.",
    ),
    "cancel_order": OperatorCopy(
        "Cancel order",
        "Cancel one working order at the broker. "
        "The broker may reject the request if the order has already filled.",
    ),
    "reconcile_now": OperatorCopy(
        "Reconcile now",
        "Run a reconciliation sweep against the broker immediately. "
        "Useful after a hold is cleared or after a manual order intervention.",
    ),
    "recover_exact_execution_evidence": OperatorCopy(
        "Recover exact execution evidence",
        "Read one retained Alpaca paper execution and prepare the Clerk's no-delta coverage proof.",
    ),
    "resolve_execution_coverage": OperatorCopy(
        "Resolve execution coverage",
        "Replace one matching cumulative recovery record with verified exact execution evidence.",
    ),
    "cancel_verified_working_orders": OperatorCopy(
        "Cancel verified working orders",
        "Cancel only working orders whose exact Clerk and broker identities are proven.",
    ),
    "prepare_safe_flatten": OperatorCopy(
        "Prepare safe flatten",
        "Prepare a fresh reduction plan without submitting an order.",
    ),
    "stop_bot_decisions": OperatorCopy(
        "Stop bot decisions",
        "Stop new decisions while existing exposure remains under Clerk custody.",
    ),
    "open_custody_timeline": OperatorCopy(
        "Open custody timeline",
        "Inspect the immutable operation-first evidence timeline.",
    ),
    "rebuild_from_mirror": OperatorCopy(
        "Rebuild from mirror",
        "Rebuild a failed authority only from a contiguous verified mirror.",
    ),
    "reset_authority": OperatorCopy(
        "Reset authority",
        "Create a new authority generation only after fresh flat and order-free proof.",
    ),
}

# The full set of codes the panel can emit — used by the snapshot + copy-coverage
# tests. ``STOPPED_OUTCOME`` is the disambiguated duty-outcome copy key for the
# ``STOPPED`` duty-outcome kind.
ALL_VOCABULARY_CODES: Final[frozenset[str]] = (
    PHASES
    | DESIRED_STATES
    | HOLD_REASONS
    | RECONCILIATION_VERDICTS
    | CHANNEL_STATES
    | frozenset(STATION_IDS)
    | STATION_STATES
    | frozenset(ACTION_IDS)
    | {"CRASHED", "EXITED_UNVERIFIED", "STOPPED_OUTCOME"}
)


def copy_for(code: str) -> OperatorCopy:
    """Return the server-authored copy for ``code`` or raise ``KeyError``.

    The panel projections call this to attach label/explanation to every code
    they emit; a missing key is a contract bug surfaced immediately, never a
    raw enum leaking to the UI.
    """
    return OPERATOR_COPY[code]


def duty_outcome_copy_key(kind: str) -> str:
    """Map a duty-outcome kind to its copy key (disambiguates ``STOPPED``)."""
    return "STOPPED_OUTCOME" if kind == "STOPPED" else kind

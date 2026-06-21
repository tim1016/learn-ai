"""Shared Resume / Pause / Stop guard resolver (PRD #616).

The canonical, pure-function resolver for the three Resume guards
required by ADR-0010 §3 and ADR-0011 §6.  Consumed by:

1. The capability projection (``operator_surface.actions.resume`` /
   ``actions.pause`` / ``actions.stop``).
2. The mutation endpoint
   (``POST /api/live-instances/{sid}/desired-state``), which re-runs
   the same resolution immediately before the durable write so a
   stale snapshot cannot drive a write past the same gate.
3. The CLI (``app.engine.live.run.cmd_resume``), which used to
   implement two of the three guards with a ``--force`` bypass.

The three artifact readers are pure folds over file inputs:

- ``read_broker_safety_verdict`` — the reactive ADR-0011 verdict
  (``BrokerSafetyVerdict.final_verdict``) captured on the
  ``verdict_snapshot.json`` written by the live engine each tick.
- ``read_reconciliation_receipt`` — the latest reconciliation receipt
  filed under the instance's run dir.  Returns ``NOT_AVAILABLE``
  until the writer is wired; that honest state is reflected in the
  reason code, not silently treated as "passes".
- ``read_uncertain_intent_state`` — folds ``intent_events.jsonl``
  for unresolved ``ACK_FAILED_UNCERTAIN`` events; a corrupt or
  unreadable WAL becomes ``state=UNKNOWN`` (fail-closed).

The composed ``ResumeGuardState`` is intentionally a value object;
tests exercise the resolver against artifact-state combinations, not
by reading the three artifact files directly.

The closed reason-code vocabulary is the only set of disabled
reasons returned by the server; the Frontend's typed lookup is
exhaustive and unknown codes fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

# ---------------------------------------------------------------------------
# Closed reason-code vocabulary — the only set of disabled reasons the server
# returns for ``actions.resume`` / ``actions.pause`` / ``actions.stop`` and
# the ``flatten_and_pause`` / ``mark_poisoned`` legacy codes the evaluator
# already emitted.  Drop ``SAFETY_BLOCK_HALT`` and ``RECONCILE_NOT_WIRED``
# in favour of structured ADR-0011-aligned codes.
# ---------------------------------------------------------------------------

RESUME_REASON_CODES: frozenset[str] = frozenset(
    {
        # Broker safety verdict gate (identity, ADR-0011 amendment)
        "BROKER_SAFETY_UNSAFE",
        "BROKER_SAFETY_UNKNOWN",
        # Submission-capability gate (ADR-0011 amendment, PRD #619-A).
        # Identity is paper-only, but the declared submit mode and the
        # actual readonly setting do not satisfy the run's contract.
        "SUBMISSION_CAPABILITY_BLOCKED",
        "SUBMISSION_CAPABILITY_UNKNOWN",
        # Reconciliation receipt gate
        "RECONCILIATION_FAILED",
        "RECONCILIATION_STALE",
        "RECONCILIATION_NOT_AVAILABLE",
        "RECONCILIATION_UNKNOWN",
        # Uncertain-intent gate
        "UNRESOLVED_UNCERTAIN_INTENT",
        "UNCERTAIN_INTENT_STATE_UNKNOWN",
        # Intent-state pair rules
        "ALREADY_RUNNING",
        "ALREADY_PAUSED",
        "STOPPED_REQUIRES_REDEPLOY",
        "REDEPLOY_REQUIRED",
    }
)

# ---------------------------------------------------------------------------
# Artifact-state value objects (pure folds — tests exercise as black boxes).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerSafetyArtifact:
    """Resolution of the broker safety gate.

    ``state``:
      - ``SAFE``  -> verdict is ``paper-only`` (Resume permitted by this
        guard).
      - ``UNSAFE`` -> verdict positively indicates live / non-paper risk.
      - ``UNKNOWN`` -> no signal (snapshot absent, unreadable, or empty)
        OR verdict is ``unknown``.  Fail-closed.
    ``verdict``: the raw verdict string for diagnostics (or ``None``).
    """

    state: Literal["SAFE", "UNSAFE", "UNKNOWN"]
    verdict: str | None = None


@dataclass(frozen=True)
class ReconciliationArtifact:
    """Resolution of the reconciliation receipt gate.

    ``state``:
      - ``PASSED`` -> receipt exists, postdates the relevant run/broker
        state, and indicates clean reconciliation.
      - ``FAILED`` -> receipt exists and reports a divergence.
      - ``STALE`` -> receipt exists but predates the relevant
        run/broker state.
      - ``NOT_AVAILABLE`` -> reconciliation writer is not yet wired
        (today's honest state).
      - ``UNKNOWN`` -> receipt unreadable / malformed.  Fail-closed.
    """

    state: Literal["PASSED", "FAILED", "STALE", "NOT_AVAILABLE", "UNKNOWN"]
    receipt_path: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SubmissionCapabilityArtifact:
    """Resolution of the ADR-0011 amendment submission-capability gate.

    PRD #619-A: identity (``BrokerSafetyArtifact``) and submission
    capability are independent facts. The capability fact is derived
    from durable child/run evidence — the declared ``submit_mode`` on
    the spec/ledger and the actual ``readonly`` setting used to
    construct the child (today carried on ``run_status.json``).

    ``state``:
      - ``SATISFIED`` -> declared submit_mode + actual readonly together
        satisfy the run's contract (``live_paper`` ⇒ ``readonly=False``;
        ``shadow`` ⇒ no order submission required so any readonly is
        ok).
      - ``BLOCKED`` -> declared and actual diverge in a way that would
        positively refuse order submission (e.g. ``submit_mode=live_paper``
        but ``readonly_at_start=True``).
      - ``UNKNOWN`` -> either the declared submit_mode or the actual
        readonly cannot be proven from durable child/run evidence
        (run_status.json absent, missing fields, malformed). Fail-closed.
    """

    state: Literal["SATISFIED", "BLOCKED", "UNKNOWN"]
    declared_submit_mode: Literal["live_paper", "shadow"] | None = None
    readonly_at_start: bool | None = None
    detail: str | None = None


@dataclass(frozen=True)
class UncertainIntentArtifact:
    """Resolution of the uncertain-intent gate.

    ``state``:
      - ``CLEAR`` -> no unresolved ``ACK_FAILED_UNCERTAIN`` events.
      - ``PRESENT`` -> one or more unresolved uncertain intents in
        the WAL (carry the list for diagnostics).
      - ``UNKNOWN`` -> WAL corrupt or unreadable.  Fail-closed.
    """

    state: Literal["CLEAR", "PRESENT", "UNKNOWN"]
    unresolved_intent_ids: tuple[str, ...] = ()


class ResumeGuardState(BaseModel):
    """Single composed value consumed by every Resume entry point.

    The resolver runs once per request; this object is passed to:

    - ``evaluate_action`` for the capability projection.
    - The desired-state mutation endpoint (re-validation).
    - The CLI ``cmd_resume`` (replacement for ad-hoc artifact scans).

    ``reason_codes`` carries the **full** list of applicable reason
    codes in priority order (highest first).  The single-line tooltip
    renders ``reason_codes[0]``; the structured response carries all.

    PRD #619-A §A6: ``allow_resume`` is a derived ``@computed_field``
    over ``reason_codes`` — there is one source of truth, and a future
    writer that forgets to recompute the boolean cannot drift from the
    list.
    """

    broker_safety: BrokerSafetyArtifact = Field()
    submission_capability: SubmissionCapabilityArtifact = Field()
    reconciliation: ReconciliationArtifact = Field()
    uncertain_intent: UncertainIntentArtifact = Field()
    reason_codes: list[str]

    model_config = {"arbitrary_types_allowed": True}

    # PRD #619-A §A6 — collapse ``allow_resume`` to a derived property so
    # there is exactly one source of truth: the empty/non-empty
    # ``reason_codes`` list. The previous stored field could drift from
    # the list if a future writer forgot to recompute it.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def allow_resume(self) -> bool:
        return not self.reason_codes


# ---------------------------------------------------------------------------
# Priority order for the tooltip (PRD #616).  Highest priority first; the
# structured response carries the full list, the tooltip renders the head.
# Intent-state-pair codes (``STOPPED_REQUIRES_REDEPLOY``, ``ALREADY_RUNNING``,
# ``ALREADY_PAUSED``, ``REDEPLOY_REQUIRED``) are layered above the artifact
# guards in the action evaluator because they short-circuit before the
# artifact guards run.
# ---------------------------------------------------------------------------

_REASON_PRIORITY: tuple[str, ...] = (
    "STOPPED_REQUIRES_REDEPLOY",
    "BROKER_SAFETY_UNSAFE",
    "BROKER_SAFETY_UNKNOWN",
    # Capability codes sit just below identity — a paper-verified
    # identity that lacks order capability is more important to
    # surface than a stale reconciliation receipt.
    "SUBMISSION_CAPABILITY_BLOCKED",
    "SUBMISSION_CAPABILITY_UNKNOWN",
    "UNRESOLVED_UNCERTAIN_INTENT",
    "UNCERTAIN_INTENT_STATE_UNKNOWN",
    "RECONCILIATION_FAILED",
    "RECONCILIATION_STALE",
    "RECONCILIATION_NOT_AVAILABLE",
    "RECONCILIATION_UNKNOWN",
    "REDEPLOY_REQUIRED",
    "ALREADY_RUNNING",
    "ALREADY_PAUSED",
)


def sort_reason_codes(codes: list[str]) -> list[str]:
    """Sort codes by the documented priority order; unknown codes go last."""
    priority_index = {code: i for i, code in enumerate(_REASON_PRIORITY)}
    return sorted(codes, key=lambda c: priority_index.get(c, len(_REASON_PRIORITY)))


# ---------------------------------------------------------------------------
# Artifact readers — pure folds.  Each takes a single file path; the
# resolver owns artifact selection and freshness.  Tests stub the file or
# call the reader directly.
# ---------------------------------------------------------------------------


def read_broker_safety_verdict(verdict_snapshot_path: Path) -> BrokerSafetyArtifact:
    """Read the per-run verdict_snapshot.json filed by the live engine.

    Per ADR-0011, the snapshot carries ``{"verdict": "paper-only" |
    "unsafe" | "unknown", ...}``.  A missing snapshot is ``UNKNOWN``
    (fail-closed); a corrupted snapshot is ``UNKNOWN``; only an
    explicit ``paper-only`` is SAFE.
    """
    if not verdict_snapshot_path.exists():
        return BrokerSafetyArtifact(state="UNKNOWN", verdict=None)
    try:
        import json as _json

        data = _json.loads(verdict_snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return BrokerSafetyArtifact(state="UNKNOWN", verdict=None)
    verdict_value = data.get("verdict") if isinstance(data, dict) else None
    if verdict_value == "paper-only":
        return BrokerSafetyArtifact(state="SAFE", verdict="paper-only")
    if verdict_value == "unsafe":
        return BrokerSafetyArtifact(state="UNSAFE", verdict="unsafe")
    if verdict_value == "unknown":
        return BrokerSafetyArtifact(state="UNKNOWN", verdict="unknown")
    # Any other (malformed) string is UNKNOWN.
    if isinstance(verdict_value, str):
        return BrokerSafetyArtifact(state="UNKNOWN", verdict=verdict_value)
    return BrokerSafetyArtifact(state="UNKNOWN", verdict=None)


def read_reconciliation_receipt(
    run_dir: Path | None,
    *,
    relevant_after_ms: int | None = None,
) -> ReconciliationArtifact:
    """Read the latest reconciliation receipt for the run.

    PRD #616 ships the *reader* only.  Receipt-writer wiring is
    downstream; until it lands, the honest state is ``NOT_AVAILABLE``.
    The cockpit surfaces this honestly via the
    ``RECONCILIATION_NOT_AVAILABLE`` reason code when a guard treats
    it as blocking; it is otherwise informational.

    ``relevant_after_ms`` validates the receipt postdates the run /
    broker state of interest.  When the file's last_reconcile_ms <
    relevant_after_ms the receipt is ``STALE``.
    """
    if run_dir is None:
        return ReconciliationArtifact(state="NOT_AVAILABLE", receipt_path=None)
    receipt_path = run_dir / "reconciliation_receipt.json"
    if not receipt_path.exists():
        return ReconciliationArtifact(state="NOT_AVAILABLE", receipt_path=None)
    try:
        import json as _json

        data = _json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ReconciliationArtifact(
            state="UNKNOWN",
            receipt_path=str(receipt_path),
            detail="receipt unreadable",
        )
    if not isinstance(data, dict):
        return ReconciliationArtifact(
            state="UNKNOWN",
            receipt_path=str(receipt_path),
            detail="receipt malformed",
        )
    last_ms = data.get("last_reconcile_ms")
    if relevant_after_ms is not None and isinstance(last_ms, int) and last_ms < relevant_after_ms:
        return ReconciliationArtifact(
            state="STALE",
            receipt_path=str(receipt_path),
            detail=f"receipt {last_ms} predates required {relevant_after_ms}",
        )
    status = data.get("status")
    if status == "passed":
        return ReconciliationArtifact(state="PASSED", receipt_path=str(receipt_path))
    if status == "failed":
        return ReconciliationArtifact(
            state="FAILED",
            receipt_path=str(receipt_path),
            detail=str(data.get("detail") or ""),
        )
    return ReconciliationArtifact(
        state="UNKNOWN",
        receipt_path=str(receipt_path),
        detail=f"unknown status {status!r}",
    )


def read_submission_capability(
    run_status_path: Path | None,
) -> SubmissionCapabilityArtifact:
    """Read the durable child/run capability evidence from ``run_status.json``.

    PRD #619-A: the live engine's ``cmd_start`` writes ``run_status.json``
    at boot carrying ``submit_mode_at_start`` (declared on the spec) and
    ``readonly_at_start`` (the actual setting used to construct the
    child). Both are immutable startup facts, persisted through every
    subsequent lifecycle write via ``model_copy(update=...)`` so the
    Resume gate has a stable source even after the engine exits.

    A missing file, missing fields, or a sidecar that predates this PR
    is ``UNKNOWN`` (fail-closed) — the cockpit surfaces it as
    ``SUBMISSION_CAPABILITY_UNKNOWN``. Resume blocks until 619-B's
    ``engine_runtime.json`` arrives or the run is restarted under the
    new sidecar contract.

    Capability semantics (ADR-0011 amendment):

    - ``submit_mode=live_paper`` + ``readonly=False`` -> SATISFIED
      (the child is allowed to submit paper orders).
    - ``submit_mode=live_paper`` + ``readonly=True`` -> BLOCKED
      (the lower-altitude four-layer would refuse; surface the gate
      so the operator sees why Resume is disabled).
    - ``submit_mode=shadow`` + any readonly -> SATISFIED (shadow runs
      do not submit; readonly is moot for the contract).
    """
    import json as _json

    if run_status_path is None or not run_status_path.exists():
        return SubmissionCapabilityArtifact(state="UNKNOWN", detail="run_status.json absent")
    try:
        data = _json.loads(run_status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SubmissionCapabilityArtifact(state="UNKNOWN", detail="run_status.json unreadable")
    if not isinstance(data, dict):
        return SubmissionCapabilityArtifact(state="UNKNOWN", detail="run_status.json malformed")

    declared = data.get("submit_mode_at_start")
    readonly = data.get("readonly_at_start")
    if declared not in ("live_paper", "shadow"):
        return SubmissionCapabilityArtifact(
            state="UNKNOWN", detail="submit_mode_at_start missing or invalid"
        )
    if not isinstance(readonly, bool):
        return SubmissionCapabilityArtifact(
            state="UNKNOWN",
            declared_submit_mode=declared,
            detail="readonly_at_start missing or invalid",
        )
    if declared == "live_paper" and readonly:
        return SubmissionCapabilityArtifact(
            state="BLOCKED",
            declared_submit_mode=declared,
            readonly_at_start=readonly,
            detail="declared submit_mode=live_paper but child constructed with readonly=True",
        )
    return SubmissionCapabilityArtifact(
        state="SATISFIED",
        declared_submit_mode=declared,
        readonly_at_start=readonly,
    )


def read_uncertain_intent_state(wal_path: Path | None) -> UncertainIntentArtifact:
    """Pure fold over ``intent_events.jsonl`` for unresolved uncertains.

    Mirrors the legacy ``_scan_wal_for_unresolved_uncertains`` (see
    ``app/engine/live/run.py``) but as a shared resolver input.  A
    corrupt WAL is ``UNKNOWN`` (fail-closed); missing WAL is ``CLEAR``.
    """
    if wal_path is None or not wal_path.exists():
        return UncertainIntentArtifact(state="CLEAR")
    try:
        from app.engine.live.intent_events import IntentEventType
        from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
    except ImportError:
        return UncertainIntentArtifact(state="UNKNOWN")
    try:
        events = IntentWal(wal_path).read_tail()
    except IntentWalCorruptError:
        return UncertainIntentArtifact(state="UNKNOWN")
    pending_uncertain: set[str] = set()
    resolution_types = {
        IntentEventType.SUBMITTED.value,
        IntentEventType.SUBMITTED_RECOVERED.value,
        IntentEventType.INTENT_NOT_ACCEPTED.value,
        IntentEventType.SUBMIT_UNCERTAIN_HALTED.value,
    }
    for event in events:
        et = event.event_type.value
        if et == IntentEventType.ACK_FAILED_UNCERTAIN.value:
            pending_uncertain.add(event.intent_id)
        elif et in resolution_types:
            pending_uncertain.discard(event.intent_id)
    if not pending_uncertain:
        return UncertainIntentArtifact(state="CLEAR")
    return UncertainIntentArtifact(state="PRESENT", unresolved_intent_ids=tuple(sorted(pending_uncertain)))


# ---------------------------------------------------------------------------
# Composed resolver — accepts pre-resolved artifacts (tests) or the artifact
# paths (production callers).
# ---------------------------------------------------------------------------


def resolve_guard_state(
    *,
    broker_safety: BrokerSafetyArtifact,
    submission_capability: SubmissionCapabilityArtifact,
    reconciliation: ReconciliationArtifact,
    uncertain_intent: UncertainIntentArtifact,
) -> ResumeGuardState:
    """Compose the four artifact resolutions into a single state.

    Pure: same inputs always produce the same output.  Tests exercise
    every artifact-state combination through this function.

    PRD #619-A: the composition is

    ::

        can_resume =
            broker_identity is PAPER_VERIFIED
            AND submission_capability_satisfies(run_submit_mode)
            AND reconciliation in {PASSED, NOT_AVAILABLE}
            AND uncertain_intent is CLEAR

    Per-gate availability is gate-specific (PRD authority principle #3):
    ``reconciliation=NOT_AVAILABLE`` is non-blocking (the writer is not
    yet wired), but ``submission_capability=UNKNOWN`` and
    ``uncertain_intent=UNKNOWN`` are both blocking. There is no
    universal "NOT_AVAILABLE never blocks" rule.
    """
    reason_codes: list[str] = []

    if broker_safety.state == "UNSAFE":
        reason_codes.append("BROKER_SAFETY_UNSAFE")
    elif broker_safety.state == "UNKNOWN":
        reason_codes.append("BROKER_SAFETY_UNKNOWN")

    if submission_capability.state == "BLOCKED":
        reason_codes.append("SUBMISSION_CAPABILITY_BLOCKED")
    elif submission_capability.state == "UNKNOWN":
        reason_codes.append("SUBMISSION_CAPABILITY_UNKNOWN")

    if uncertain_intent.state == "PRESENT":
        reason_codes.append("UNRESOLVED_UNCERTAIN_INTENT")
    elif uncertain_intent.state == "UNKNOWN":
        reason_codes.append("UNCERTAIN_INTENT_STATE_UNKNOWN")

    if reconciliation.state == "FAILED":
        reason_codes.append("RECONCILIATION_FAILED")
    elif reconciliation.state == "STALE":
        reason_codes.append("RECONCILIATION_STALE")
    elif reconciliation.state == "UNKNOWN":
        reason_codes.append("RECONCILIATION_UNKNOWN")
    # NOT_AVAILABLE is the honest "writer not wired yet" state — surfaced
    # informationally but not appended as a blocker (gate-specific
    # availability policy per PRD #619-A authority principle #3).

    sorted_codes = sort_reason_codes(reason_codes)

    return ResumeGuardState(
        broker_safety=broker_safety,
        submission_capability=submission_capability,
        reconciliation=reconciliation,
        uncertain_intent=uncertain_intent,
        reason_codes=sorted_codes,
    )


def resolve_guard_state_from_paths(
    *,
    verdict_snapshot_path: Path | None,
    run_status_path: Path | None,
    run_dir_for_reconciliation: Path | None,
    intent_wal_path: Path | None,
) -> ResumeGuardState:
    """Production helper: read the four artifacts from disk + compose.

    Used by the desired-state mutation endpoint and the CLI.  The
    capability projection composes the same way against the status
    pipeline's already-fetched paths.
    """
    if verdict_snapshot_path is not None:
        broker_safety = read_broker_safety_verdict(verdict_snapshot_path)
    else:
        broker_safety = BrokerSafetyArtifact(state="UNKNOWN")
    submission_capability = read_submission_capability(run_status_path)
    reconciliation = read_reconciliation_receipt(run_dir_for_reconciliation)
    uncertain_intent = read_uncertain_intent_state(intent_wal_path)
    return resolve_guard_state(
        broker_safety=broker_safety,
        submission_capability=submission_capability,
        reconciliation=reconciliation,
        uncertain_intent=uncertain_intent,
    )


def empty_guard_state() -> ResumeGuardState:
    """Default state when no artifacts exist (nothing-ever-deployed).

    Both Resume and Pause are permitted; there is no run to safeguard.
    Used by the capability projection when there is no live binding
    and no latest run to read artifacts from.
    """
    return ResumeGuardState(
        broker_safety=BrokerSafetyArtifact(state="SAFE"),
        submission_capability=SubmissionCapabilityArtifact(state="SATISFIED"),
        reconciliation=ReconciliationArtifact(state="NOT_AVAILABLE"),
        uncertain_intent=UncertainIntentArtifact(state="CLEAR"),
        reason_codes=[],
    )

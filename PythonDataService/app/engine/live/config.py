"""Live-runtime configuration.

Phase 1 intentionally keeps this module small. Broker-mode safety stays in
``app.broker.ibkr.config`` and order safety stays in ``app.broker.ibkr.orders``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path

from app.engine.execution.order_sizer import SizingPolicy
from app.engine.live.order_identity import DEFAULT_ORDER_REF_MAX_LENGTH
from app.schemas.broker_capability import SessionKind

# Single source of truth for the operator-supplied ``live_config`` dict keys.
# The deploy boundary (``HostRunnerDeployRequest._validate_sizing``) rejects
# unknown siblings; ``_live_config_from_ledger`` rejects them when reading a
# legacy ledger. Adding a field here is a deliberate two-sided change.
LIVE_CONFIG_LEDGER_KEYS: frozenset[str] = frozenset(
    {
        "symbol",
        "force_flat_at",
        "consolidator_period_min",
        "run_dir",
        "max_submit_latency_ms",
        "sizing",
        "allowed_sessions",
        # PRD #593 Slice 1A — operator-declared instrument plan.
        # Hashed into ``run_id`` like every other key here. The current
        # deployment-validation live path consumes exactly one long stock leg;
        # unsupported shapes remain declarative until their resolver ships.
        "action",
        # ADR 0014 §6 — per-instance lag thresholds for the broker-activity
        # reconciliation verdict ladder. Optional block; absence ⇒ engine
        # uses ``ReconciliationTimingPolicy`` defaults. Hashed into
        # ``run_id`` so a threshold change forces a redeploy (cross-run
        # comparability of reconciliation verdicts is preserved).
        "reconciliation_timing_policy",
    }
)

DEFAULT_ALLOWED_SESSIONS: tuple[SessionKind, ...] = ("RTH",)
_SESSION_ORDER: tuple[SessionKind, ...] = ("PRE", "RTH", "POST", "OVERNIGHT")


@dataclass(frozen=True)
class LiveConfig:
    """Engine-level knobs for paper runtime tests and later CLI wiring."""

    symbol: str = "SPY"
    # Wall-clock cutoff (interpreted in the same timezone as the bar's
    # ``time`` field) at which the live engine cancels open orders and
    # market-flats every position. Set to ``None`` to disable; the
    # default 15:55 ET targets the standard NYSE close at 16:00. Mirrors
    # ``ExecutionConfig.force_flat_at`` from the backtest engine so the
    # two driver paths can be aligned by passing ``None`` on both sides.
    force_flat_at: time | None = time(15, 55)
    consolidator_period_min: int = 15
    run_dir: Path = Path("live_runs")
    max_submit_latency_ms: int = 500

    # ADR 0009 — live position-sizing policy. ``None`` ⇒ legacy/unknown
    # (pre-policy ``SimpleFloorSizing`` all-in); a sizing-aware deploy ALWAYS
    # writes an explicit policy (the canonical safe default is FixedShares(1)),
    # so a fresh empty-``live_config`` run never hash-collides with the safe
    # canary. Hashed into ``run_id`` through ``live_config`` like every other
    # field on this dataclass.
    sizing: SizingPolicy | None = None
    # PRD #1005 Slice 2 — strategy-declared sessions in which this live
    # instance is allowed to submit. The default is regular trading hours
    # only. The submit path additionally intersects this with the order
    # mechanism's supported sessions, so declaring PRE/POST/OVERNIGHT cannot
    # activate extended-hours placement until that mechanism ships.
    allowed_sessions: tuple[SessionKind, ...] = DEFAULT_ALLOWED_SESSIONS

    # ── Durable submit protocol (ADR-0008 / PRD #446) ──────────────────────────
    # Master switch. Stays False until BOTH Acceptance-Gate receipts exist;
    # ``broker_ownership_query.require_durable_submit_activation`` refuses
    # activation otherwise, so flipping this alone cannot turn the protocol on.
    durable_submit_enabled: bool = False
    # Conservative cap used to bound ``build_order_ref`` and the
    # ``strategy_instance_id`` length rule for the deterministic core. TODO(#446
    # Gate #1): the REAL cap is ``durable_submit_verified_order_ref_cap``, set
    # only from a live paper receipt; this fallback just gives the pure logic a
    # bound to enforce. Truncation is silent and catastrophic.
    durable_submit_order_ref_max_length: int = DEFAULT_ORDER_REF_MAX_LENGTH
    # The orderRef cap PROVEN by a live paper order (Gate #1). ``None`` =
    # unverified => activation refused (ADR-0008 §1: "C is intentionally unset
    # until the paper-receipt gate verifies the actual echoed cap").
    durable_submit_verified_order_ref_cap: int | None = None

    # ADR 0014 §6 — per-instance reconciliation lag thresholds. Stored as
    # a dict (not a typed model) so this dataclass stays a plain LiveConfig
    # and the publisher constructs ``ReconciliationTimingPolicy`` on demand.
    # ``None`` ⇒ publisher uses the policy's built-in defaults
    # (``caveat_lag_ms=2000``, ``excessive_lag_ms=10000``).
    reconciliation_timing_policy: dict | None = None


def normalize_allowed_sessions(value: object | None) -> tuple[SessionKind, ...]:
    """Normalize the live_config.allowed_sessions allow-list.

    The ledger stores the canonical order so semantically identical lists hash
    identically. Strings are rejected because ``"RTH"`` is too easy to confuse
    with an iterable of characters.
    """
    if value is None:
        return DEFAULT_ALLOWED_SESSIONS
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError("allowed_sessions must be an array of session names")
    raw_values = [str(item).strip().upper() for item in value]
    if not raw_values:
        raise ValueError("allowed_sessions must contain at least one session")
    invalid = sorted(set(raw_values) - set(_SESSION_ORDER))
    if invalid:
        raise ValueError(f"allowed_sessions contains unsupported sessions: {invalid}")
    normalized = tuple(kind for kind in _SESSION_ORDER if kind in raw_values)
    if not normalized:
        raise ValueError("allowed_sessions must contain at least one supported session")
    return normalized


def stock_symbol_from_action_plan(action: object) -> str | None:
    """Return the single stock underlying declared by a live action plan.

    Action plans are operator-authored deploy identity. For the current
    stock-only runtime path, exactly one long stock leg is the traded ticker.
    Option, short, and multi-leg plans are not consumable by the stock runtime
    yet, so they deliberately return ``None``.
    """
    if not isinstance(action, dict):
        return None
    on_enter = action.get("on_enter")
    if not isinstance(on_enter, list) or not on_enter:
        return None

    symbols: set[str] = set()
    for leg in on_enter:
        if not isinstance(leg, dict):
            return None
        if leg.get("position") != "long":
            return None
        instrument = leg.get("instrument")
        if not isinstance(instrument, dict):
            return None
        if instrument.get("kind") != "stock":
            return None
        underlying = instrument.get("underlying")
        if not isinstance(underlying, str) or not underlying.strip():
            return None
        symbols.add(underlying.strip().upper())

    if len(symbols) != 1 or len(on_enter) != 1:
        return None
    return next(iter(symbols))

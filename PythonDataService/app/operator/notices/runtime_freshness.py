from __future__ import annotations

from dataclasses import dataclass

from app.operator.notices.schema import (
    OperatorNotice,
    OperatorNoticeAction,
    OperatorNoticeActionability,
    OperatorNoticeCode,
    OperatorNoticeTier,
    RuntimeFreshnessReasonCode,
)
from app.services.runtime_freshness import RuntimeFreshness


@dataclass(frozen=True)
class _Rule:
    priority: int
    source_codes: frozenset[RuntimeFreshnessReasonCode]
    notice_code: OperatorNoticeCode
    tier: OperatorNoticeTier
    title: str
    message: str
    actionability: OperatorNoticeActionability
    resolution: str
    action: OperatorNoticeAction
    runbook_slug: str | None = None
    suppress_banner: bool = False


_RUNBOOK = "runtime-freshness"


# Rules declared in priority-descending order and pre-sorted at module load.
# A rule fires when ALL of its source_codes are present in the active set
# and not yet consumed by a higher-priority rule (all-of / subset semantics).
_RUNTIME_FRESHNESS_RULES: tuple[_Rule, ...] = tuple(sorted([
    _Rule(
        priority=100,
        source_codes=frozenset({"CONTROL_PLANE_BOOT_ID_MISMATCH"}),
        notice_code="runtime.control_plane_boot_id_mismatch",
        tier="critical",
        title="Cockpit and engine disagree on boot identity",
        message=(
            "The engine reports a different boot ID than the cockpit. A restart "
            "happened that the cockpit did not initiate. Stop trusting cockpit "
            "state, verify positions at IBKR, and redeploy."
        ),
        actionability="routed",
        resolution="Clears after the operator verifies positions at IBKR and redeploys the bot from a trusted runtime.",
        action=OperatorNoticeAction(kind="open_runbook", label="How to recover", target=_RUNBOOK),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=95,
        source_codes=frozenset({"CONTROL_PLANE_LEASE_STALE"}),
        notice_code="runtime.control_plane_lease_stale",
        tier="critical",
        title="Control-plane lease is stale",
        message=(
            "Another control-plane lease holder hasn't checked in. The bot is "
            "in a guarded state. Verify only one cockpit or host runner is "
            "attached to this run."
        ),
        actionability="actuatable",
        resolution="Clears when the cockpit renews the control-plane lease and the engine reports the same lease holder.",
        action=OperatorNoticeAction(
            kind="renew_control_plane_lease",
            label="Renew control-plane lease",
            target="daemon_lease",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=90,
        source_codes=frozenset({"COMMAND_LOOP_STALE"}),
        notice_code="runtime.command_loop_unresponsive",
        tier="critical",
        title="Bot is not responding to commands",
        message=(
            "Pause, Resume, Stop, or Flatten may not take effect until the bot "
            "recovers. If this persists, stop the bot from the host runner and "
            "verify positions at IBKR."
        ),
        actionability="routed",
        resolution="Clears when the command loop heartbeat is fresh again or after the operator verifies positions and restarts the bot.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check positions in IBKR",
            target="ibkr_positions",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=85,
        source_codes=frozenset({"ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE"}),
        notice_code="runtime.engine_runtime_incompatible",
        tier="critical",
        title="Engine runtime is incompatible",
        message=(
            "The engine runtime version is incompatible with the cockpit. The "
            "bot will not start trading. Redeploy with a matching runtime."
        ),
        actionability="actuatable",
        resolution="Clears when a redeployed bot reports a compatible engine runtime version.",
        action=OperatorNoticeAction(kind="redeploy", label="Redeploy bot", target="configuration_tab"),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=84,
        source_codes=frozenset({"ENGINE_RUNTIME_MISSING"}),
        notice_code="runtime.engine_runtime_incompatible",
        tier="critical",
        title="Engine runtime is incompatible",
        message=(
            "The engine runtime version is incompatible with the cockpit. The "
            "bot will not start trading. Redeploy with a matching runtime."
        ),
        actionability="actuatable",
        resolution="Clears when a redeployed bot reports a compatible engine runtime version.",
        action=OperatorNoticeAction(kind="redeploy", label="Redeploy bot", target="configuration_tab"),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=80,
        source_codes=frozenset({"BROKER_PROBE_MISSING"}),
        notice_code="runtime.broker_probe_missing",
        tier="warning",
        title="Broker probe is missing",
        message=(
            "The broker probe has not run since the bot started. Cockpit sees "
            "no broker telemetry. Check that the broker daemon is connected."
        ),
        actionability="routed",
        resolution="Clears when broker probe telemetry is present and fresh in the child runtime.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check broker daemon",
            target="broker_daemon",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=75,
        source_codes=frozenset({"BROKER_PROBE_STALE"}),
        notice_code="runtime.broker_probe_stale",
        tier="warning",
        title="Broker probe is stale",
        message=(
            "The broker probe has not returned a fresh status within the "
            "freshness window. The bot is protecting itself."
        ),
        actionability="self_resolving",
        resolution="Clears automatically when the child runtime reports a fresh broker probe.",
        action=OperatorNoticeAction(kind="none"),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=70,
        source_codes=frozenset({"BAR_LOOP_HEARTBEAT_STALE", "BAR_LOOP_LATEST_BAR_STALE"}),
        notice_code="runtime.market_data_feed_stalled",
        tier="warning",
        title="Market data feed is stalled",
        message=(
            "Both the heartbeat and the most recent bar are stale. New trading "
            "decisions are held until fresh data arrives."
        ),
        actionability="routed",
        resolution="Clears when IBKR market data heartbeat and latest-bar evidence are fresh again.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check IBKR connection",
            target="ibkr_connection",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=68,
        source_codes=frozenset({"BAR_LOOP_FIRST_BAR_TIMEOUT"}),
        notice_code="runtime.market_data_first_bar_timeout",
        tier="critical",
        title="IBKR market data is silent",
        message=(
            "IBKR accepted the live bar subscription, but no first bar arrived "
            "before the startup deadline. This often means a competing live "
            "session or paper market-data entitlement issue is starving the "
            "paper API feed. New trading decisions are held until bars arrive."
        ),
        actionability="routed",
        resolution="Clears when the first IBKR live bar arrives or after the operator fixes the IBKR market-data session.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Fix IBKR market data",
            target="ibkr_connection",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=65,
        source_codes=frozenset({"BAR_LOOP_SOURCE_MISSING"}),
        notice_code="runtime.market_data_stale",
        tier="warning",
        title="Market data has not started",
        message=(
            "The bot subscribed to the market-data feed, but no source bar has "
            "arrived yet. New trading decisions are held until IBKR sends bars."
        ),
        actionability="routed",
        resolution="Clears when IBKR sends source bars for this subscription.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check IBKR market data",
            target="ibkr_connection",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=60,
        source_codes=frozenset({"BAR_LOOP_LATEST_BAR_STALE"}),
        notice_code="runtime.market_data_stale",
        tier="warning",
        title="Market data is stale",
        message=(
            "The most recent bar is older than the freshness window. New "
            "trading decisions are held until fresh data arrives."
        ),
        actionability="routed",
        resolution="Clears when a fresh IBKR bar arrives inside the freshness window.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check IBKR market data",
            target="ibkr_connection",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=50,
        source_codes=frozenset({"BAR_LOOP_HEARTBEAT_STALE"}),
        notice_code="runtime.market_data_stale",
        tier="warning",
        title="Market data heartbeat is stale",
        message=(
            "The data feed heartbeat is older than the freshness window. New "
            "trading decisions are held until fresh data arrives."
        ),
        actionability="routed",
        resolution="Clears when the child runtime reports a fresh market-data heartbeat.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check IBKR market data",
            target="ibkr_connection",
        ),
        runbook_slug=_RUNBOOK,
    ),
    _Rule(
        priority=20,
        source_codes=frozenset({"BAR_LOOP_SESSION_HALTED"}),
        notice_code="runtime.market_session_halted",
        tier="info",
        title="Trading session is halted",
        message=(
            "The exchange has halted the session for this symbol. The bot will "
            "resume when the halt clears."
        ),
        actionability="self_resolving",
        resolution="Clears automatically when the exchange halt clears for this symbol.",
        action=OperatorNoticeAction(kind="none"),
    ),
    _Rule(
        priority=10,
        source_codes=frozenset({"BAR_LOOP_SESSION_CLOSED"}),
        notice_code="runtime.market_closed",
        tier="info",
        title="Market closed",
        message=(
            "The bot is idle until the regular trading session opens. No "
            "trading decision is being made."
        ),
        actionability="self_resolving",
        resolution="Clears automatically when the regular trading session opens.",
        action=OperatorNoticeAction(kind="none"),
        suppress_banner=True,
    ),
], key=lambda r: -r.priority))


def _collect_codes(freshness: RuntimeFreshness) -> tuple[set[str], dict[str, int | None]]:
    """Return the union of active reason codes plus per-domain ages."""
    codes: set[str] = set()
    ages: dict[str, int | None] = {}
    for domain_name, domain in (
        ("command_loop", freshness.command_loop),
        ("broker", freshness.broker),
        ("bar_loop", freshness.bar_loop),
        ("control_plane", freshness.control_plane),
    ):
        for code in domain.stale_reason_codes:
            codes.add(code)
        ages[f"{domain_name}_age_ms"] = domain.age_ms
    return codes, ages


def _rule_matches(rule: _Rule, active: frozenset[str]) -> bool:
    return rule.source_codes <= active


def _build_notice(rule: _Rule, active_codes: set[str], facts: dict[str, int | None], now_ms: int | None) -> OperatorNotice:
    matched_sources = sorted(rule.source_codes & active_codes)
    return OperatorNotice(
        code=rule.notice_code,
        tier=rule.tier,
        title=rule.title,
        message=rule.message,
        source_codes=matched_sources,
        forensic_facts={k: v for k, v in facts.items() if v is not None},
        actionability=rule.actionability,
        resolution=rule.resolution,
        action=rule.action,
        runbook_slug=rule.runbook_slug,
        occurred_at_ms=now_ms,
    )


def compose_runtime_freshness_notices(
    freshness: RuntimeFreshness | None,
    *,
    now_ms: int | None = None,
) -> tuple[OperatorNotice | None, list[OperatorNotice]]:
    """Compose runtime-freshness notices for the operator surface.

    Returns ``(headline, additional_reasons)``.

    ``headline`` is the highest-priority non-suppressed notice, or ``None``
    when the only active rule is banner-suppressed (e.g. market closed).

    ``additional_reasons`` contains every matched notice *except* the exact
    headline object (identity-based, not code-based), so two rules that happen
    to emit the same code for different reasons both surface in
    ``additional_reasons``; only the one elevated to headline is excluded.
    """
    if freshness is None:
        return None, []

    active_codes, ages = _collect_codes(freshness)
    if not active_codes:
        return None, []

    active_frozen = frozenset(active_codes)

    matched: list[_Rule] = []
    consumed: set[str] = set()
    for rule in _RUNTIME_FRESHNESS_RULES:
        if not _rule_matches(rule, active_frozen):
            continue
        new_codes = rule.source_codes & active_frozen
        if new_codes <= consumed:
            continue
        matched.append(rule)
        consumed |= new_codes

    reasons = [_build_notice(rule, active_codes, ages, now_ms) for rule in matched]
    headline = next((n for rule, n in zip(matched, reasons, strict=False) if not rule.suppress_banner), None)
    # additional_reasons: every notice except the exact headline object (identity check).
    additional_reasons = [n for n in reasons if n is not headline]
    return headline, additional_reasons

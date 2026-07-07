"""Fleet/account-level contamination (ADR 0005, #399).

The fleet altitude is the *only* readiness signal authored by the backend: no
single engine can see sibling namespaces, so account-level residual is computed
here by aggregating every managed instance's namespace-attributed expected
position and differencing it against the net account snapshot:

    residual[symbol] = net_account_position[symbol] - Σ instance_expected[symbol]

A non-zero residual is a position no managed instance created — foreign / manual
/ unmanaged contamination. Pure logic, no I/O.
"""

from __future__ import annotations


def compute_fleet_contamination(
    net_positions: dict[str, int] | None,
    explained_by_instance: dict[str, dict[str, int]],
    *,
    policy_blocks_starts: bool = False,
) -> dict:
    """Difference the net account snapshot against the sum of instance expecteds.

    ``net_positions`` is ``None`` when the broker is unavailable -> the verdict
    is ``unknown`` (never guessed). ``policy_blocks_starts`` reflects the fleet
    policy gate: only when enabled *and* contaminated does it block starts.
    """
    explained_total: dict[str, int] = {}
    buckets: list[dict] = []
    for sid in sorted(explained_by_instance):
        positions = {s.upper(): int(q) for s, q in explained_by_instance[sid].items()}
        buckets.append({"strategy_instance_id": sid, "positions": positions})
        for symbol, qty in positions.items():
            explained_total[symbol] = explained_total.get(symbol, 0) + qty

    if net_positions is None:
        return {
            "net_positions": None,
            "explained_total": explained_total,
            "explained_by_instance": buckets,
            "residual": {},
            "verdict": "unknown",
            "policy_blocks_starts": False,
            "summary": "Net account position unavailable — contamination unknown.",
        }

    net = {s.upper(): int(q) for s, q in net_positions.items()}
    residual: dict[str, int] = {}
    for symbol in set(net) | set(explained_total):
        delta = net.get(symbol, 0) - explained_total.get(symbol, 0)
        if delta != 0:
            residual[symbol] = delta

    contaminated = bool(residual)
    verdict = "contaminated" if contaminated else "clean"
    if not contaminated:
        summary = "Account clean — every position is explained by a managed instance."
    else:
        positive = {symbol: qty for symbol, qty in residual.items() if qty > 0}
        negative = {symbol: qty for symbol, qty in residual.items() if qty < 0}
        if positive and not negative:
            summary = f"Unmanaged broker position(s): {_format_residual(positive)}."
        elif negative and not positive:
            summary = (
                "Managed bot artifacts overstate broker position(s): "
                f"{_format_residual(negative)}. Refresh reconciliation or retire stale runs."
            )
        else:
            summary = f"Broker and managed bot position evidence disagree: {_format_residual(residual)}."
    return {
        "net_positions": net,
        "explained_total": explained_total,
        "explained_by_instance": buckets,
        "residual": residual,
        "verdict": verdict,
        "policy_blocks_starts": policy_blocks_starts and contaminated,
        "summary": summary,
    }


def _format_residual(residual: dict[str, int]) -> str:
    return ", ".join(f"{sym} {qty:+d}" for sym, qty in sorted(residual.items()))


# ---------------------------------------------------------------------------
# PRD #616 — Fleet account-identity summary
# ---------------------------------------------------------------------------


def compute_account_identity(
    instance_account_ids: dict[str, str | None],
    broker_connected_account: str | None,
    *,
    broker_account_known: bool,
) -> dict:
    """Derive the ``FleetAccountSummary`` identity fields.

    Pure logic: ``instance_account_ids`` maps each managed
    ``strategy_instance_id`` to its ledger-recorded ``account_id``
    (``None`` when the ledger pre-dates the field).
    ``broker_connected_account`` is the live IBKR-connected account id
    (``None`` when the broker is unavailable / not yet wired);
    ``broker_account_known`` distinguishes "definitively unavailable"
    (we tried, got nothing) from "never queried" so the reason-code
    distinguishes ``BROKER_ACCOUNT_UNAVAILABLE`` from silence.

    Returns ``{account_id, account_identity, account_identity_reason_codes}``.

    Rules — the cheapest unambiguous one wins:

    - **No managed instances** → identity is ``UNKNOWN`` with no
      reason codes (an empty fleet is not an identity disagreement).
    - **Every instance's account_id is missing** → identity is
      ``UNKNOWN`` with ``ACCOUNT_ID_MISSING``.
    - **Mixed account ids across instances** → identity is
      ``CONFLICTING`` with ``INSTANCE_ACCOUNT_MISMATCH``; the canonical
      account_id is the most common one (deterministic by sort).
    - **All instances agree** → the agreed id is canonical.  If
      ``broker_connected_account`` is set and differs, identity is
      ``CONFLICTING`` with ``BROKER_ACCOUNT_MISMATCH``.  If
      ``broker_account_known`` is False, identity is still
      ``CONSISTENT`` (we don't know enough to disagree) but the
      ``BROKER_ACCOUNT_UNAVAILABLE`` reason is surfaced
      informationally.
    """
    declared: list[str] = []
    none_count = 0
    for sid in sorted(instance_account_ids):
        v = instance_account_ids[sid]
        if v is None or not v.strip():
            none_count += 1
        else:
            declared.append(v.strip())

    if not instance_account_ids:
        return {
            "account_id": broker_connected_account or None,
            "account_identity": "UNKNOWN",
            "account_identity_reason_codes": [],
        }

    if not declared:
        return {
            "account_id": broker_connected_account or None,
            "account_identity": "UNKNOWN",
            "account_identity_reason_codes": ["ACCOUNT_ID_MISSING"],
        }

    # Choose canonical: most common, tie-broken by lexical order.
    counts: dict[str, int] = {}
    for v in declared:
        counts[v] = counts.get(v, 0) + 1
    max_count = max(counts.values())
    candidates = sorted(k for k, c in counts.items() if c == max_count)
    canonical = candidates[0]

    reason_codes: list[str] = []
    if len(set(declared)) > 1:
        reason_codes.append("INSTANCE_ACCOUNT_MISMATCH")
    if none_count > 0:
        reason_codes.append("ACCOUNT_ID_MISSING")

    if reason_codes:
        identity = "CONFLICTING"
    else:
        identity = "CONSISTENT"

    if broker_account_known:
        if broker_connected_account and broker_connected_account.strip() != canonical:
            identity = "CONFLICTING"
            reason_codes.append("BROKER_ACCOUNT_MISMATCH")
    else:
        reason_codes.append("BROKER_ACCOUNT_UNAVAILABLE")

    return {
        "account_id": canonical,
        "account_identity": identity,
        "account_identity_reason_codes": reason_codes,
    }


def compute_fleet_account_summary(
    *,
    net_positions: dict[str, int] | None,
    explained_by_instance: dict[str, dict[str, int]],
    instance_account_ids: dict[str, str | None],
    broker_connected_account: str | None,
    broker_account_known: bool,
    policy_blocks_starts: bool = False,
) -> dict:
    """Compose the ``FleetAccountSummary`` shape.

    Wraps ``compute_fleet_contamination`` and ``compute_account_identity``
    so the router builds one DTO without duplicating the input wiring.
    """
    contamination = compute_fleet_contamination(
        net_positions,
        explained_by_instance,
        policy_blocks_starts=policy_blocks_starts,
    )
    identity = compute_account_identity(
        instance_account_ids,
        broker_connected_account,
        broker_account_known=broker_account_known,
    )
    return {
        "account_id": identity["account_id"],
        "account_identity": identity["account_identity"],
        "account_identity_reason_codes": identity["account_identity_reason_codes"],
        "contamination": contamination,
    }

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
        parts = ", ".join(f"{sym} {qty:+d}" for sym, qty in sorted(residual.items()))
        summary = f"Account residual: {parts} unattributed outside managed namespaces."
    return {
        "net_positions": net,
        "explained_total": explained_total,
        "explained_by_instance": buckets,
        "residual": residual,
        "verdict": verdict,
        "policy_blocks_starts": policy_blocks_starts and contaminated,
        "summary": summary,
    }

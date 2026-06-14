"""Morning-gate pre-flight halt checks for the live runner.

Per spec ``docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md``
sections 6.4 (next-session halt rules) and 9 (operational safety).

Distinct from § 7's intra-day fatal halt (which lives in ``halt.py``):
this module runs once at session start and either lets the run proceed
or refuses to place new orders today. A failure here does NOT contaminate
the run — the prior receipt is still valid; today is just skipped.

Each check returns a ``CheckResult``. The orchestrator (``run.py``)
collects them and refuses to start if any has ``passed=False``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

NTP_PACKET_SIZE = 48
NTP_EPOCH_OFFSET_SEC = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01


@dataclass(frozen=True)
class CheckResult:
    """One pre-flight check's outcome.

    Attributes:
      name:    Stable identifier for the check (e.g. "clean_tree").
      passed:  True if the run may proceed past this gate.
      detail:  Human-readable summary of what was observed.
      data:    Optional structured fields for programmatic post-mortem.
    """

    name: str
    passed: bool
    detail: str
    data: dict = field(default_factory=dict)


# ──────────────────────────── Clean-tree gate ────────────────────────


def _git_status_porcelain(scope_paths: list[Path], *, cwd: Path) -> list[str]:
    """Return the porcelain output of ``git status -- <scope>`` as a list of lines.

    Empty list means the scope is clean for tracked files. The check
    intentionally tolerates untracked files outside ``scope_paths`` (per
    § 9 "Untracked files outside the run scope are tolerated").
    """
    args = ["git", "status", "--porcelain", "--"] + [str(p) for p in scope_paths]
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=10.0,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git status failed: rc={proc.returncode} stderr={proc.stderr!r}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def check_clean_tree(scope_paths: list[Path], *, repo_root: Path) -> CheckResult:
    """Refuse to run with a dirty source tree (§ 9).

    Runs ``git status --porcelain`` for the listed scope paths
    (typically ``PythonDataService/`` and ``references/qc-shadow/``).
    Pass = empty output. Untracked files NOT under ``scope_paths``
    don't show up in the scoped output — they're tolerated.

    This is what makes ``LiveRunLedger.code_sha`` (set to
    ``git rev-parse HEAD``) actually identify the running code: a dirty
    tree would mean the running code has uncommitted deltas that
    ``HEAD`` doesn't capture.
    """
    try:
        dirty = _git_status_porcelain(scope_paths, cwd=repo_root)
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name="clean_tree",
            passed=False,
            detail=f"git status failed: {exc}",
            data={"error": str(exc)},
        )
    if dirty:
        return CheckResult(
            name="clean_tree",
            passed=False,
            detail=(
                f"working tree has {len(dirty)} uncommitted change(s) within scope "
                f"{[str(p) for p in scope_paths]}: {dirty[:5]}"
            ),
            data={"dirty_lines": dirty},
        )
    return CheckResult(
        name="clean_tree",
        passed=True,
        detail=f"clean tree across {[str(p) for p in scope_paths]}",
    )


# ──────────────────────────── NTP gate ───────────────────────────────


def query_ntp_offset_seconds(server: str = "pool.ntp.org", *, timeout_seconds: float = 5.0) -> float:
    """Return ``ntp_time - local_time`` in seconds (stdlib SNTP, no extra deps).

    Sends a single SNTP request to ``server:123`` and reads the 48-byte
    response. The transmit timestamp (offset 40, 8 bytes) is interpreted
    as a fixed-point seconds-and-fraction since 1900-01-01 UTC. We
    subtract the Unix epoch offset and compare against ``time.time()``.

    Raises ``OSError``/``socket.timeout`` on transport failure — caller
    decides whether to treat as halt.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_seconds)
    try:
        # LI=0 (no warning), VN=4, Mode=3 (client). Header byte = 0x23.
        request = b"\x23" + b"\x00" * 47
        sock.sendto(request, (server, 123))
        local_send_time = time.time()
        data, _ = sock.recvfrom(NTP_PACKET_SIZE)
        local_recv_time = time.time()
    finally:
        sock.close()

    if len(data) < NTP_PACKET_SIZE:
        raise OSError(f"short NTP response: got {len(data)} bytes, expected {NTP_PACKET_SIZE}")

    # Transmit timestamp at offset 40, big-endian "seconds | fraction".
    secs, frac = struct.unpack("!II", data[40:48])
    ntp_seconds_since_1900 = secs + frac / 2**32
    ntp_unix_seconds = ntp_seconds_since_1900 - NTP_EPOCH_OFFSET_SEC
    # Compare against local-clock midpoint of the round-trip — this
    # correctly handles non-trivial RTT but does not correct for
    # network asymmetry. For our 1-second budget that's fine.
    local_midpoint = (local_send_time + local_recv_time) / 2.0
    return ntp_unix_seconds - local_midpoint


def check_ntp_offset(
    *,
    server: str = "pool.ntp.org",
    max_offset_seconds: float = 1.0,
    timeout_seconds: float = 5.0,
) -> CheckResult:
    """Refuse to run if local clock drifts more than ``max_offset_seconds`` from NTP.

    The fill-time reconciliation tolerance is ±5 s (§ 6.3); a 1 s NTP
    budget keeps clock drift well under the meaningful comparison
    window. Server failures (DNS, packet loss, timeouts) are treated as
    halt — we'd rather pause a session than reconcile against an
    unverified clock.
    """
    try:
        offset = query_ntp_offset_seconds(server, timeout_seconds=timeout_seconds)
    except (TimeoutError, OSError) as exc:
        return CheckResult(
            name="ntp_offset",
            passed=False,
            detail=f"NTP query to {server} failed: {exc}",
            data={"error": str(exc), "server": server},
        )
    abs_offset = abs(offset)
    if abs_offset > max_offset_seconds:
        return CheckResult(
            name="ntp_offset",
            passed=False,
            detail=(
                f"local clock drift {offset:+.3f}s exceeds {max_offset_seconds}s budget "
                f"vs {server}"
            ),
            data={"offset_seconds": offset, "server": server},
        )
    return CheckResult(
        name="ntp_offset",
        passed=True,
        detail=f"clock drift {offset:+.3f}s within {max_offset_seconds}s budget",
        data={"offset_seconds": offset, "server": server},
    )


# ──────────────────────────── Unexpected-position gate ───────────────


class _PositionsLike(Protocol):
    """Minimal duck-type for the broker positions snapshot we need.

    Mirrors ``IbkrPositionsSnapshot``'s shape but as a Protocol so the
    test can pass any object with ``positions: list[<has symbol+quantity>]``.
    """

    @property
    def positions(self) -> list: ...  # pragma: no cover


def check_all_in_coexistence(
    *,
    proposed_symbol: str,
    proposed_sizing: object,
    broker_positions: _PositionsLike | None,
    sibling_all_in_symbols: set[str] | None = None,
) -> CheckResult:
    """ADR 0009 § 9 / Decision 13 — symbol-scoped all-in coexistence guard.

    The interim stand-in for the capital-sleeve layer (which is deferred).
    Refuses to start a new run when **resolved sizing is ``SetHoldings(1.0)``**
    *and* either:

    1. The bound trade symbol has non-zero exposure in the broker account
       (any source — managed or not). An existing all-in position would
       silently absorb a second all-in target.
    2. Another managed live binding on this account holds ``SetHoldings(1.0)``
       on the **same symbol** (passed via ``sibling_all_in_symbols``).

    ``FixedShares`` / ``FixedNotional`` are **never** blocked — those policies
    don't fight for the whole portfolio. Cross-symbol all-in concurrency
    (SPY all-in + AAPL all-in on the same cash account) is **permitted-but-unsafe**
    in v1; the capital-sleeve layer (Decision 9) is what eventually closes
    it. We do not block cross-symbol because account-wide blocking would
    refuse a clean SPY canary deploy onto an account that holds an
    unrelated manual position.

    ``proposed_sizing`` is taken as ``object`` to keep the signature usable
    from both the typed runtime path (a ``SizingPolicy`` instance) and the
    deploy-form gate (a raw dict). The function isinstance-checks and dict-
    sniffs to decide whether the policy is the gating ``SetHoldings(1.0)``.

    When ``broker_positions`` is ``None`` the broker probe is unavailable;
    the check returns ``passed=False`` with a "broker unreachable" detail so
    the operator sees the failure mode rather than a silent pass that could
    deploy onto a non-flat account.
    """
    from decimal import Decimal as _Decimal

    # Sniff the policy. Anything other than SetHoldings(1.0) is unconditionally OK.
    is_all_in = _is_set_holdings_full(proposed_sizing)
    if not is_all_in:
        return CheckResult(
            name="all_in_coexistence",
            passed=True,
            detail="not all-in; coexistence guard does not apply",
        )

    expected = proposed_symbol.upper()
    sibling = {s.upper() for s in sibling_all_in_symbols} if sibling_all_in_symbols else set()
    if expected in sibling:
        return CheckResult(
            name="all_in_coexistence",
            passed=False,
            detail=(
                f"another managed live binding on this account already holds "
                f"SetHoldings(1.0) on {expected}; the capital-sleeve layer is the "
                "fix and is not yet built"
            ),
            data={"reason": "sibling_all_in", "symbol": expected},
        )

    if broker_positions is None:
        return CheckResult(
            name="all_in_coexistence",
            passed=False,
            detail=(
                "broker positions unreachable; cannot prove the trade symbol is flat "
                "before launching an all-in run"
            ),
            data={"reason": "broker_unreachable", "symbol": expected},
        )

    for pos in broker_positions.positions:
        symbol = str(pos.symbol).upper()
        if symbol != expected:
            continue
        try:
            qty = _Decimal(str(pos.quantity))
        except (ValueError, TypeError):
            continue
        if qty != 0:
            return CheckResult(
                name="all_in_coexistence",
                passed=False,
                detail=(
                    f"existing exposure on {expected} ({qty}) would silently absorb "
                    "an all-in target; flatten or wait before launching"
                ),
                data={
                    "reason": "symbol_exposure_present",
                    "symbol": expected,
                    "quantity": str(qty),
                },
            )

    return CheckResult(
        name="all_in_coexistence",
        passed=True,
        detail=f"all-in on {expected} cleared (symbol is flat and no sibling holds it)",
    )


def _is_set_holdings_full(policy: object) -> bool:
    """True iff ``policy`` is the gating ``SetHoldings(1.0)`` shape.

    Accepts both the typed ``SizingPolicy`` form and a raw dict (the
    deploy-form gate calls with the operator's submitted payload before it
    has been parsed by the discriminated union).
    """
    from decimal import Decimal as _Decimal

    from app.engine.execution.order_sizer import SetHoldings

    if isinstance(policy, SetHoldings):
        return policy.fraction == _Decimal("1.0")
    if isinstance(policy, dict):
        if policy.get("kind") != "SetHoldings":
            return False
        fraction = policy.get("fraction")
        try:
            return _Decimal(str(fraction)) == _Decimal("1.0")
        except (ValueError, TypeError):
            return False
    return False


def check_unexpected_position(
    snapshot: _PositionsLike,
    *,
    expected_symbol: str,
    managed_symbols: set[str] | None = None,
) -> CheckResult:
    """Refuse to run if the broker holds a position this strategy instance
    cannot account for.

    Two failure shapes flag the account as unmodellable:

    - a **short** position in ``expected_symbol`` (the strategy is long-only);
    - a position in a symbol **outside all managed namespaces** — a
      foreign/manual position or a leftover from a prior halted run.

    ``managed_symbols`` is the union of symbols owned by every managed
    strategy instance on this account (including this one). A position in a
    *sibling* managed instance's symbol is **not** this instance's
    contamination and is excluded from the verdict — otherwise two managed
    instances on one account each falsely flag the other. The per-instance
    gate is self-consistency only; account-level contamination is a separate
    fleet concern (ADR 0005). When ``managed_symbols`` is ``None`` the managed
    set is just ``{expected_symbol}``, preserving the original
    single-instance behaviour; the host daemon widens it with sibling
    symbols (#392).
    """
    expected = expected_symbol.upper()
    managed = {s.upper() for s in managed_symbols} if managed_symbols else {expected}
    managed.add(expected)
    bad_positions: list[dict] = []
    for pos in snapshot.positions:
        symbol = str(pos.symbol).upper()
        quantity = float(pos.quantity)
        if symbol == expected:
            if quantity < 0:
                bad_positions.append({"symbol": symbol, "quantity": quantity, "reason": "short_position"})
            continue
        if symbol in managed:
            # A managed sibling instance owns this symbol — not this
            # instance's contamination. Excluded from the self-consistency
            # verdict; the fleet view (ADR 0005) owns account-level residual.
            continue
        bad_positions.append({"symbol": symbol, "quantity": quantity, "reason": "non_strategy_symbol"})
    if bad_positions:
        return CheckResult(
            name="unexpected_position",
            passed=False,
            detail=f"{len(bad_positions)} unexpected position(s): {bad_positions[:3]}",
            data={"unexpected": bad_positions},
        )
    return CheckResult(
        name="unexpected_position",
        passed=True,
        detail=f"no unexpected positions; only allowed shape is long {expected_symbol}",
    )


# ──────────────────────────── Run-state gate ─────────────────────────


def check_run_state_intact(run_dir: Path) -> CheckResult:
    """Verify the run directory carries an intact ledger.

    Existence of ``run_ledger.json`` is mandatory for any session past
    day 1 — it's what binds the run identity (§ 10).
    """
    ledger_path = run_dir / "run_ledger.json"
    if not ledger_path.exists():
        return CheckResult(
            name="run_state_intact",
            passed=False,
            detail=f"run_ledger.json not found at {ledger_path}",
        )
    try:
        json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="run_state_intact",
            passed=False,
            detail=f"run_ledger.json at {ledger_path} is unreadable: {exc}",
            data={"error": str(exc)},
        )
    return CheckResult(
        name="run_state_intact",
        passed=True,
        detail=f"run_ledger.json present and parseable at {ledger_path}",
    )


# ──────────────────────────── No-halt-flag gate ──────────────────────


def check_no_halt_flag(run_dir: Path) -> CheckResult:
    """Refuse to run if a prior day's reconciliation wrote a ``halt.flag``.

    See ``app.engine.live.reconcile.write_day_report`` — a halt flag is
    written whenever an engine-class divergence or fill breach occurred
    on the prior day (§ 6.4). The flag stays in place until an operator
    resolves it; resuming requires a new ``run_id`` per § 7.2.

    Distinct from ``poisoned.flag`` (§ 7) which is a fatal-halt sentinel
    written intra-day for broker-state divergence; that gate lives in
    ``halt.py`` because it has different semantics (no resume on same
    run_id at all, vs. halt-this-session-only here).
    """
    flag = run_dir / "halt.flag"
    if not flag.exists():
        return CheckResult(name="no_halt_flag", passed=True, detail="no prior-day halt flag set")
    try:
        payload = json.loads(flag.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return CheckResult(
        name="no_halt_flag",
        passed=False,
        detail=f"halt.flag set by prior day: {payload}",
        data={"halt_payload": payload},
    )


# ─────────────── Sizing-policy-present gate (Phase 1 / VCR-0001) ─────


def check_sizing_policy_present(live_config: dict) -> CheckResult:
    """Refuse to start a run whose ledger has no explicit ``sizing`` policy.

    VCR-0001 / Phase 1 — closes the back door where a pre-policy ledger
    (legacy ``live_config={}`` or one carrying siblings without ``sizing``)
    would start under the legacy ``SimpleFloorSizing`` all-in path. Mirrors
    the deploy-boundary refusal so the operator's next step is to redeploy
    with an explicit policy. There is no override flag: ``live_config`` is
    hashed into ``run_id``, so a start-time effective-sizing change would
    make the identity fingerprint dishonest.

    The pre-flight surface returns the failure as a structured ``CheckResult``
    so the operator-facing pre-flight subcommand renders the same message;
    the runtime gate in ``cmd_start`` raises the refusal directly.
    """
    if isinstance(live_config, dict) and live_config.get("sizing") is not None:
        return CheckResult(
            name="sizing_policy_present",
            passed=True,
            detail="live_config.sizing present",
        )
    return CheckResult(
        name="sizing_policy_present",
        passed=False,
        detail=(
            "live_config.sizing missing — Phase 1 / ADR 0009 requires every new "
            "run to carry an explicit sizing policy. Redeploy with an explicit "
            "policy (Safe canary: {'sizing': {'kind': 'FixedShares', 'value': 1}})."
        ),
    )


# ──────────────────────────── Yesterday-artifacts gate ───────────────


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_yesterday_artifacts_valid(
    *,
    run_dir: Path,
    qc_dir: Path,
    docs_dir: Path,
    yesterday_day_n: int,
) -> CheckResult:
    """Refuse to run if yesterday's reconciliation receipts don't reconcile (§ 6.4 #6, #7).

    Walks the SHA-256 sidecar that ``reconcile.py`` writes alongside
    each daily report and verifies every named artifact still hashes to
    the value recorded — catches silent corruption, accidental
    overwrites, and missing QC exports between sessions.

    ``yesterday_day_n`` is 1-indexed; pass ``day_n=0`` to short-circuit
    the check on day 1 of a run (no prior receipt yet).
    """
    if yesterday_day_n <= 0:
        return CheckResult(
            name="yesterday_artifacts",
            passed=True,
            detail="day 1 of run; no prior-day artifacts to verify",
        )

    sidecar = run_dir / "reconcile" / f"day-{yesterday_day_n}.hashes.json"
    if not sidecar.exists():
        return CheckResult(
            name="yesterday_artifacts",
            passed=False,
            detail=f"hash sidecar missing: {sidecar}",
        )
    try:
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="yesterday_artifacts",
            passed=False,
            detail=f"hash sidecar unreadable: {exc}",
            data={"error": str(exc)},
        )

    # Per § 6.5, the sidecar records hashes for these seven artifacts
    # the day's md summarizes. We iterate over the *expected* keyset
    # (not just whatever the sidecar happens to carry) so a sidecar
    # missing one of the required keys is caught as a halt rather than
    # silently passing — that was a CodeRabbit P1 from the original
    # Phase C-1 PR.
    targets: dict[str, Path] = {
        "reconcile_json": run_dir / "reconcile" / f"day-{yesterday_day_n}.json",
        "reconcile_parquet": run_dir / "reconcile" / f"day-{yesterday_day_n}.parquet",
        "python_executions_parquet": run_dir / "executions.parquet",
        "python_trades_parquet": run_dir / "trades.parquet",
        "qc_export_trades": qc_dir / "trades.csv",
        "qc_export_indicators": qc_dir / "indicators.csv",
        "run_ledger": run_dir / "run_ledger.json",
    }

    mismatches: list[dict] = []
    for key, path in targets.items():
        if key not in manifest:
            mismatches.append({"key": key, "reason": "missing_from_manifest"})
            continue
        recorded = manifest[key]
        if recorded is None:
            # Spec § 6.5: explicit ``~`` / null means this artifact
            # wasn't part of yesterday's receipt (e.g. no QC export
            # because day 0). Tolerated only if the path is also
            # absent on disk — otherwise something wrote the file
            # outside the reconciler.
            if path.exists():
                mismatches.append(
                    {"key": key, "path": str(path), "reason": "manifest_null_but_file_present"}
                )
            continue
        if not path.exists():
            mismatches.append({"key": key, "path": str(path), "reason": "missing"})
            continue
        actual = _file_sha256(path)
        if actual != recorded:
            mismatches.append(
                {
                    "key": key,
                    "path": str(path),
                    "reason": "hash_mismatch",
                    "recorded": recorded,
                    "actual": actual,
                }
            )

    md_path = docs_dir / f"day-{yesterday_day_n}.md"
    if not md_path.exists():
        mismatches.append({"key": "day_md", "path": str(md_path), "reason": "missing"})

    if mismatches:
        return CheckResult(
            name="yesterday_artifacts",
            passed=False,
            detail=f"{len(mismatches)} artifact issue(s): {mismatches[:3]}",
            data={"mismatches": mismatches},
        )
    return CheckResult(
        name="yesterday_artifacts",
        passed=True,
        detail=f"all yesterday-day-{yesterday_day_n} artifacts verified",
    )


# ──────────────────────────── Orchestrator ───────────────────────────


def run_pre_flight(checks: list[CheckResult]) -> tuple[bool, list[CheckResult]]:
    """Aggregate a list of check results into a halt-or-pass verdict.

    Returns ``(all_passed, checks)``. Any failure means halt; the call
    site logs every result regardless so the morning report can show
    why we stopped (or that we didn't).
    """
    all_passed = all(r.passed for r in checks)
    return all_passed, checks

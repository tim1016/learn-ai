"""Tests for app.engine.live.pre_flight.

Each halt rule has at least one pass case and one fail case. NTP is
exercised both happy-path (mocked successful response) and degraded
(socket error → halt).

The clean-tree check uses a real on-disk git repo built per-test in
``tmp_path`` — no monkey-patching of subprocess; the actual
``git status`` command runs.
"""

from __future__ import annotations

import json
import shutil
import socket
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.engine.live.pre_flight import (
    NTP_EPOCH_OFFSET_SEC,
    NTP_PACKET_SIZE,
    check_clean_tree,
    check_no_halt_flag,
    check_ntp_offset,
    check_run_state_intact,
    check_unexpected_position,
    check_yesterday_artifacts_valid,
    query_ntp_offset_seconds,
    run_pre_flight,
)

# git is required for the clean_tree tests; skipped in environments
# (e.g. the polygon-data-service container) where the binary isn't
# installed. CI runs natively on ubuntu-latest, where git is present.
requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in this environment",
)

# ──────────────────────────── git-repo fixture ───────────────────────


def _init_repo(repo: Path) -> None:
    """Initialise a tiny on-disk git repo so check_clean_tree can run."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Bare clean git repo in ``tmp_path/repo`` with one initial commit."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_repo(repo_path)
    (repo_path / "PythonDataService").mkdir()
    (repo_path / "PythonDataService" / "init.py").write_text("# initial\n")
    subprocess.run(
        ["git", "add", "PythonDataService/init.py"],
        cwd=repo_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial", "--no-gpg-sign"],
        cwd=repo_path,
        check=True,
    )
    return repo_path


# ──────────────────────────── check_clean_tree ───────────────────────


@requires_git
def test_clean_tree_passes_on_clean_scope(repo: Path) -> None:
    result = check_clean_tree([Path("PythonDataService")], repo_root=repo)
    assert result.passed is True
    assert "clean tree" in result.detail


@requires_git
def test_clean_tree_fails_when_scope_has_modifications(repo: Path) -> None:
    (repo / "PythonDataService" / "init.py").write_text("# modified\n")
    result = check_clean_tree([Path("PythonDataService")], repo_root=repo)
    assert result.passed is False
    assert "uncommitted change" in result.detail


@requires_git
def test_clean_tree_tolerates_untracked_files_outside_scope(repo: Path) -> None:
    """Untracked files outside scope_paths must NOT trip the gate (§ 9)."""
    (repo / "outside.txt").write_text("scratch\n")
    result = check_clean_tree([Path("PythonDataService")], repo_root=repo)
    assert result.passed is True


@requires_git
def test_clean_tree_fails_when_scope_has_untracked_files(repo: Path) -> None:
    """Untracked files INSIDE scope are uncommitted state and should halt."""
    (repo / "PythonDataService" / "scratch.py").write_text("temp\n")
    result = check_clean_tree([Path("PythonDataService")], repo_root=repo)
    assert result.passed is False


# ──────────────────────────── check_ntp_offset ───────────────────────


def _build_ntp_response(unix_seconds: float) -> bytes:
    """Construct a 48-byte SNTP response packet with the given Unix time as transmit timestamp."""
    ntp_secs_1900 = unix_seconds + NTP_EPOCH_OFFSET_SEC
    secs = int(ntp_secs_1900)
    frac = int((ntp_secs_1900 - secs) * (2**32))
    packet = bytearray(NTP_PACKET_SIZE)
    packet[40:48] = struct.pack("!II", secs, frac)
    return bytes(packet)


class _FakeSocket:
    """Minimal socket double that returns a fixed NTP response."""

    def __init__(self, response: bytes | None, *, raise_on_send: type[Exception] | None = None) -> None:
        self._response = response
        self._raise = raise_on_send
        self.closed = False

    def settimeout(self, _: float) -> None:
        pass

    def sendto(self, *_args, **_kwargs) -> None:
        if self._raise is not None:
            raise self._raise("simulated network failure")

    def recvfrom(self, _bufsize: int) -> tuple[bytes, tuple[str, int]]:
        assert self._response is not None
        return self._response, ("1.2.3.4", 123)

    def close(self) -> None:
        self.closed = True


def test_check_ntp_offset_passes_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_mod

    fixed_local = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: fixed_local)
    response = _build_ntp_response(fixed_local + 0.05)  # 50 ms drift, under 1 s budget

    monkeypatch.setattr(socket, "socket", lambda _f, _t: _FakeSocket(response))

    result = check_ntp_offset(max_offset_seconds=1.0)
    assert result.passed is True
    assert "0.050s" in result.detail or "0.05" in result.detail


def test_check_ntp_offset_halts_on_drift_above_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_mod

    fixed_local = 1_700_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: fixed_local)
    response = _build_ntp_response(fixed_local + 2.5)  # 2.5 s drift, over 1 s budget

    monkeypatch.setattr(socket, "socket", lambda _f, _t: _FakeSocket(response))

    result = check_ntp_offset(max_offset_seconds=1.0)
    assert result.passed is False
    assert "exceeds" in result.detail


def test_check_ntp_offset_halts_on_socket_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "socket",
        lambda _f, _t: _FakeSocket(None, raise_on_send=OSError),
    )
    result = check_ntp_offset(timeout_seconds=0.1)
    assert result.passed is False
    assert "NTP query" in result.detail


def test_query_ntp_offset_seconds_rejects_short_response(monkeypatch: pytest.MonkeyPatch) -> None:
    short = b"\x00" * 10  # < 48 bytes
    monkeypatch.setattr(socket, "socket", lambda _f, _t: _FakeSocket(short))
    with pytest.raises(OSError, match="short NTP response"):
        query_ntp_offset_seconds(timeout_seconds=0.1)


# ──────────────────────────── check_unexpected_position ──────────────


@dataclass
class _Pos:
    symbol: str
    quantity: float


@dataclass
class _Snap:
    positions: list


def test_unexpected_position_passes_for_empty_snapshot() -> None:
    result = check_unexpected_position(_Snap(positions=[]), expected_symbol="SPY")
    assert result.passed is True


def test_unexpected_position_passes_for_long_spy_only() -> None:
    snap = _Snap(positions=[_Pos(symbol="SPY", quantity=200)])
    result = check_unexpected_position(snap, expected_symbol="SPY")
    assert result.passed is True


def test_unexpected_position_fails_on_non_strategy_symbol() -> None:
    snap = _Snap(positions=[_Pos(symbol="QQQ", quantity=10)])
    result = check_unexpected_position(snap, expected_symbol="SPY")
    assert result.passed is False
    assert "non_strategy_symbol" in str(result.data)


def test_unexpected_position_fails_on_short_strategy_symbol() -> None:
    snap = _Snap(positions=[_Pos(symbol="SPY", quantity=-50)])
    result = check_unexpected_position(snap, expected_symbol="SPY")
    assert result.passed is False
    assert "short_position" in str(result.data)


def test_unexpected_position_excludes_sibling_managed_symbol() -> None:
    """Two managed instances on one account (SPY + QQQ): the SPY instance must
    not flag the sibling's QQQ position as contamination (#395 regression)."""
    snap = _Snap(positions=[_Pos(symbol="SPY", quantity=100), _Pos(symbol="QQQ", quantity=50)])
    result = check_unexpected_position(
        snap, expected_symbol="SPY", managed_symbols={"SPY", "QQQ"}
    )
    assert result.passed is True


def test_unexpected_position_still_fails_on_foreign_symbol_outside_managed_set() -> None:
    """A symbol owned by no managed instance is still foreign contamination."""
    snap = _Snap(positions=[_Pos(symbol="SPY", quantity=100), _Pos(symbol="GOOG", quantity=10)])
    result = check_unexpected_position(
        snap, expected_symbol="SPY", managed_symbols={"SPY", "QQQ"}
    )
    assert result.passed is False
    assert "non_strategy_symbol" in str(result.data)
    assert "GOOG" in str(result.data)
    assert "QQQ" not in str(result.data)


def test_unexpected_position_still_fails_on_short_own_symbol_within_managed_set() -> None:
    """A short in the instance's own symbol fails even when a managed set is given."""
    snap = _Snap(positions=[_Pos(symbol="SPY", quantity=-100)])
    result = check_unexpected_position(
        snap, expected_symbol="SPY", managed_symbols={"SPY", "QQQ"}
    )
    assert result.passed is False
    assert "short_position" in str(result.data)


# ──────────────────────────── check_run_state_intact ─────────────────


def test_run_state_intact_passes_when_ledger_present(tmp_path: Path) -> None:
    (tmp_path / "run_ledger.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    result = check_run_state_intact(tmp_path)
    assert result.passed is True


def test_run_state_intact_fails_when_ledger_missing(tmp_path: Path) -> None:
    result = check_run_state_intact(tmp_path)
    assert result.passed is False
    assert "not found" in result.detail


def test_run_state_intact_fails_on_corrupt_ledger(tmp_path: Path) -> None:
    (tmp_path / "run_ledger.json").write_text("not json", encoding="utf-8")
    result = check_run_state_intact(tmp_path)
    assert result.passed is False
    assert "unreadable" in result.detail


# ──────────────────────────── check_no_halt_flag ─────────────────────


def test_no_halt_flag_passes_when_absent(tmp_path: Path) -> None:
    result = check_no_halt_flag(tmp_path)
    assert result.passed is True


def test_no_halt_flag_fails_when_flag_present(tmp_path: Path) -> None:
    (tmp_path / "halt.flag").write_text(
        json.dumps({"day_n": 3, "reasons": ["engine-class divergence count=1"]}),
        encoding="utf-8",
    )
    result = check_no_halt_flag(tmp_path)
    assert result.passed is False
    assert "engine-class" in str(result.data)


# ──────────────────────────── check_yesterday_artifacts_valid ────────


def test_yesterday_artifacts_short_circuits_on_day_zero(tmp_path: Path) -> None:
    result = check_yesterday_artifacts_valid(
        run_dir=tmp_path,
        qc_dir=tmp_path / "qc",
        docs_dir=tmp_path / "docs",
        yesterday_day_n=0,
    )
    assert result.passed is True


def test_yesterday_artifacts_passes_when_all_hashes_match(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    qc_dir = tmp_path / "qc"
    docs_dir = tmp_path / "docs"
    (run_dir / "reconcile").mkdir(parents=True)
    qc_dir.mkdir()
    docs_dir.mkdir()

    json_path = run_dir / "reconcile" / "day-1.json"
    parquet_path = run_dir / "reconcile" / "day-1.parquet"
    qc_trades = qc_dir / "trades.csv"
    qc_indicators = qc_dir / "indicators.csv"
    md_path = docs_dir / "day-1.md"
    json_path.write_bytes(b'{"summary": {}}')
    parquet_path.write_bytes(b"parquet-bytes")
    qc_trades.write_text("entry_time_ms\n1\n")
    qc_indicators.write_text("bar_close_ms,signal\n1,HOLD\n")
    md_path.write_text("# day 1\n")

    import hashlib

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    # Spec § 6.5 manifest carries all seven keys — null for the ones
    # whose artifacts don't exist for this scenario (no python
    # executions / trades / run ledger in this minimal fixture).
    sidecar = run_dir / "reconcile" / "day-1.hashes.json"
    sidecar.write_text(
        json.dumps(
            {
                "reconcile_json": sha(json_path),
                "reconcile_parquet": sha(parquet_path),
                "python_executions_parquet": None,
                "python_trades_parquet": None,
                "qc_export_trades": sha(qc_trades),
                "qc_export_indicators": sha(qc_indicators),
                "run_ledger": None,
            }
        ),
        encoding="utf-8",
    )

    result = check_yesterday_artifacts_valid(
        run_dir=run_dir, qc_dir=qc_dir, docs_dir=docs_dir, yesterday_day_n=1
    )
    assert result.passed is True


def test_yesterday_artifacts_fails_when_required_key_missing_from_manifest(tmp_path: Path) -> None:
    """CodeRabbit P1 fix — a sidecar missing one of the seven required
    keys is no longer treated as 'pass'; it's a missing-from-manifest halt."""
    run_dir = tmp_path / "run"
    qc_dir = tmp_path / "qc"
    docs_dir = tmp_path / "docs"
    (run_dir / "reconcile").mkdir(parents=True)
    qc_dir.mkdir()
    docs_dir.mkdir()
    (docs_dir / "day-1.md").write_text("# day 1\n")

    sidecar = run_dir / "reconcile" / "day-1.hashes.json"
    sidecar.write_text(
        json.dumps(
            {
                # 'reconcile_json' is intentionally absent.
                "reconcile_parquet": None,
                "python_executions_parquet": None,
                "python_trades_parquet": None,
                "qc_export_trades": None,
                "qc_export_indicators": None,
                "run_ledger": None,
            }
        ),
        encoding="utf-8",
    )

    result = check_yesterday_artifacts_valid(
        run_dir=run_dir, qc_dir=qc_dir, docs_dir=docs_dir, yesterday_day_n=1
    )
    assert result.passed is False
    assert "missing_from_manifest" in str(result.data)


def test_yesterday_artifacts_fails_when_manifest_null_but_file_present(tmp_path: Path) -> None:
    """A null manifest entry must not silently skip when the artifact actually exists.

    That would mean the reconciler said 'no artifact here' but something
    else wrote one — broker-state divergence by another name.
    """
    run_dir = tmp_path / "run"
    qc_dir = tmp_path / "qc"
    docs_dir = tmp_path / "docs"
    (run_dir / "reconcile").mkdir(parents=True)
    qc_dir.mkdir()
    docs_dir.mkdir()
    (docs_dir / "day-1.md").write_text("# day 1\n")

    # File present on disk, sidecar says null.
    (run_dir / "executions.parquet").write_bytes(b"unexpected")

    sidecar = run_dir / "reconcile" / "day-1.hashes.json"
    sidecar.write_text(
        json.dumps(
            {
                "reconcile_json": None,
                "reconcile_parquet": None,
                "python_executions_parquet": None,
                "python_trades_parquet": None,
                "qc_export_trades": None,
                "qc_export_indicators": None,
                "run_ledger": None,
            }
        ),
        encoding="utf-8",
    )

    result = check_yesterday_artifacts_valid(
        run_dir=run_dir, qc_dir=qc_dir, docs_dir=docs_dir, yesterday_day_n=1
    )
    assert result.passed is False
    assert "manifest_null_but_file_present" in str(result.data)


def test_yesterday_artifacts_fails_when_sidecar_missing(tmp_path: Path) -> None:
    result = check_yesterday_artifacts_valid(
        run_dir=tmp_path,
        qc_dir=tmp_path / "qc",
        docs_dir=tmp_path / "docs",
        yesterday_day_n=1,
    )
    assert result.passed is False
    assert "hash sidecar missing" in result.detail


def test_yesterday_artifacts_fails_when_artifact_hash_mismatches(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    qc_dir = tmp_path / "qc"
    docs_dir = tmp_path / "docs"
    (run_dir / "reconcile").mkdir(parents=True)
    qc_dir.mkdir()
    docs_dir.mkdir()

    json_path = run_dir / "reconcile" / "day-1.json"
    json_path.write_bytes(b'{"summary": {}}')
    (docs_dir / "day-1.md").write_text("# day 1\n")

    sidecar = run_dir / "reconcile" / "day-1.hashes.json"
    # Complete manifest with all seven required keys; only
    # reconcile_json carries a (wrong) hash so the test isolates the
    # hash-mismatch detection path.
    sidecar.write_text(
        json.dumps(
            {
                "reconcile_json": "0" * 64,  # wrong hash
                "reconcile_parquet": None,
                "python_executions_parquet": None,
                "python_trades_parquet": None,
                "qc_export_trades": None,
                "qc_export_indicators": None,
                "run_ledger": None,
            }
        ),
        encoding="utf-8",
    )

    result = check_yesterday_artifacts_valid(
        run_dir=run_dir, qc_dir=qc_dir, docs_dir=docs_dir, yesterday_day_n=1
    )
    assert result.passed is False
    assert "hash_mismatch" in str(result.data)


def test_yesterday_artifacts_fails_when_md_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    qc_dir = tmp_path / "qc"
    docs_dir = tmp_path / "docs"
    (run_dir / "reconcile").mkdir(parents=True)
    qc_dir.mkdir()
    docs_dir.mkdir()
    sidecar = run_dir / "reconcile" / "day-1.hashes.json"
    sidecar.write_text(json.dumps({}), encoding="utf-8")

    result = check_yesterday_artifacts_valid(
        run_dir=run_dir, qc_dir=qc_dir, docs_dir=docs_dir, yesterday_day_n=1
    )
    assert result.passed is False
    assert "day_md" in str(result.data)


# ──────────────────────────── run_pre_flight aggregator ──────────────


def test_run_pre_flight_passes_when_all_pass() -> None:
    from app.engine.live.pre_flight import CheckResult

    checks = [CheckResult(name="a", passed=True, detail=""), CheckResult(name="b", passed=True, detail="")]
    all_passed, results = run_pre_flight(checks)
    assert all_passed is True
    assert len(results) == 2


def test_run_pre_flight_halts_when_any_fails() -> None:
    from app.engine.live.pre_flight import CheckResult

    checks = [
        CheckResult(name="a", passed=True, detail=""),
        CheckResult(name="b", passed=False, detail="bad"),
    ]
    all_passed, _ = run_pre_flight(checks)
    assert all_passed is False


# ────────────────── ADR 0009 PR5 — all-in coexistence guard ─────────────


def _positions_snapshot(positions: list[dict]) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        positions=[SimpleNamespace(**p) for p in positions]
    )


def test_all_in_coexistence_passes_for_non_all_in_policies() -> None:
    from decimal import Decimal as _Decimal

    from app.engine.execution.order_sizer import FixedNotional, FixedShares, SetHoldings
    from app.engine.live.pre_flight import check_all_in_coexistence

    snapshot = _positions_snapshot([{"symbol": "SPY", "quantity": 999}])
    assert check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing=FixedShares(value=1),
        broker_positions=snapshot,
    ).passed
    assert check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing=FixedNotional(value=_Decimal("100")),
        broker_positions=snapshot,
    ).passed
    assert check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing=None,
        broker_positions=snapshot,
    ).passed
    # SetHoldings(0.5) is also non-all-in (the 0<f<1 fractional case is
    # deferred but the guard only refuses 1.0).
    assert check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing=SetHoldings(fraction=_Decimal("0.5")),
        broker_positions=snapshot,
    ).passed


def test_all_in_coexistence_blocks_when_symbol_not_flat() -> None:
    from app.engine.live.pre_flight import check_all_in_coexistence

    snapshot = _positions_snapshot([{"symbol": "SPY", "quantity": 199}])
    result = check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing={"kind": "SetHoldings", "fraction": "1.0"},
        broker_positions=snapshot,
    )
    assert not result.passed
    assert result.data["reason"] == "symbol_exposure_present"


def test_all_in_coexistence_passes_when_symbol_flat() -> None:
    from app.engine.live.pre_flight import check_all_in_coexistence

    snapshot = _positions_snapshot([{"symbol": "QQQ", "quantity": 50}])
    result = check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing={"kind": "SetHoldings", "fraction": "1.0"},
        broker_positions=snapshot,
    )
    assert result.passed, (
        "Cross-symbol all-in is permitted-but-unsafe in v1 (Decision 13); the "
        "guard only refuses when the trade symbol itself is non-flat"
    )


def test_all_in_coexistence_blocks_on_sibling_all_in_same_symbol() -> None:
    from app.engine.live.pre_flight import check_all_in_coexistence

    snapshot = _positions_snapshot([])
    result = check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing={"kind": "SetHoldings", "fraction": "1.0"},
        broker_positions=snapshot,
        sibling_all_in_symbols={"SPY"},
    )
    assert not result.passed
    assert result.data["reason"] == "sibling_all_in"


def test_all_in_coexistence_fails_when_broker_unreachable() -> None:
    from app.engine.live.pre_flight import check_all_in_coexistence

    result = check_all_in_coexistence(
        proposed_symbol="SPY",
        proposed_sizing={"kind": "SetHoldings", "fraction": "1.0"},
        broker_positions=None,
    )
    assert not result.passed
    assert result.data["reason"] == "broker_unreachable"


# ─────────────── Phase 1 / VCR-0001 — sizing policy present ──────────


def test_sizing_policy_present_passes_for_safe_canary() -> None:
    """The Safe canary (``FixedShares(1)``) — the deploy-form default — is the
    canonical pass case for the new pre-flight check."""
    from app.engine.live.pre_flight import check_sizing_policy_present

    result = check_sizing_policy_present(
        {"sizing": {"kind": "FixedShares", "value": 1}}
    )
    assert result.passed
    assert result.name == "sizing_policy_present"


def test_sizing_policy_present_fails_for_legacy_ledger() -> None:
    """A pre-policy ledger (no ``sizing`` key) fails the gate — the runner must
    refuse to start, and the operator's next step is to redeploy with an explicit
    policy."""
    from app.engine.live.pre_flight import check_sizing_policy_present

    result = check_sizing_policy_present({})
    assert not result.passed
    assert "redeploy" in result.detail.lower()


def test_sizing_policy_present_fails_when_sibling_keys_only() -> None:
    """``live_config`` carrying siblings without ``sizing`` is the same legacy
    case — fail closed."""
    from app.engine.live.pre_flight import check_sizing_policy_present

    result = check_sizing_policy_present({"symbol": "SPY"})
    assert not result.passed
    assert "redeploy" in result.detail.lower()

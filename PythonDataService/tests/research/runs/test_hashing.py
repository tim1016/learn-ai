"""Properties of canonical-JSON hashing used by the run ledger."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import date
from pathlib import Path

from app.research.runs import ledger
from app.research.runs.hashing import (
    canonical_json,
    hash_payload,
    make_data_snapshot_id,
)
from app.research.runs.ledger import (
    compute_window_files_fingerprint,
    resolve_data_root_revision,
)


def test_canonical_json_key_order_independent():
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "y": 2, "x": 1}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_no_whitespace():
    s = canonical_json({"a": 1, "b": [2, 3]})
    assert " " not in s
    assert "\n" not in s
    assert s == '{"a":1,"b":[2,3]}'


def test_canonical_json_nested_keys_sorted():
    payload = {"outer": {"z": 1, "a": 2}, "alpha": [{"q": 1, "p": 0}]}
    s = canonical_json(payload)
    assert s == '{"alpha":[{"p":0,"q":1}],"outer":{"a":2,"z":1}}'


def test_canonical_json_preserves_unicode():
    payload = {"name": "résumé"}
    s = canonical_json(payload)
    assert "résumé" in s


def test_hash_payload_is_64_hex_chars():
    h = hash_payload({"a": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_payload_stable_across_calls():
    payload = {"name": "spec", "indicators": [{"id": "ema", "period": 10}]}
    assert hash_payload(payload) == hash_payload(payload)


def test_hash_payload_stable_across_key_order():
    a = {"id": "ema", "period": 10, "source": "close"}
    b = {"source": "close", "period": 10, "id": "ema"}
    assert hash_payload(a) == hash_payload(b)


def test_hash_payload_changes_on_value_change():
    base = {"period": 10}
    mutated = {"period": 11}
    assert hash_payload(base) != hash_payload(mutated)


def test_hash_payload_changes_on_key_change():
    a = {"period": 10}
    b = {"window": 10}
    assert hash_payload(a) != hash_payload(b)


def test_hash_payload_distinguishes_int_and_str():
    assert hash_payload({"x": 10}) != hash_payload({"x": "10"})


def test_make_data_snapshot_id_format():
    out = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc123",
    )
    assert out == "SPY|15|1700000000000|1710000000000|abc123"


def test_make_data_snapshot_id_changes_with_each_field():
    base = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_symbol = make_data_snapshot_id(
        symbol="QQQ",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_resolution = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=30,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_start = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_001,
        end_ms=1_710_000_000_000,
        data_root_revision="abc",
    )
    diff_end = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_001,
        data_root_revision="abc",
    )
    diff_revision = make_data_snapshot_id(
        symbol="SPY",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_710_000_000_000,
        data_root_revision="def",
    )
    assert len({base, diff_symbol, diff_resolution, diff_start, diff_end, diff_revision}) == 6


# ---------------------------------------------------------------------------
# Per-file mtime fingerprint — replaces the prior "directory mtime"
# revision suffix that was too coarse to detect cache-content drift.
# ---------------------------------------------------------------------------
def _write_empty_lean_minute_zip(root: Path, symbol: str, trading_date: date, mtime_s: float) -> Path:
    """Write a LEAN-shaped minute zip with a known mtime.

    The zip body is irrelevant to ``compute_window_files_fingerprint`` —
    only the file path and mtime matter — so we ship a minimal valid zip
    with an empty CSV. ``os.utime`` pins the mtime to a known value so
    the test asserts stable hashing.
    """
    sym_dir = root / "equity" / "usa" / "minute" / symbol.lower()
    sym_dir.mkdir(parents=True, exist_ok=True)
    zip_path = sym_dir / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{trading_date.strftime('%Y%m%d')}_{symbol.lower()}_minute_trade.csv", "")
    zip_path.write_bytes(buf.getvalue())
    os.utime(zip_path, (mtime_s, mtime_s))
    return zip_path


def test_compute_window_files_fingerprint_files_none_when_no_data_root(tmp_path):
    # No LEAN roots passed in, no matching files — sentinel value.
    out = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        data_roots=[],
    )
    assert out == "files:none"


def test_compute_window_files_fingerprint_files_none_when_window_has_no_files(tmp_path):
    # Data root exists but contains no zips for the window — still sentinel.
    out = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        data_roots=[tmp_path],
    )
    assert out == "files:none"


def test_compute_window_files_fingerprint_stable_when_unchanged(tmp_path):
    _write_empty_lean_minute_zip(tmp_path, "SPY", date(2024, 1, 2), 1_700_000_000.0)
    _write_empty_lean_minute_zip(tmp_path, "SPY", date(2024, 1, 3), 1_700_000_100.0)

    first = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        data_roots=[tmp_path],
    )
    second = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        data_roots=[tmp_path],
    )
    assert first == second
    assert first.startswith("files:")
    # 16 hex chars after the prefix.
    assert len(first) == len("files:") + 16


def test_compute_window_files_fingerprint_changes_when_an_mtime_changes(tmp_path):
    _write_empty_lean_minute_zip(tmp_path, "SPY", date(2024, 1, 2), 1_700_000_000.0)
    second_path = _write_empty_lean_minute_zip(
        tmp_path, "SPY", date(2024, 1, 3), 1_700_000_100.0
    )

    before = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        data_roots=[tmp_path],
    )

    # Touch the second file: same content, fresh mtime.
    new_mtime = 1_700_999_999.0
    os.utime(second_path, (new_mtime, new_mtime))

    after = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
        data_roots=[tmp_path],
    )
    assert before != after, (
        "Per-file mtime change must shift the fingerprint — that is the whole "
        "reason this replaced the directory-level mtime."
    )


def test_compute_window_files_fingerprint_ignores_files_outside_window(tmp_path):
    _write_empty_lean_minute_zip(tmp_path, "SPY", date(2024, 1, 2), 1_700_000_000.0)
    # File outside [start, end] — must not influence the fingerprint.
    out_of_window = _write_empty_lean_minute_zip(
        tmp_path, "SPY", date(2024, 1, 10), 1_700_000_500.0
    )

    inside_only = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        data_roots=[tmp_path],
    )

    # Touch the out-of-window file; fingerprint for the narrow window
    # must remain stable.
    os.utime(out_of_window, (1_799_999_999.0, 1_799_999_999.0))

    inside_only_after = compute_window_files_fingerprint(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        data_roots=[tmp_path],
    )
    assert inside_only == inside_only_after


def test_resolve_data_root_revision_prefers_window_files_before_later_git_root(
    tmp_path, monkeypatch
):
    reference_root = tmp_path / "reference"
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    first_path = _write_empty_lean_minute_zip(
        reference_root, "SPY", date(2024, 1, 2), 1_700_000_000.0
    )

    monkeypatch.setenv("LEAN_DATA_ROOT", str(reference_root))
    monkeypatch.setenv("LEAN_DATA_CACHE", str(cache_root))
    monkeypatch.delenv("LEAN_DATA_ROOT_REVISION", raising=False)

    def fake_run(*args, **kwargs):
        if kwargs.get("cwd") == str(cache_root):
            return ledger.subprocess.CompletedProcess(args[0], 0, stdout="cache-sha\n", stderr="")
        return ledger.subprocess.CompletedProcess(args[0], 1, stdout="", stderr="not a git repo")

    monkeypatch.setattr(ledger.subprocess, "run", fake_run)

    before = resolve_data_root_revision(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
    )
    os.utime(first_path, (1_700_000_100.0, 1_700_000_100.0))
    after = resolve_data_root_revision(
        symbol="SPY",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
    )

    assert before.startswith("files:")
    assert after.startswith("files:")
    assert before != "cache-sha"
    assert after != "cache-sha"
    assert before != after

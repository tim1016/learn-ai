"""``RunLedger`` — immutable run-identity record.

A ledger entry captures every input that, taken together, determines
the result of a backtest run. Two runs whose ledgers agree on
``strategy_spec_hash``, ``data_snapshot_id``, ``engine_version``,
fill/cost fields, and ``random_seed`` are guaranteed (by the
deterministic engine) to produce identical results — so their
``result_hash``, ``trade_log_hash``, and ``metrics_hash`` must match.
The EMA acceptance gate enforces this invariant.

Forward-compat fields included in v1 because they're cheap now and ugly
to migrate later:
  * ``parent_run_id`` — set on walk-forward fold runs (Phase C),
    Monte Carlo simulation runs (Phase D), sensitivity-sweep child runs
    (Phase E)
  * ``parent_spec_hash`` — set on sensitivity grid runs (Phase E) so the
    grid can be rolled up by spec lineage
  * ``random_seed`` — recorded even though the engine is currently
    deterministic without an RNG; locks Phase D's reshuffle/resample
    determinism contract before MC code lands
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.research.runs.window import WindowSummary

ENGINE_VERSION = "0.1.0"
"""Bumped on engine semantic change. See ``docs/references/run-ledger.md``."""

logger = logging.getLogger(__name__)

_GIT_COMMIT_CACHE: str | None = None


def _capture_git_commit() -> str:
    """Return the repo's ``HEAD`` SHA, captured once per process.

    Captured at first call and memoized — the value is constant for the
    lifetime of the process. Falls back to ``"unknown"`` when git is
    unavailable, the working tree isn't a git checkout, or the call
    times out. The ledger field exists so future replays can detect
    "ran on a different commit"; it's not load-bearing for correctness.
    """
    global _GIT_COMMIT_CACHE
    if _GIT_COMMIT_CACHE is not None:
        return _GIT_COMMIT_CACHE
    try:
        # Run from this file's directory — guarantees we land in the right
        # repo even when CWD differs (e.g., container running from /).
        cwd = Path(__file__).resolve().parent
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=str(cwd),
            check=False,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            if sha:
                _GIT_COMMIT_CACHE = sha
                return sha
        logger.debug(
            "[RUNS] git rev-parse HEAD returned %d; engine_git_commit will be 'unknown'",
            proc.returncode,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # FileNotFoundError → git binary missing (CI image without git).
        # TimeoutExpired → process hang. OSError → fork/exec failure.
        # Any of these are diagnostic, not fatal: the ledger field is
        # informational, not part of the deterministic identity contract.
        logger.debug("[RUNS] git rev-parse HEAD unavailable: %s", exc)
    _GIT_COMMIT_CACHE = "unknown"
    return _GIT_COMMIT_CACHE


def now_ms_utc() -> int:
    """Return current wall-clock time as ``int64 ms`` since Unix epoch UTC."""
    return int(time.time() * 1000)


def _data_root_paths() -> list[Path]:
    """Resolve ``LEAN_DATA_ROOT`` / ``LEAN_DATA_CACHE`` env vars to existing dirs."""
    roots: list[Path] = []
    for env_var in ("LEAN_DATA_ROOT", "LEAN_DATA_CACHE"):
        val = os.environ.get(env_var)
        if not val:
            continue
        root = Path(val)
        if root.is_dir():
            roots.append(root)
    return roots


def compute_window_files_fingerprint(
    *,
    symbol: str,
    start_date: Date,
    end_date: Date,
    data_roots: list[Path] | None = None,
) -> str:
    """Fingerprint per-file mtimes for every LEAN minute zip in the window.

    Returns ``"files:<sha256_first_16_hex>"`` over the sorted list of
    ``(filename, mtime_ms)`` tuples for every ``YYYYMMDD_trade.zip``
    file found under any data root for ``symbol`` whose date is inside
    ``[start_date, end_date]``. When no matching files exist (data root
    missing, window outside cache coverage), returns the literal
    ``"files:none"`` so the snapshot id is still well-formed.

    Replaces the prior ``mtime:<dir_mtime>`` suffix, which reflected the
    *data-root directory's* mtime — that value could be months stale even
    when the underlying bar files were fresh, masking cache-content drift.
    """
    roots = data_roots if data_roots is not None else _data_root_paths()
    if not roots:
        return "files:none"
    symbol_lower = symbol.lower()
    one_day = timedelta(days=1)
    entries: list[tuple[str, int]] = []
    current = start_date
    while current <= end_date:
        filename = f"{current.strftime('%Y%m%d')}_trade.zip"
        for root in roots:
            candidate = root / "equity" / "usa" / "minute" / symbol_lower / filename
            try:
                mtime_ms = int(candidate.stat().st_mtime * 1000)
            except OSError:
                continue
            entries.append((filename, mtime_ms))
            break
        current += one_day
    if not entries:
        return "files:none"
    entries.sort()
    payload = "\n".join(f"{name},{mtime}" for name, mtime in entries).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"files:{digest}"


def resolve_data_root_revision(
    *,
    symbol: str | None = None,
    start_date: Date | None = None,
    end_date: Date | None = None,
) -> str:
    """Identify the LEAN data root for ``data_snapshot_id``.

    Strategy (best to fallback):
      1. Env var ``LEAN_DATA_ROOT_REVISION`` if explicitly set — lets
         CI/ops pin a revision string.
      2. ``files:<sha256_first_16_hex>`` over per-file mtimes for the
         requested ``(symbol, start_date, end_date)`` window when those
         arguments are supplied and matching minute zips exist. This is
         evaluated before git probing so the snapshot follows
         ``LeanMinuteDataReader`` root precedence: the first zip hit for
         each date is the file that identifies the run.
      3. ``git rev-parse HEAD`` of the data-root directory if it's a git
         working tree. Useful when the data is vendored or generated
         from a versioned source.
      4. ``"unknown"`` (only when no window args were supplied and the
         git branch above failed too).

    The revision string is part of ``data_snapshot_id``, so changing it
    invalidates ledger identity. Callers that need stricter content
    addressability (e.g. CI parity tests across machines) should set
    ``LEAN_DATA_ROOT_REVISION`` explicitly.
    """
    explicit = os.environ.get("LEAN_DATA_ROOT_REVISION")
    if explicit:
        return explicit

    roots = _data_root_paths()
    if symbol is not None and start_date is not None and end_date is not None:
        fingerprint = compute_window_files_fingerprint(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            data_roots=roots,
        )
        if fingerprint != "files:none":
            return fingerprint

    for root in roots:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.0,
                cwd=str(root),
                check=False,
            )
            if proc.returncode == 0:
                sha = proc.stdout.strip()
                if sha:
                    return sha
            logger.debug(
                "[RUNS] data root %s: git rev-parse returned %d; "
                "falling back to per-file fingerprint",
                root,
                proc.returncode,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug(
                "[RUNS] data root %s: git unavailable (%s); falling back to per-file fingerprint",
                root,
                exc,
            )

    if symbol is not None and start_date is not None and end_date is not None:
        return "files:none"
    return "unknown"


class RunLedger(BaseModel):
    """Immutable identity record for a single strategy run.

    Persisted as ``ledger.json`` in the run's artifact directory.
    Pydantic ``extra='forbid'`` makes schema drift loud rather than
    silent — adding a field is a deliberate ``schema_version`` bump.

    Schema versions:
      * v1.0: Initial schema.
      * v1.1: Added optional ``prediction_set_hash`` field for ML prediction set tracking.
      * v1.2: Added optional ``window_summary`` field — calendar breakdown
        (sessions included / weekends + holidays excluded) for the run's
        date window. Older ledgers continue to load with this field as
        ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "1.1", "1.2"] = "1.2"
    run_id: str

    # Lineage — set on child runs (folds, MC simulations, sweep points).
    parent_run_id: str | None = None
    parent_spec_hash: str | None = None

    # Spec identity.
    strategy_spec_id: str  # caller-provided label or fixture name
    strategy_spec_hash: str  # sha256(canonical_json(spec))
    strategy_spec_json: dict  # verbatim spec snapshot

    # Engine identity.
    engine_name: Literal["learn_ai_event_driven"] = "learn_ai_event_driven"
    engine_version: str = ENGINE_VERSION
    engine_git_commit: str

    # Run config.
    symbol: str
    resolution_minutes: int = Field(ge=1)
    start_ms: int
    end_ms: int
    initial_cash: float = Field(ge=0.0)
    fill_mode: str  # "signal_bar_close" | "next_bar_open"
    commission_per_order: float = Field(ge=0.0)
    slippage_per_share: float = Field(default=0.0, ge=0.0)
    warmup_policy: Literal["spec_indicator_warmup"] = "spec_indicator_warmup"
    random_seed: int = 0

    # Data identity.
    data_source: Literal["lean_minute_reader"] = "lean_minute_reader"
    data_snapshot_id: str
    prediction_set_hash: str | None = None

    # Result identity (filled after the run completes).
    result_hash: str | None = None
    trade_log_hash: str | None = None
    metrics_hash: str | None = None

    # Lifecycle.
    created_at_ms: int = Field(default_factory=now_ms_utc)
    completed_at_ms: int | None = None
    status: Literal["running", "completed", "failed"] = "running"
    failure_reason: str | None = None

    # Calendar breakdown of [start_ms, end_ms) interpreted as NY-local
    # midnights. Optional so v1.0 / v1.1 ledgers still load with
    # ``window_summary=None``. New runs always populate it.
    window_summary: WindowSummary | None = None

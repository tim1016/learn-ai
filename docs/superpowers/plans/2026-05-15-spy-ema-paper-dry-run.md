# SPY EMA paper dry-run (B2) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship indicator-state persistence for `SpyEmaCrossoverAlgorithm` so a 09:30 ET `start` can resume from the prior session's indicator internals instead of burning ~3 h 45 m on warmup. Then run a full-RTH dry run on Tue 2026-05-19 with `decisions.parquet` populated from the first 15-min bar (09:45 ET), not from 13:15 ET.

**Architecture:** Generic JSON envelope + strategy-specific payload, stable global sidecar keyed by identity-tuple (`spy_ema_crossover/SPY_15m.json`), per-run hydration receipt that flows into the reconcile hash manifest. Hydrate policy is a tri-state (`require | optional | disabled`); validation is a six-check ladder gated by `pandas_market_calendars` NYSE previous-session equality. Writes fire at force-flat completion and at graceful-shutdown `finally`; the shutdown write uses a "newer" check to keep an early-Ctrl-C from clobbering force-flat's canonical sidecar.

**Tech Stack:** Python 3.11+, Pydantic v2, `pandas_market_calendars` (already in `requirements-light.txt`), `Decimal` everywhere for indicator math, `int64 ms UTC` for all timestamps on the wire. Pytest + `pytest-asyncio`. Ruff project-scope.

**Spec reference:** `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md`

**PR sequence:**
- **PR1** = tasks 1–12 on branch `feat/indicator-state-persistence-spy-ema` (already created off master; design doc already committed there as `90e4fd4`).
- **PR2** = task 13 on a separate branch `test/live-to-reconcile-producer`, cut from master **after PR1 merges**.
- Operator dry-run (Mon 2026-05-18 seed day, Tue 2026-05-19 B2 gate) happens after both PRs merge; the gate's deliverable is a receipt PR adding `docs/references/reconciliations/dry-run-2026-05-19/day-0.md`.

---

## File structure (PR1)

**Create:**
- `PythonDataService/app/engine/live/nyse_calendar.py` — `previous_completed_nyse_session_close_ms`
- `PythonDataService/app/engine/live/indicator_state.py` — `IndicatorStateEnvelope`, `IndicatorStatePayload`, `HydratePolicy`, `HydrationReceipt`, `ValidationResult`, `IndicatorStateRepo`, `IndicatorStateHydrationError`, top-level `hydrate` / `maybe_write` functions
- `PythonDataService/tests/engine/live/test_nyse_calendar.py`
- `PythonDataService/tests/engine/live/test_indicator_state_envelope.py`
- `PythonDataService/tests/engine/live/test_indicator_state_repo.py`
- `PythonDataService/tests/engine/live/test_spy_ema_persistence.py`
- `PythonDataService/tests/engine/live/test_live_context_hydrate.py`
- `PythonDataService/tests/engine/live/test_live_engine_checkpoint.py`

**Modify:**
- `PythonDataService/app/engine/indicators/base.py` — Indicator base gets `to_state_dict() -> dict` and `restore_state(state: dict) -> None` (default impls cover the common fields; subclasses override `_to_state_extra` / `_restore_state_extra` for their additional state)
- `PythonDataService/app/engine/indicators/sma.py` — `_to_state_extra` / `_restore_state_extra` for `_window` and `_sum`
- `PythonDataService/app/engine/indicators/ema.py` — `_to_state_extra` / `_restore_state_extra` for `_sma` (delegates to SMA)
- `PythonDataService/app/engine/indicators/rsi.py` — `_to_state_extra` / `_restore_state_extra` for the six RSI-specific fields
- `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py` — add `STRATEGY_KEY`, `CONSOLIDATOR_PERIOD_MIN`, `report_state_for_persistence`, `restore_state_from_persistence`, `validate_state_payload`
- `PythonDataService/app/engine/live/live_context.py` — ctor params `hydrate_policy`, `run_dir`; methods `hydrate_indicator_state`, `maybe_write_indicator_state`
- `PythonDataService/app/engine/live/live_engine.py` — three call sites: post-init hydrate, force-flat write, finally write; LiveContext ctor wiring
- `PythonDataService/app/engine/live/run.py` — `start --hydrate-policy` / `--allow-cold-start` flags; exit code 4
- `PythonDataService/app/engine/live/reconcile.py` — extend day-N hash manifest with `indicator_state_hydration.json` if present
- `PythonDataService/tests/indicators/test_ema.py` (or wherever EMA tests live) — bit-identical state round-trip test
- `PythonDataService/tests/indicators/test_rsi.py` — same
- `PythonDataService/tests/engine/live/test_reconcile.py` — add the hydration-receipt manifest entry test
- `PythonDataService/tests/engine/live/test_run_cli.py` — add `--hydrate-policy` flag tests, exit-code-4 path
- `docs/runbooks/ibkr-paper-dry-run.md` — Step 3 hydrate-policy subsection, Step 3a seed-day variant
- `docs/ibkr-integration-authority.md` — §6 surface table, §11 Phase 10 prereq flip, §12 operational checklist
- `PythonDataService/app/engine/live/README.md` — "Indicator state persistence" section

**File structure (PR2):**

**Create:**
- `PythonDataService/tests/engine/live/test_live_engine_to_reconcile_producer.py`

---

## Conventions referenced throughout

- **Decimal-safe JSON:** every `Decimal` field serializes as a quoted string; `None` stays `null`. On restore, `Decimal(str_value)` to round-trip exactly.
- **int64 ms UTC** on every timestamp at any boundary (per `.claude/rules/numerical-rigor.md`).
- **Tests run via** `podman exec polygon-data-service python -m pytest /app/tests/...`. Lint via `ruff check PythonDataService/app/ PythonDataService/tests/` **from the host** (per memory: ruff from host, not container).
- **Project-scope** lint and pytest before commits, not per-file.
- **Frequent commits** at the end of each task. Commit messages use the repo's existing prefix style (`feat(live):`, `test(live):`, `docs(authority):` etc.).

---

## Task 1: NYSE calendar helper

**Files:**
- Create: `PythonDataService/app/engine/live/nyse_calendar.py`
- Test: `PythonDataService/tests/engine/live/test_nyse_calendar.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/live/test_nyse_calendar.py`:

```python
"""Tests for previous_completed_nyse_session_close_ms.

The function is consumed only by indicator-state hydrate-validation
(see indicator_state.py); test it as a pure function so the validation
ladder's correctness rests on a deterministic primitive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.engine.live.nyse_calendar import (
    NoSessionError,
    previous_completed_nyse_session_close_ms,
)

_NY = ZoneInfo("America/New_York")


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """Local NY datetime -> int64 ms UTC."""
    return int(datetime(year, month, day, hour, minute, tzinfo=_NY).astimezone(UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    "case,session_start_ms,expected_prev_close_ms",
    [
        ("tue_after_normal_mon", _ms(2026, 5, 19, 9, 30), _ms(2026, 5, 18, 16, 0)),
        ("tue_after_memorial_day_mon", _ms(2026, 5, 26, 9, 30), _ms(2026, 5, 22, 16, 0)),
        ("mon_after_normal_fri", _ms(2026, 5, 18, 9, 30), _ms(2026, 5, 15, 16, 0)),
        # Thanksgiving 2026 = Thu Nov 26; Fri Nov 27 is early close at 13:00.
        ("fri_after_thanksgiving_thu", _ms(2026, 11, 27, 12, 0), _ms(2026, 11, 25, 16, 0)),
        # Day after Black Friday early-close.
        ("mon_after_early_close", _ms(2026, 11, 30, 9, 30), _ms(2026, 11, 27, 13, 0)),
        # Independence Day 2026 falls Saturday → observed Friday 7/3.
        ("mon_after_observed_independence", _ms(2026, 7, 6, 9, 30), _ms(2026, 7, 2, 16, 0)),
    ],
)
def test_previous_completed_session_close(case: str, session_start_ms: int, expected_prev_close_ms: int) -> None:
    actual = previous_completed_nyse_session_close_ms(session_start_ms)
    assert actual == expected_prev_close_ms, (
        f"{case}: expected {expected_prev_close_ms}, got {actual}"
    )


def test_weekend_session_start_raises_no_session_error() -> None:
    sat = _ms(2026, 5, 16, 9, 30)
    # The function asks for the previous SESSION close. A start_ms on
    # a non-session day is pathological but the function is still
    # expected to find the previous session's close — Friday's close.
    # We declare the contract: a session_start_ms before the first
    # session in our lookback raises NoSessionError. A Saturday with
    # 14-day lookback returns Friday close (not a raise).
    assert previous_completed_nyse_session_close_ms(sat) == _ms(2026, 5, 15, 16, 0)


def test_session_start_ms_before_any_lookback_session_raises() -> None:
    # 1970 — far before NYSE data; pandas_market_calendars covers it,
    # but the contract is: if no session exists in the lookback window
    # ending at session_start_ms, raise.
    with pytest.raises(NoSessionError):
        previous_completed_nyse_session_close_ms(0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_nyse_calendar.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.engine.live.nyse_calendar'`. That's a valid failure — module doesn't exist yet.

- [ ] **Step 3: Implement the helper**

Create `PythonDataService/app/engine/live/nyse_calendar.py`:

```python
"""NYSE previous-completed-session lookup, in int64 ms UTC.

Consumed only by indicator-state hydrate-validation (see
indicator_state.py check #3 in the ladder). Pure function; no IO; uses
pandas_market_calendars (already in requirements-light.txt) for the
authoritative NYSE schedule incl. early-close days and holidays.

Why ms UTC: per .claude/rules/numerical-rigor.md the canonical timestamp
format for any boundary is int64 ms UTC. Local timezone strings never
escape this function — input is UTC ms, output is UTC ms; the only NY
arithmetic happens inside pandas_market_calendars' tz-aware Timestamps.
"""

from __future__ import annotations

import pandas as pd
import pandas_market_calendars as mcal


class NoSessionError(LookupError):
    """No completed NYSE session exists in the lookback window."""


_LOOKBACK_DAYS = 14
_CALENDAR_NAME = "NYSE"


def previous_completed_nyse_session_close_ms(session_start_ms: int) -> int:
    """Return int64 ms UTC of the most recent NYSE session close strictly before session_start_ms.

    Honors early-close days (13:00 ET) and holidays. The previous
    session may be 1, 2, or 3+ calendar days back (weekend, holiday,
    holiday-after-weekend).

    Raises NoSessionError if no completed session exists in the
    14-day lookback window. (Pathological inputs only — the operator
    shouldn't be starting a runner with a session_start_ms with no
    trading history.)
    """
    cal = mcal.get_calendar(_CALENDAR_NAME)
    session_start_ts = pd.Timestamp(session_start_ms, unit="ms", tz="UTC")
    start = (session_start_ts - pd.Timedelta(days=_LOOKBACK_DAYS)).normalize()
    end = session_start_ts.normalize()
    schedule = cal.schedule(start_date=start, end_date=end)
    if schedule.empty:
        raise NoSessionError(
            f"no NYSE sessions in {_LOOKBACK_DAYS}-day lookback ending at {session_start_ts}"
        )
    # schedule['market_close'] is tz-aware UTC; filter strictly < start.
    earlier = schedule[schedule["market_close"] < session_start_ts]
    if earlier.empty:
        raise NoSessionError(
            f"no completed NYSE session strictly before {session_start_ts}"
        )
    last_close: pd.Timestamp = earlier["market_close"].iloc[-1]
    # pandas Timestamp -> int64 ms. .value is ns.
    return int(last_close.value // 1_000_000)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_nyse_calendar.py -v
```

Expected: all 8 tests pass (6 parameterized + 2 standalone).

- [ ] **Step 5: Ruff check the new files**

Run from host:
```
ruff check PythonDataService/app/engine/live/nyse_calendar.py PythonDataService/tests/engine/live/test_nyse_calendar.py
```

Expected: no issues.

- [ ] **Step 6: Commit**

```
git add PythonDataService/app/engine/live/nyse_calendar.py PythonDataService/tests/engine/live/test_nyse_calendar.py
git commit -m "$(cat <<'EOF'
feat(live): NYSE previous-completed-session helper for indicator-state staleness check

Pure function over pandas_market_calendars NYSE schedule. Consumed by
indicator-state hydrate-validation (check #3 in the six-row ladder).
Honors early-close days and holidays.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Indicator state Pydantic models + HydratePolicy enum

**Files:**
- Create: `PythonDataService/app/engine/live/indicator_state.py` (models only; repo logic comes in Task 3)
- Test: `PythonDataService/tests/engine/live/test_indicator_state_envelope.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/live/test_indicator_state_envelope.py`:

```python
"""Tests for IndicatorStateEnvelope, IndicatorStatePayload, HydratePolicy, HydrationReceipt."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.engine.live.indicator_state import (
    HydratePolicy,
    HydrationReceipt,
    IndicatorStateEnvelope,
    ValidationResult,
)


def _valid_envelope_dict() -> dict:
    return {
        "schema_version": 1,
        "strategy_key": "spy_ema_crossover",
        "symbol": "SPY",
        "consolidator_period_min": 15,
        "last_consolidated_bar_end_ms": 1747166100000,
        "captured_at_ms": 1747166107842,
        "captured_reason": "force_flat",
        "code_sha": "abc123",
        "strategy_spec_sha": "def456",
        "payload": {"ema5": {"is_ready": True, "samples": 18}},
    }


def test_envelope_round_trip_via_json() -> None:
    env = IndicatorStateEnvelope.model_validate(_valid_envelope_dict())
    serialized = env.model_dump_json()
    parsed_back = IndicatorStateEnvelope.model_validate_json(serialized)
    assert parsed_back == env


def test_envelope_rejects_schema_version_other_than_1() -> None:
    bad = _valid_envelope_dict()
    bad["schema_version"] = 2
    with pytest.raises(ValidationError):
        IndicatorStateEnvelope.model_validate(bad)


def test_envelope_rejects_unknown_captured_reason() -> None:
    bad = _valid_envelope_dict()
    bad["captured_reason"] = "periodic"  # not in Literal["force_flat", "shutdown"]
    with pytest.raises(ValidationError):
        IndicatorStateEnvelope.model_validate(bad)


def test_envelope_payload_is_pass_through_dict() -> None:
    env_dict = _valid_envelope_dict()
    env_dict["payload"] = {"arbitrary": "shape", "decimals_as_strings": "1.234"}
    env = IndicatorStateEnvelope.model_validate(env_dict)
    assert env.payload == {"arbitrary": "shape", "decimals_as_strings": "1.234"}


def test_hydrate_policy_values() -> None:
    # Verify the three values match CLI flag values exactly.
    assert HydratePolicy.REQUIRE.value == "require"
    assert HydratePolicy.OPTIONAL.value == "optional"
    assert HydratePolicy.DISABLED.value == "disabled"


def test_hydrate_policy_from_string() -> None:
    assert HydratePolicy("require") is HydratePolicy.REQUIRE


def test_hydration_receipt_serializes_accepted_true() -> None:
    receipt = HydrationReceipt(
        schema_version=1,
        hydrated_at_ms=1747641007500,
        policy=HydratePolicy.REQUIRE,
        global_path="PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json",
        global_sha256="abc",
        accepted=True,
        strategy_key="spy_ema_crossover",
        symbol="SPY",
        consolidator_period_min=15,
        sidecar_last_consolidated_bar_end_ms=1747166100000,
        expected_prev_session_close_ms=1747166100000,
        calendar="NYSE",
        validation=ValidationResult.all_passed(),
    )
    j = json.loads(receipt.model_dump_json())
    assert j["accepted"] is True
    assert j["validation"]["failure_reason"] is None
    assert j["policy"] == "require"


def test_validation_result_all_passed_factory() -> None:
    vr = ValidationResult.all_passed()
    assert vr.schema_version_ok and vr.identity_ok and vr.calendar_ok
    assert vr.payload_shape_ok and vr.indicators_ready_ok and vr.lifecycle_flat_ok
    assert vr.failure_reason is None


def test_validation_result_failure_factory() -> None:
    vr = ValidationResult.failed("calendar_stale", calendar_ok=False)
    assert vr.failure_reason == "calendar_stale"
    assert vr.calendar_ok is False
    # Checks that did not fail remain True (or whatever the caller passed).
    assert vr.schema_version_ok is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_indicator_state_envelope.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.engine.live.indicator_state'`.

- [ ] **Step 3: Implement the models**

Create `PythonDataService/app/engine/live/indicator_state.py`:

```python
"""Indicator state persistence — envelope, payload, policy, receipt, validation.

Generic envelope, strategy-specific payload. The envelope's identity
fields (strategy_key, symbol, consolidator_period_min) double as the
sidecar's lookup key on the filesystem.

The validation ladder (consumed by hydrate(), implemented in this
module) runs six checks in order; first failure stops and populates
``ValidationResult.failure_reason``. See
``docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md``
§4.1 for the ladder definition.

Decimal-safe: indicator internal numeric state serializes as quoted
strings in the payload (passed through as ``dict[str, Any]`` from this
module's POV; per-strategy ``validate_state_payload`` enforces the
shape). int64 ms UTC for every timestamp at the wire boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HydratePolicy(str, Enum):
    """Tri-state policy for indicator-state hydration on start.

    Default for the B2 dry-run gate and paper-week operation is REQUIRE.
    OPTIONAL is for seed days. DISABLED is the explicit operator escape
    hatch (--allow-cold-start) that skips the read entirely but still
    writes at end-of-session so today seeds tomorrow.
    """

    REQUIRE = "require"
    OPTIONAL = "optional"
    DISABLED = "disabled"


FailureReason = Literal[
    "disabled_by_operator",
    "missing",
    "schema_mismatch",
    "identity_mismatch",
    "calendar_stale",
    "payload_mismatch",
    "indicators_unready",
    "lifecycle_not_flat",
]


class ValidationResult(BaseModel):
    """Per-check booleans + the first failure reason."""

    model_config = ConfigDict(frozen=True)

    schema_version_ok: bool = True
    identity_ok: bool = True
    calendar_ok: bool = True
    payload_shape_ok: bool = True
    indicators_ready_ok: bool = True
    lifecycle_flat_ok: bool = True
    failure_reason: FailureReason | None = None

    @classmethod
    def all_passed(cls) -> ValidationResult:
        return cls()

    @classmethod
    def failed(cls, reason: FailureReason, **flag_overrides: bool) -> ValidationResult:
        return cls(failure_reason=reason, **flag_overrides)


CapturedReason = Literal["force_flat", "shutdown"]


class IndicatorStateEnvelope(BaseModel):
    """Generic envelope wrapping a strategy-specific payload.

    Identity tuple = (strategy_key, symbol, consolidator_period_min).
    Used both as the sidecar's filesystem key and as the validation
    ladder's identity check (#2).

    Timestamps are int64 ms UTC. Payload is opaque to this module —
    each strategy is responsible for its own payload shape via
    ``validate_state_payload``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_key: str
    symbol: str
    consolidator_period_min: int = Field(gt=0)
    last_consolidated_bar_end_ms: int = Field(gt=0)
    captured_at_ms: int = Field(gt=0)
    captured_reason: CapturedReason
    code_sha: str
    strategy_spec_sha: str
    payload: dict[str, Any]


class HydrationReceipt(BaseModel):
    """Per-run forensic record of what happened at hydrate time.

    Always written, regardless of accepted=true/false. The reconcile
    hash manifest picks it up if present.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    hydrated_at_ms: int
    policy: HydratePolicy
    global_path: str
    global_sha256: str | None
    accepted: bool
    strategy_key: str
    symbol: str
    consolidator_period_min: int
    sidecar_last_consolidated_bar_end_ms: int | None = None
    expected_prev_session_close_ms: int | None = None
    calendar: str = "NYSE"
    validation: ValidationResult


class IndicatorStateHydrationError(RuntimeError):
    """Raised when hydrate() is called under REQUIRE policy and validation fails.

    Carries the receipt the runner just wrote so callers can surface
    the failure reason without re-reading the file.
    """

    def __init__(self, receipt: HydrationReceipt) -> None:
        self.receipt = receipt
        super().__init__(
            f"indicator state hydration failed under {receipt.policy.value} policy: "
            f"{receipt.validation.failure_reason}"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_indicator_state_envelope.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5: Ruff check**

Run from host:
```
ruff check PythonDataService/app/engine/live/indicator_state.py PythonDataService/tests/engine/live/test_indicator_state_envelope.py
```

Expected: no issues.

- [ ] **Step 6: Commit**

```
git add PythonDataService/app/engine/live/indicator_state.py PythonDataService/tests/engine/live/test_indicator_state_envelope.py
git commit -m "$(cat <<'EOF'
feat(live): indicator-state Pydantic models + HydratePolicy tri-state

Generic envelope (strategy_key, symbol, period, last_bar_end_ms, captured_at,
captured_reason, code_sha, strategy_spec_sha, payload) wrapping a strategy-
specific payload dict. HydratePolicy={REQUIRE, OPTIONAL, DISABLED} matches
CLI flag values. ValidationResult records the six-check ladder outcome;
HydrationReceipt is the per-run forensic record. Repo + ladder come next.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: IndicatorStateRepo — atomic write, advisory lock, "newer" check

**Files:**
- Modify: `PythonDataService/app/engine/live/indicator_state.py` (add `IndicatorStateRepo` class)
- Test: `PythonDataService/tests/engine/live/test_indicator_state_repo.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/live/test_indicator_state_repo.py`:

```python
"""Tests for IndicatorStateRepo — atomic write, advisory lock, newer-check."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.indicator_state import (
    IndicatorStateEnvelope,
    IndicatorStateRepo,
)


def _make_envelope(last_bar_ms: int, captured_at_ms: int = 1_700_000_000_000) -> IndicatorStateEnvelope:
    return IndicatorStateEnvelope(
        schema_version=1,
        strategy_key="spy_ema_crossover",
        symbol="SPY",
        consolidator_period_min=15,
        last_consolidated_bar_end_ms=last_bar_ms,
        captured_at_ms=captured_at_ms,
        captured_reason="force_flat",
        code_sha="abc",
        strategy_spec_sha="def",
        payload={"ema5": {"is_ready": True}},
    )


def test_read_missing_returns_none(tmp_path: Path) -> None:
    repo = IndicatorStateRepo(tmp_path / "missing.json")
    assert repo.read() is None


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    repo = IndicatorStateRepo(tmp_path / "state.json")
    env = _make_envelope(last_bar_ms=1_700_000_000_000)
    repo.write(env)
    loaded = repo.read()
    assert loaded == env


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    deep_path = tmp_path / "nested" / "dir" / "state.json"
    repo = IndicatorStateRepo(deep_path)
    env = _make_envelope(last_bar_ms=1_700_000_000_000)
    repo.write(env)
    assert deep_path.exists()


def test_corrupt_json_read_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json")
    repo = IndicatorStateRepo(path)
    # Per spec: corrupt JSON is one of the failure modes the validation
    # ladder catches. Repo.read raises; callers convert to receipt.
    with pytest.raises(Exception):
        repo.read()


def test_is_newer_than_existing_true_when_no_existing(tmp_path: Path) -> None:
    repo = IndicatorStateRepo(tmp_path / "missing.json")
    new = _make_envelope(last_bar_ms=1_700_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(new) is True


def test_is_newer_than_existing_false_when_equal(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    base = _make_envelope(last_bar_ms=1_700_000_000_000)
    repo.write(base)
    same_bar = _make_envelope(last_bar_ms=1_700_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(same_bar) is False


def test_is_newer_than_existing_false_when_older(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    older = _make_envelope(last_bar_ms=1_500_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(older) is False


def test_is_newer_than_existing_true_when_strictly_newer(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    newer = _make_envelope(last_bar_ms=1_800_000_000_000)
    assert repo.is_strictly_newer_than_on_disk(newer) is True


def test_sha256_of_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    assert repo.sha256_of_on_disk() is None  # missing file
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    sha = repo.sha256_of_on_disk()
    assert sha is not None and len(sha) == 64  # hex sha-256


def test_atomic_write_does_not_leak_tmp_on_success(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    repo = IndicatorStateRepo(path)
    repo.write(_make_envelope(last_bar_ms=1_700_000_000_000))
    # The .tmp file should not survive a successful write.
    tmp_siblings = list(tmp_path.glob("*.tmp*"))
    assert tmp_siblings == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_indicator_state_repo.py -v
```

Expected: `ImportError: cannot import name 'IndicatorStateRepo' ...` — class not defined yet.

- [ ] **Step 3: Implement IndicatorStateRepo**

Add to the bottom of `PythonDataService/app/engine/live/indicator_state.py` (before any module-level definitions that would reference it):

```python
import hashlib
import json
import os
import sys
from pathlib import Path


class IndicatorStateRepo:
    """Atomic-write JSON repo for a single envelope on a stable path.

    Path identity = (strategy_key, symbol, consolidator_period_min);
    callers construct the path themselves from the identity tuple
    (see ``stable_global_path``).

    Atomic write: serialize -> write to <path>.tmp -> os.replace.
    On POSIX this is atomic for the rename; on Windows os.replace
    handles the existing-file case too.

    Advisory lock: best-effort fcntl on POSIX, msvcrt on Windows.
    The lock window is the duration of the atomic write only.
    Concurrent readers may see either the old or the new file but
    never a torn one. (The runner is a single process; the lock
    guards against developer footguns like two CLI invocations
    racing on the same machine.)
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> IndicatorStateEnvelope | None:
        """Return the envelope, or None if the file does not exist.

        Raises on malformed JSON or schema violations — the validation
        ladder converts that into a ``schema_mismatch`` receipt.
        """
        if not self._path.exists():
            return None
        with self._path.open("r", encoding="utf-8") as fh:
            return IndicatorStateEnvelope.model_validate_json(fh.read())

    def write(self, envelope: IndicatorStateEnvelope) -> None:
        """Atomic write of envelope to ``self._path`` under advisory lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload_json = envelope.model_dump_json(indent=2).encode("utf-8")
        with _file_lock(self._path):
            with open(tmp_path, "wb") as fh:
                fh.write(payload_json)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)

    def is_strictly_newer_than_on_disk(self, candidate: IndicatorStateEnvelope) -> bool:
        """Return True iff there is no existing sidecar or candidate's bar is strictly newer.

        Used by shutdown-checkpoint write to refuse overwriting force-flat's
        canonical sidecar with an earlier-Ctrl-C state.
        """
        if not self._path.exists():
            return True
        existing = self.read()
        if existing is None:
            return True
        return candidate.last_consolidated_bar_end_ms > existing.last_consolidated_bar_end_ms

    def sha256_of_on_disk(self) -> str | None:
        """Return SHA-256 hex of the on-disk bytes, or None if absent."""
        if not self._path.exists():
            return None
        h = hashlib.sha256()
        with self._path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


def stable_global_path(
    artifacts_root: Path,
    strategy_key: str,
    symbol: str,
    consolidator_period_min: int,
) -> Path:
    """Return the canonical sidecar path for an identity tuple.

    Layout: <artifacts_root>/live_state/<strategy_key>/<symbol>_<period>m.json
    """
    return (
        artifacts_root
        / "live_state"
        / strategy_key
        / f"{symbol.upper()}_{consolidator_period_min}m.json"
    )


# ---------------------------------------------------------------------------
# Advisory file lock — cross-platform (fcntl on POSIX, msvcrt on Windows).
# ---------------------------------------------------------------------------

import contextlib


@contextlib.contextmanager
def _file_lock(target_path: Path):
    """Acquire an advisory lock on a sibling .lock file for the lifetime of the context.

    Best-effort. Failure to acquire (lock-file directory denied,
    Windows lock-file already open from another process) raises so
    the caller surfaces it rather than silently writing without
    synchronization.
    """
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open the lockfile for writing — created if missing.
    fh = open(lock_path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            # Lock the entire file (LK_LOCK blocks; LK_NBLCK raises on contention).
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
```

(Note: the `import hashlib`, `import json`, `import os`, `import sys`, `from pathlib import Path`, and `import contextlib` lines should be moved up to the top of the file to keep imports grouped — but the diff above shows them inline for clarity. Move them during implementation; ruff will complain otherwise.)

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_indicator_state_repo.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 5: Ruff check**

Run from host:
```
ruff check PythonDataService/app/engine/live/indicator_state.py PythonDataService/tests/engine/live/test_indicator_state_repo.py
```

Expected: no issues (imports grouped at top of file).

- [ ] **Step 6: Commit**

```
git add PythonDataService/app/engine/live/indicator_state.py PythonDataService/tests/engine/live/test_indicator_state_repo.py
git commit -m "$(cat <<'EOF'
feat(live): IndicatorStateRepo — atomic write, advisory lock, newer-check

Cross-platform advisory lock (fcntl on POSIX, msvcrt on Windows). Atomic
write via tmp+os.replace with fsync. Strictly-newer comparison on
last_consolidated_bar_end_ms is what makes early-Ctrl-C-before-force-flat
safe — a partial-day state cannot clobber force-flat's canonical sidecar.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Indicator base class — `to_state_dict` / `restore_state`

**Files:**
- Modify: `PythonDataService/app/engine/indicators/base.py`
- Test: `PythonDataService/tests/indicators/test_indicator_base_persistence.py` (new file; the existing base-class tests focus on update semantics)

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/indicators/test_indicator_base_persistence.py`:

```python
"""Tests for Indicator.to_state_dict / restore_state on the base class.

Subclass round-trip + bit-identical-output tests come in Tasks 5–7;
this task pins the base contract: common fields persist; subclass
extras are an extension point via _to_state_extra/_restore_state_extra.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.engine.indicators.base import Indicator


class _CountingIndicator(Indicator):
    """Minimal subclass for testing: records last value as the indicator value."""

    def _compute_next_value(self, time: datetime, value: Decimal) -> Decimal | None:
        return value


def test_to_state_dict_includes_common_fields() -> None:
    ind = _CountingIndicator("X", 3)
    ind.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    ind.update(datetime(2026, 5, 18, 14, 15, tzinfo=UTC), Decimal("101"))
    state = ind.to_state_dict()
    assert state["name"] == "X"
    assert state["period"] == 3
    assert state["samples"] == 2
    assert state["current_value"] == "101"
    assert state["current_time_ms"] == int(datetime(2026, 5, 18, 14, 15, tzinfo=UTC).timestamp() * 1000)
    assert state["previous_value"] == "100"
    assert state["previous_time_ms"] == int(datetime(2026, 5, 18, 14, 0, tzinfo=UTC).timestamp() * 1000)


def test_restore_state_round_trip() -> None:
    src = _CountingIndicator("X", 3)
    src.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    src.update(datetime(2026, 5, 18, 14, 15, tzinfo=UTC), Decimal("101"))
    state = src.to_state_dict()

    dst = _CountingIndicator("X", 3)
    dst.restore_state(state)
    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    assert dst.current_time == src.current_time
    assert dst.previous_value == src.previous_value
    assert dst.previous_time == src.previous_time


def test_restore_state_rejects_name_mismatch() -> None:
    src = _CountingIndicator("X", 3)
    src.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100"))
    state = src.to_state_dict()

    dst = _CountingIndicator("Y", 3)
    import pytest

    with pytest.raises(ValueError, match="name mismatch"):
        dst.restore_state(state)


def test_restore_state_rejects_period_mismatch() -> None:
    src = _CountingIndicator("X", 3)
    state = src.to_state_dict()
    state["samples"] = 0  # fresh; just probing the period check

    dst = _CountingIndicator("X", 5)
    import pytest

    with pytest.raises(ValueError, match="period mismatch"):
        dst.restore_state(state)


def test_to_state_dict_decimals_are_strings() -> None:
    ind = _CountingIndicator("X", 3)
    ind.update(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), Decimal("100.123456789012345"))
    state = ind.to_state_dict()
    # Quoted-string preserves Decimal precision exactly.
    assert isinstance(state["current_value"], str)
    assert Decimal(state["current_value"]) == Decimal("100.123456789012345")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_indicator_base_persistence.py -v
```

Expected: `AttributeError: 'Indicator' object has no attribute 'to_state_dict'`.

- [ ] **Step 3: Add `to_state_dict` / `restore_state` to the Indicator base**

Modify `PythonDataService/app/engine/indicators/base.py` — add to the `Indicator` class (and `BarIndicator` class with the same body — both classes mirror the same fields).

Add these methods to `Indicator` (after `_reset_state`):

```python
    def to_state_dict(self) -> dict:
        """Serialize the indicator's persistable state to a JSON-safe dict.

        Common fields are produced by this base method; subclasses with
        additional state override ``_to_state_extra`` to merge their
        own keys. Decimals serialize as quoted strings; timestamps as
        int64 ms UTC.
        """
        return {
            "name": self.name,
            "period": self.period,
            "samples": self.samples,
            "current_value": _decimal_to_str(self._current_value),
            "current_time_ms": _datetime_to_ms(self._current_time),
            "previous_value": _decimal_to_str(self._previous_value),
            "previous_time_ms": _datetime_to_ms(self._previous_time),
            **self._to_state_extra(),
        }

    def restore_state(self, state: dict) -> None:
        """Restore from a dict produced by ``to_state_dict``.

        Raises ``ValueError`` on identity mismatch (different name or
        period). Subclasses override ``_restore_state_extra`` to
        consume their own keys.
        """
        if state["name"] != self.name:
            raise ValueError(f"name mismatch: state={state['name']!r} self={self.name!r}")
        if state["period"] != self.period:
            raise ValueError(f"period mismatch: state={state['period']} self={self.period}")
        self.samples = int(state["samples"])
        self._current_value = _str_to_decimal(state["current_value"])
        self._current_time = _ms_to_datetime(state["current_time_ms"])
        self._previous_value = _str_to_decimal(state["previous_value"])
        self._previous_time = _ms_to_datetime(state["previous_time_ms"])
        self._restore_state_extra(state)

    def _to_state_extra(self) -> dict:
        """Override in subclasses to add subclass-specific fields."""
        return {}

    def _restore_state_extra(self, state: dict) -> None:
        """Override in subclasses to consume subclass-specific fields."""
```

Add the same methods to `BarIndicator`. Their bodies are identical (the field set is the same).

Add these helpers at the bottom of the file (module-private):

```python
from datetime import UTC


def _decimal_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _str_to_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _datetime_to_ms(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp() * 1000)


def _ms_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, tz=UTC)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_indicator_base_persistence.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Run the existing indicator tests as a regression check**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/ -v
```

Expected: every prior indicator test still passes.

- [ ] **Step 6: Ruff check**

```
ruff check PythonDataService/app/engine/indicators/base.py PythonDataService/tests/indicators/test_indicator_base_persistence.py
```

- [ ] **Step 7: Commit**

```
git add PythonDataService/app/engine/indicators/base.py PythonDataService/tests/indicators/test_indicator_base_persistence.py
git commit -m "$(cat <<'EOF'
feat(indicators): Indicator base — to_state_dict / restore_state with subclass extras

Common fields (name, period, samples, current_value, current_time,
previous_value, previous_time) handled by the base; subclasses override
_to_state_extra / _restore_state_extra for their own state. Decimals
serialize as quoted strings (round-trip exact); timestamps as int64 ms UTC
per the boundary rule in .claude/rules/numerical-rigor.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: SMA, EMA, RSI subclass persistence

**Files:**
- Modify: `PythonDataService/app/engine/indicators/sma.py`, `ema.py`, `rsi.py`
- Test: `PythonDataService/tests/indicators/test_sma_persistence.py`, `test_ema_persistence.py`, `test_rsi_persistence.py` (new files)

- [ ] **Step 1: Write the failing tests for SMA**

Create `PythonDataService/tests/indicators/test_sma_persistence.py`:

```python
"""SMA persistence — round-trip + bit-identical outputs on next bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.indicators.sma import SimpleMovingAverage


def _feed(ind: SimpleMovingAverage, values: list[Decimal], t0: datetime) -> None:
    for i, v in enumerate(values):
        ind.update(t0 + timedelta(minutes=15 * i), v)


def test_round_trip_through_state_dict() -> None:
    src = SimpleMovingAverage("S", 3)
    _feed(src, [Decimal(x) for x in ("100", "101", "102", "103")], datetime(2026, 5, 18, 14, 0, tzinfo=UTC))
    state = src.to_state_dict()

    dst = SimpleMovingAverage("S", 3)
    dst.restore_state(state)
    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    # Internals match: deque contents and sum.
    assert list(dst._window) == list(src._window)
    assert dst._sum == src._sum


def test_bit_identical_outputs_after_restore() -> None:
    """The load-bearing property: a restored SMA + the next bar produces
    the exact same value as a freshly-warmed SMA + the same bar."""
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    warmup = [Decimal(x) for x in ("100", "101", "102", "103")]

    src = SimpleMovingAverage("S", 3)
    _feed(src, warmup, t0)
    state = src.to_state_dict()

    # Path A: continue the original.
    extra_bar_time = t0 + timedelta(minutes=15 * 4)
    src.update(extra_bar_time, Decimal("104"))
    expected = src.current_value

    # Path B: restore a fresh instance and feed the same extra bar.
    dst = SimpleMovingAverage("S", 3)
    dst.restore_state(state)
    dst.update(extra_bar_time, Decimal("104"))
    actual = dst.current_value

    assert actual == expected  # Decimal equality — atol=0
```

- [ ] **Step 2: Run to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_sma_persistence.py -v
```

Expected: round-trip fails — `_window` and `_sum` aren't restored.

- [ ] **Step 3: Add `_to_state_extra` / `_restore_state_extra` to SMA**

Modify `PythonDataService/app/engine/indicators/sma.py` — add to the `SimpleMovingAverage` class (after `_reset_state`):

```python
    def _to_state_extra(self) -> dict:
        return {
            "window": [str(v) for v in self._window],
            "sum": str(self._sum),
        }

    def _restore_state_extra(self, state: dict) -> None:
        from collections import deque

        self._window = deque(
            (Decimal(v) for v in state["window"]),
            maxlen=self.period,
        )
        self._sum = Decimal(state["sum"])
```

- [ ] **Step 4: Run to verify pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_sma_persistence.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Repeat for EMA — write failing test**

Create `PythonDataService/tests/indicators/test_ema_persistence.py`:

```python
"""EMA persistence — round-trip + bit-identical outputs on next bars.

EMA carries an internal SMA used to seed the EMA during warmup. The
SMA's state must also persist for the round-trip to be exact through
the warmup-then-recursion transition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.indicators.ema import ExponentialMovingAverage


def _feed(ind: ExponentialMovingAverage, values: list[Decimal], t0: datetime) -> None:
    for i, v in enumerate(values):
        ind.update(t0 + timedelta(minutes=15 * i), v)


def test_round_trip_post_warmup() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    # Period 5; feed 8 bars so EMA is in the recursive phase.
    warmup = [Decimal(x) for x in ("100", "101", "102", "103", "104", "105", "106", "107")]
    src = ExponentialMovingAverage("EMA5", 5)
    _feed(src, warmup, t0)
    assert src.is_ready

    state = src.to_state_dict()
    dst = ExponentialMovingAverage("EMA5", 5)
    dst.restore_state(state)

    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    # SMA internals also restored.
    assert dst._sma.current_value == src._sma.current_value
    assert list(dst._sma._window) == list(src._sma._window)


def test_bit_identical_outputs_after_restore_post_warmup() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    warmup = [Decimal(x) for x in ("100", "101", "102", "103", "104", "105", "106", "107")]
    src = ExponentialMovingAverage("EMA5", 5)
    _feed(src, warmup, t0)
    state = src.to_state_dict()

    next_t = t0 + timedelta(minutes=15 * 8)
    src.update(next_t, Decimal("108"))
    expected = src.current_value

    dst = ExponentialMovingAverage("EMA5", 5)
    dst.restore_state(state)
    dst.update(next_t, Decimal("108"))

    assert dst.current_value == expected


def test_bit_identical_for_five_more_bars_after_restore() -> None:
    """Stronger property: equivalence persists through several iterations,
    not just the next bar. This is the load-bearing claim of warm-start
    equivalence."""
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    warmup = [Decimal(x) for x in ("100", "101", "102", "103", "104", "105", "106", "107")]
    src = ExponentialMovingAverage("EMA5", 5)
    _feed(src, warmup, t0)
    state = src.to_state_dict()

    dst = ExponentialMovingAverage("EMA5", 5)
    dst.restore_state(state)

    for i in range(5):
        t = t0 + timedelta(minutes=15 * (8 + i))
        v = Decimal(108 + i)
        src.update(t, v)
        dst.update(t, v)
        assert dst.current_value == src.current_value, f"bar {i}: {dst.current_value} != {src.current_value}"
```

- [ ] **Step 6: Run to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_ema_persistence.py -v
```

Expected: `dst._sma.current_value` differs from `src._sma.current_value` — SMA seed isn't persisted yet at the EMA level.

- [ ] **Step 7: Add `_to_state_extra` / `_restore_state_extra` to EMA**

Modify `PythonDataService/app/engine/indicators/ema.py` — add to the `ExponentialMovingAverage` class (after `_reset_state`):

```python
    def _to_state_extra(self) -> dict:
        # The k and one_minus_k constants are derived from `period` in
        # __init__; no need to persist them. Only the SMA seed state
        # contains live data.
        return {
            "sma_state": self._sma.to_state_dict(),
        }

    def _restore_state_extra(self, state: dict) -> None:
        self._sma.restore_state(state["sma_state"])
```

- [ ] **Step 8: Run to verify pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_ema_persistence.py -v
```

Expected: 3 tests pass.

- [ ] **Step 9: Repeat for RSI — write failing test**

Create `PythonDataService/tests/indicators/test_rsi_persistence.py`:

```python
"""RSI persistence — round-trip + bit-identical outputs on next bars.

RSI carries six extra fields beyond the base: _prev_input, _avg_gain,
_avg_loss, _gain_sum, _loss_sum, _delta_samples.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.indicators.rsi import RelativeStrengthIndex


def _feed(ind: RelativeStrengthIndex, values: list[Decimal], t0: datetime) -> None:
    for i, v in enumerate(values):
        ind.update(t0 + timedelta(minutes=15 * i), v)


def test_round_trip_post_warmup() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    # Period 14 needs 15 samples to be ready; feed 20.
    closes = [Decimal(100 + i) for i in range(20)]
    src = RelativeStrengthIndex("RSI14", 14)
    _feed(src, closes, t0)
    assert src.is_ready

    state = src.to_state_dict()
    dst = RelativeStrengthIndex("RSI14", 14)
    dst.restore_state(state)

    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    assert dst._prev_input == src._prev_input
    assert dst._avg_gain == src._avg_gain
    assert dst._avg_loss == src._avg_loss
    assert dst._delta_samples == src._delta_samples


def test_bit_identical_outputs_for_five_more_bars_after_restore() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    closes = [Decimal(100 + i) for i in range(20)]
    src = RelativeStrengthIndex("RSI14", 14)
    _feed(src, closes, t0)
    state = src.to_state_dict()

    dst = RelativeStrengthIndex("RSI14", 14)
    dst.restore_state(state)

    # Mix of up and down moves to exercise both avg_gain and avg_loss branches.
    next_values = [Decimal("125"), Decimal("123"), Decimal("128"), Decimal("130"), Decimal("129")]
    for i, v in enumerate(next_values):
        t = t0 + timedelta(minutes=15 * (20 + i))
        src.update(t, v)
        dst.update(t, v)
        assert dst.current_value == src.current_value, f"bar {i}: {dst.current_value} != {src.current_value}"
```

- [ ] **Step 10: Run to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_rsi_persistence.py -v
```

Expected: round-trip fails — RSI's six extra fields aren't persisted.

- [ ] **Step 11: Add `_to_state_extra` / `_restore_state_extra` to RSI**

Modify `PythonDataService/app/engine/indicators/rsi.py` — add to the `RelativeStrengthIndex` class (after `_reset_state`):

```python
    def _to_state_extra(self) -> dict:
        return {
            "prev_input": None if self._prev_input is None else str(self._prev_input),
            "avg_gain": None if self._avg_gain is None else str(self._avg_gain),
            "avg_loss": None if self._avg_loss is None else str(self._avg_loss),
            "gain_sum": str(self._gain_sum),
            "loss_sum": str(self._loss_sum),
            "delta_samples": self._delta_samples,
        }

    def _restore_state_extra(self, state: dict) -> None:
        self._prev_input = None if state["prev_input"] is None else Decimal(state["prev_input"])
        self._avg_gain = None if state["avg_gain"] is None else Decimal(state["avg_gain"])
        self._avg_loss = None if state["avg_loss"] is None else Decimal(state["avg_loss"])
        self._gain_sum = Decimal(state["gain_sum"])
        self._loss_sum = Decimal(state["loss_sum"])
        self._delta_samples = int(state["delta_samples"])
```

- [ ] **Step 12: Run to verify pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/test_rsi_persistence.py /app/tests/indicators/test_ema_persistence.py /app/tests/indicators/test_sma_persistence.py -v
```

Expected: all SMA/EMA/RSI persistence tests pass.

- [ ] **Step 13: Run full indicator test suite as regression check**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/indicators/ -v
```

Expected: every prior indicator test still passes.

- [ ] **Step 14: Ruff check**

```
ruff check PythonDataService/app/engine/indicators/ PythonDataService/tests/indicators/test_sma_persistence.py PythonDataService/tests/indicators/test_ema_persistence.py PythonDataService/tests/indicators/test_rsi_persistence.py
```

- [ ] **Step 15: Commit**

```
git add PythonDataService/app/engine/indicators/ PythonDataService/tests/indicators/test_sma_persistence.py PythonDataService/tests/indicators/test_ema_persistence.py PythonDataService/tests/indicators/test_rsi_persistence.py
git commit -m "$(cat <<'EOF'
feat(indicators): SMA/EMA/RSI _to_state_extra and _restore_state_extra

SMA persists its deque window and running sum; EMA delegates to its
internal SMA seed via to_state_dict; RSI persists prev_input, both
Wilders averages, the warmup accumulators, and delta_samples. Each
ships with a bit-identical-outputs test that proves a restored
indicator emits the exact same values on the next 5 bars as a
freshly-warmed indicator on the same data.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `SpyEmaCrossoverAlgorithm` persistence hooks

**Files:**
- Modify: `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py`
- Test: `PythonDataService/tests/engine/live/test_spy_ema_persistence.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/live/test_spy_ema_persistence.py`:

```python
"""Tests for SpyEmaCrossoverAlgorithm persistence hooks.

The strategy exposes three methods consumed by LiveContext:
  - report_state_for_persistence() -> IndicatorStatePayload | None
  - restore_state_from_persistence(payload) -> None
  - validate_state_payload(payload) -> ValidationResult

PR1 contract: report returns None unless indicators are ready AND
position is flat AND no pending orders AND no open insights.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm


def _build_warmed_strategy() -> SpyEmaCrossoverAlgorithm:
    """Construct a strategy with indicators forced past warmup, flat lifecycle."""
    strat = SpyEmaCrossoverAlgorithm()
    # Stand-alone construction (no LiveEngine.run) — drive initialize()
    # manually with a minimal fake context.
    from unittest.mock import MagicMock

    strat.ctx = MagicMock()
    strat.ctx.add_equity.return_value = "SPY"
    strat.initialize()

    # Drive enough closes through the indicators to make them ready.
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    for i in range(20):
        bar_time = t0 + timedelta(minutes=15 * i)
        close = Decimal(400 + i)
        strat._ema5.update(bar_time, close)
        strat._ema10.update(bar_time, close)
        strat._rsi14.update(bar_time, close)
    return strat


def test_report_state_returns_none_when_indicators_not_ready() -> None:
    strat = SpyEmaCrossoverAlgorithm()
    from unittest.mock import MagicMock
    strat.ctx = MagicMock()
    strat.ctx.add_equity.return_value = "SPY"
    strat.initialize()
    # No updates -> indicators not ready.
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_none_when_in_position() -> None:
    strat = _build_warmed_strategy()
    strat._in_position = True
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_none_when_pending_entry() -> None:
    strat = _build_warmed_strategy()
    from app.engine.strategy.algorithms.spy_ema_crossover import _PendingEntry

    strat._pending_entry = _PendingEntry(ema5=Decimal("400"), ema10=Decimal("399"), rsi=Decimal("60"))
    assert strat.report_state_for_persistence() is None


def test_report_state_returns_payload_when_flat_and_ready() -> None:
    strat = _build_warmed_strategy()
    payload = strat.report_state_for_persistence()
    assert payload is not None
    assert "ema5" in payload
    assert "ema10" in payload
    assert "rsi14" in payload
    assert "_prev_ema5_above_ema10" in payload
    assert "lifecycle" in payload
    assert payload["lifecycle"]["position_qty"] == 0
    assert payload["lifecycle"]["pending_orders_count"] == 0


def test_restore_state_round_trip_produces_bit_identical_next_value() -> None:
    """After hydrate, the next consolidated bar produces the same indicator
    values as if the strategy had run continuously."""
    src = _build_warmed_strategy()
    payload = src.report_state_for_persistence()
    assert payload is not None

    # Path A: continue src directly.
    next_time = datetime(2026, 5, 18, 14, 0, tzinfo=UTC) + timedelta(minutes=15 * 20)
    next_close = Decimal("420")
    src._ema5.update(next_time, next_close)
    src._ema10.update(next_time, next_close)
    src._rsi14.update(next_time, next_close)
    expected = (src._ema5.current_value, src._ema10.current_value, src._rsi14.current_value)

    # Path B: fresh strategy + restore + feed the same bar.
    dst = SpyEmaCrossoverAlgorithm()
    from unittest.mock import MagicMock
    dst.ctx = MagicMock()
    dst.ctx.add_equity.return_value = "SPY"
    dst.initialize()
    dst.restore_state_from_persistence(payload)
    dst._ema5.update(next_time, next_close)
    dst._ema10.update(next_time, next_close)
    dst._rsi14.update(next_time, next_close)
    actual = (dst._ema5.current_value, dst._ema10.current_value, dst._rsi14.current_value)

    assert actual == expected, f"bit-identical equivalence broken: {actual} != {expected}"


def test_validate_state_payload_accepts_well_formed_payload() -> None:
    strat = _build_warmed_strategy()
    payload = strat.report_state_for_persistence()
    assert payload is not None
    result = strat.validate_state_payload(payload)
    assert result.payload_shape_ok
    assert result.failure_reason is None


def test_validate_state_payload_rejects_missing_keys() -> None:
    strat = SpyEmaCrossoverAlgorithm()
    bad = {"ema5": {}}  # missing ema10, rsi14, _prev_ema5_above_ema10, lifecycle
    result = strat.validate_state_payload(bad)
    assert result.failure_reason == "payload_mismatch"
    assert result.payload_shape_ok is False


def test_strategy_key_and_period_constants() -> None:
    assert SpyEmaCrossoverAlgorithm.STRATEGY_KEY == "spy_ema_crossover"
    assert SpyEmaCrossoverAlgorithm.CONSOLIDATOR_PERIOD_MIN == 15
```

- [ ] **Step 2: Run to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_spy_ema_persistence.py -v
```

Expected: `AttributeError: type object 'SpyEmaCrossoverAlgorithm' has no attribute 'STRATEGY_KEY'`.

- [ ] **Step 3: Add the three hooks + constants to SpyEmaCrossoverAlgorithm**

Modify `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py`. Add class-level constants and three methods (place them after `on_end_of_algorithm`):

```python
class SpyEmaCrossoverAlgorithm(Strategy):
    STRATEGY_KEY = "spy_ema_crossover"
    CONSOLIDATOR_PERIOD_MIN = 15

    # ... existing __init__, initialize, etc unchanged ...

    # ---- Indicator-state persistence hooks (PR1) ----

    def report_state_for_persistence(self) -> dict | None:
        """Return the strategy's persistable state, or None if not restorable.

        Returns None when any of:
          * indicators not all is_ready (the restored state would be
            sub-warmup and the validation ladder would reject it)
          * position not flat (we'd be hydrating into an open trade
            tomorrow with no way to reconcile entry context)
          * pending entry / open trade bookkeeping is mid-flight

        On the happy path returns a dict with ema5/ema10/rsi14 indicator
        states (via to_state_dict), the prev-cross flag, and a lifecycle
        block proving the strategy is flat.
        """
        if self._ema5 is None or self._ema10 is None or self._rsi14 is None:
            return None
        if not (self._ema5.is_ready and self._ema10.is_ready and self._rsi14.is_ready):
            return None
        if self._in_position:
            return None
        if self._pending_entry is not None or self._open_trade is not None:
            return None
        return {
            "ema5": self._ema5.to_state_dict(),
            "ema10": self._ema10.to_state_dict(),
            "rsi14": self._rsi14.to_state_dict(),
            "_prev_ema5_above_ema10": self._prev_ema5_above_ema10,
            "lifecycle": {
                "position_qty": 0,
                "pending_orders_count": 0,
                "open_insights": 0,
                "last_signal_kind": None,
                "last_signal_bar_end_ms": None,
            },
        }

    def restore_state_from_persistence(self, payload: dict) -> None:
        """Rehydrate indicator internals + _prev_ema5_above_ema10 from payload.

        Caller (LiveContext.hydrate_indicator_state) guarantees that
        ``validate_state_payload(payload)`` has already passed, and
        that this is called immediately after ``initialize()`` while
        indicators are fresh-constructed and unfed.
        """
        assert self._ema5 is not None and self._ema10 is not None and self._rsi14 is not None
        self._ema5.restore_state(payload["ema5"])
        self._ema10.restore_state(payload["ema10"])
        self._rsi14.restore_state(payload["rsi14"])
        self._prev_ema5_above_ema10 = bool(payload["_prev_ema5_above_ema10"])

    def validate_state_payload(self, payload: dict):
        """Shape-check the payload for this strategy. Returns a ValidationResult.

        Imports ValidationResult locally to avoid a module-level
        cycle (indicator_state -> strategy is not desirable; this
        method is rarely called in hot paths).
        """
        from app.engine.live.indicator_state import ValidationResult

        required_top = {"ema5", "ema10", "rsi14", "_prev_ema5_above_ema10", "lifecycle"}
        if not isinstance(payload, dict) or not required_top.issubset(payload.keys()):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        if not isinstance(payload["lifecycle"], dict):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        required_lifecycle = {"position_qty", "pending_orders_count", "open_insights"}
        if not required_lifecycle.issubset(payload["lifecycle"].keys()):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        return ValidationResult.all_passed()
```

- [ ] **Step 4: Run to verify pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_spy_ema_persistence.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5: Run engine + spec parity regression check**

```
podman exec polygon-data-service python -m pytest /app/tests/engine /app/tests/test_strategy_engine.py -v -k "not slow"
```

Expected: all prior strategy / engine tests still pass.

- [ ] **Step 6: Ruff check**

```
ruff check PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py PythonDataService/tests/engine/live/test_spy_ema_persistence.py
```

- [ ] **Step 7: Commit**

```
git add PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py PythonDataService/tests/engine/live/test_spy_ema_persistence.py
git commit -m "$(cat <<'EOF'
feat(strategy): SpyEmaCrossover persistence hooks (report/restore/validate)

Three strategy-local methods plus STRATEGY_KEY/CONSOLIDATOR_PERIOD_MIN
constants. report_state_for_persistence returns None unless indicators
are ready AND position is flat AND no pending/open trade. Bit-identical
round-trip test proves indicator outputs match a freshly-warmed
strategy on the next bar after restore.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `LiveContext.hydrate_indicator_state` + `maybe_write_indicator_state`

**Files:**
- Modify: `PythonDataService/app/engine/live/live_context.py`
- Modify: `PythonDataService/app/engine/live/indicator_state.py` (add the top-level `hydrate` + `maybe_write` functions)
- Test: `PythonDataService/tests/engine/live/test_live_context_hydrate.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/live/test_live_context_hydrate.py`:

```python
"""Tests for the hydrate/write flow at the LiveContext layer.

Covers the six-row validation ladder under all three policy modes plus
the maybe_write skip semantics (None payload, newer-check).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.engine.live.indicator_state import (
    HydratePolicy,
    HydrationReceipt,
    IndicatorStateEnvelope,
    IndicatorStateHydrationError,
)
from app.engine.live.live_context import LiveContext


def _fake_strategy_with_payload(payload: dict | None, validate_ok: bool = True) -> MagicMock:
    """Build a MagicMock standing in for a strategy with persistence hooks."""
    from app.engine.live.indicator_state import ValidationResult

    s = MagicMock()
    s.STRATEGY_KEY = "spy_ema_crossover"
    s.CONSOLIDATOR_PERIOD_MIN = 15
    s.report_state_for_persistence.return_value = payload
    s.validate_state_payload.return_value = (
        ValidationResult.all_passed() if validate_ok else ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
    )
    s.restore_state_from_persistence.return_value = None
    return s


def _valid_envelope_dict(last_bar_ms: int = 1747166100000) -> dict:
    """Envelope for a Monday 2026-05-18 force-flat (15:45 ET = 19:45 UTC)."""
    return {
        "schema_version": 1,
        "strategy_key": "spy_ema_crossover",
        "symbol": "SPY",
        "consolidator_period_min": 15,
        "last_consolidated_bar_end_ms": last_bar_ms,
        "captured_at_ms": last_bar_ms + 7000,
        "captured_reason": "force_flat",
        "code_sha": "abc",
        "strategy_spec_sha": "def",
        "payload": {
            "ema5": {"name": "EMA5", "period": 5, "samples": 18, "current_value": "412.34", "current_time_ms": last_bar_ms},
            "ema10": {"name": "EMA10", "period": 10, "samples": 18, "current_value": "411.23", "current_time_ms": last_bar_ms},
            "rsi14": {"name": "RSI14", "period": 14, "samples": 18, "current_value": "58.42", "current_time_ms": last_bar_ms},
            "_prev_ema5_above_ema10": True,
            "lifecycle": {"position_qty": 0, "pending_orders_count": 0, "open_insights": 0},
        },
    }


def _make_ctx(tmp_path: Path, policy: HydratePolicy = HydratePolicy.REQUIRE) -> LiveContext:
    """Construct a LiveContext rigged for hydrate testing."""
    portfolio = MagicMock()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    return LiveContext(
        portfolio=portfolio,
        hydrate_policy=policy,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        # Pin Mon 2026-05-19 09:30 ET = 13:30 UTC = 1747661400000 ms as session start.
        session_start_ms=1747661400000,
    )


# ---- happy path ----

def test_require_happy_path_restores_and_writes_accepted_receipt(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    # Seed the global path with a valid envelope whose last_bar_ms matches
    # the previous NYSE session close (Mon 2026-05-18 16:00 ET).
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    expected_prev_close_ms = 1747602000000  # Mon 2026-05-18 16:00 ET in UTC ms
    env_dict = _valid_envelope_dict(last_bar_ms=expected_prev_close_ms)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))

    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    ctx.hydrate_indicator_state(strat)

    strat.restore_state_from_persistence.assert_called_once()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is True


# ---- failure ladder under REQUIRE ----

def test_require_missing_raises_and_writes_missing_receipt(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    strat = _fake_strategy_with_payload(payload=None)
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is False
    assert receipt.validation.failure_reason == "missing"


def test_require_calendar_stale_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    # Friday-of-prior-week close (~96h+ before Tue session start).
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    bad_old_close_ms = 1746738000000  # Fri 2026-05-08 16:00 ET
    env_dict = _valid_envelope_dict(last_bar_ms=bad_old_close_ms)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "calendar_stale"


def test_require_identity_mismatch_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    env_dict = _valid_envelope_dict(last_bar_ms=1747602000000)
    env_dict["symbol"] = "QQQ"
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "identity_mismatch"


def test_require_payload_mismatch_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    env_dict = _valid_envelope_dict(last_bar_ms=1747602000000)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    # Strategy's validate_state_payload rejects (e.g. spec drift).
    strat = _fake_strategy_with_payload(payload=env_dict["payload"], validate_ok=False)
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "payload_mismatch"


def test_require_lifecycle_not_flat_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    env_dict = _valid_envelope_dict(last_bar_ms=1747602000000)
    env_dict["payload"]["lifecycle"]["position_qty"] = 100
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "lifecycle_not_flat"


def test_require_indicators_unready_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    env_dict = _valid_envelope_dict(last_bar_ms=1747602000000)
    # EMA5 with samples < period would be flagged.
    env_dict["payload"]["ema5"]["samples"] = 2  # below period=5
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "indicators_unready"


# ---- OPTIONAL policy ----

def test_optional_missing_cold_starts_and_writes_receipt(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.OPTIONAL)
    strat = _fake_strategy_with_payload(payload=None)
    ctx.hydrate_indicator_state(strat)  # no raise
    strat.restore_state_from_persistence.assert_not_called()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is False
    assert receipt.validation.failure_reason == "missing"


def test_optional_calendar_stale_cold_starts(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.OPTIONAL)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path
    env_dict = _valid_envelope_dict(last_bar_ms=1746738000000)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    ctx.hydrate_indicator_state(strat)
    strat.restore_state_from_persistence.assert_not_called()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "calendar_stale"


# ---- DISABLED policy ----

def test_disabled_writes_receipt_without_reading_sidecar(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.DISABLED)
    strat = _fake_strategy_with_payload(payload=None)
    ctx.hydrate_indicator_state(strat)
    strat.restore_state_from_persistence.assert_not_called()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.policy == HydratePolicy.DISABLED
    assert receipt.validation.failure_reason == "disabled_by_operator"


# ---- maybe_write ----

def test_maybe_write_skips_when_strategy_reports_none(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    strat = _fake_strategy_with_payload(payload=None)
    ctx.maybe_write_indicator_state(strat, reason="force_flat", code_sha="x", strategy_spec_sha="y",
                                    last_consolidated_bar_end_ms=1747602000000)
    from app.engine.live.indicator_state import stable_global_path
    global_path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    assert not global_path.exists()


def test_maybe_write_force_flat_writes(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    payload = _valid_envelope_dict()["payload"]
    strat = _fake_strategy_with_payload(payload=payload)
    ctx.maybe_write_indicator_state(strat, reason="force_flat", code_sha="x", strategy_spec_sha="y",
                                    last_consolidated_bar_end_ms=1747602000000)
    from app.engine.live.indicator_state import stable_global_path
    global_path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    assert global_path.exists()
    on_disk = IndicatorStateEnvelope.model_validate_json(global_path.read_text())
    assert on_disk.captured_reason == "force_flat"
    assert on_disk.last_consolidated_bar_end_ms == 1747602000000


def test_maybe_write_shutdown_refuses_to_overwrite_newer_force_flat(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    payload = _valid_envelope_dict()["payload"]
    strat = _fake_strategy_with_payload(payload=payload)
    # Existing force-flat write at 15:45 ET.
    ctx.maybe_write_indicator_state(strat, reason="force_flat", code_sha="x", strategy_spec_sha="y",
                                    last_consolidated_bar_end_ms=1747602000000)
    # Attempted shutdown write at 11:00 ET (older bar). Should refuse.
    older_bar_ms = 1747585800000  # earlier same day
    ctx.maybe_write_indicator_state(strat, reason="shutdown", code_sha="x", strategy_spec_sha="y",
                                    last_consolidated_bar_end_ms=older_bar_ms)
    from app.engine.live.indicator_state import stable_global_path
    global_path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    on_disk = IndicatorStateEnvelope.model_validate_json(global_path.read_text())
    # Still the force-flat write — shutdown was refused.
    assert on_disk.captured_reason == "force_flat"
    assert on_disk.last_consolidated_bar_end_ms == 1747602000000
```

- [ ] **Step 2: Run to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_context_hydrate.py -v
```

Expected: `TypeError: LiveContext.__init__() got an unexpected keyword argument 'hydrate_policy'`.

- [ ] **Step 3: Extend `LiveContext` with hydrate + write methods**

Modify `PythonDataService/app/engine/live/live_context.py` — extend the dataclass and add two methods. Replace the existing dataclass definition with:

```python
@dataclass
class LiveContext:
    """Runtime services exposed to strategies by the live engine."""

    portfolio: LivePortfolio
    _consolidators: dict[str, list[tuple[timedelta, TradeBarConsolidator, Callable[[TradeBar], None]]]] = field(
        default_factory=dict
    )
    symbols: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    current_time: datetime | None = None
    consolidated_bars: list[TradeBar] = field(default_factory=list)
    insight_manager: InsightManager = field(default_factory=InsightManager)
    _pre_handler_hook: Callable[[TradeBar], None] | None = None

    # ---- Indicator-state persistence ----
    hydrate_policy: "HydratePolicy | None" = None
    run_dir: "Path | None" = None
    artifacts_root: "Path | None" = None
    session_start_ms: int | None = None

    # ... existing methods unchanged ...
```

(Use forward-reference string types to avoid a top-level `from .indicator_state import HydratePolicy` cycle; resolve them inside the method bodies.)

Add two methods to `LiveContext`:

```python
    def hydrate_indicator_state(self, strategy) -> None:
        """Implement the §4.1 validation ladder + receipt-write contract.

        Caller (LiveEngine.run) invokes immediately after strategy.initialize().
        Behavior depends on self.hydrate_policy:
          REQUIRE  — failure -> write receipt + raise IndicatorStateHydrationError
          OPTIONAL — failure -> write receipt + return (cold-start)
          DISABLED — never reads sidecar; writes a 'disabled_by_operator' receipt and returns
          None     — persistence disabled at the engine level (replay tests); return
        """
        from app.engine.live.indicator_state import hydrate

        if self.hydrate_policy is None:
            return
        if self.run_dir is None or self.artifacts_root is None or self.session_start_ms is None:
            raise RuntimeError("hydrate_indicator_state requires run_dir, artifacts_root, session_start_ms on LiveContext")
        hydrate(
            strategy=strategy,
            policy=self.hydrate_policy,
            artifacts_root=self.artifacts_root,
            run_dir=self.run_dir,
            session_start_ms=self.session_start_ms,
        )

    def maybe_write_indicator_state(
        self,
        strategy,
        reason: str,
        *,
        code_sha: str,
        strategy_spec_sha: str,
        last_consolidated_bar_end_ms: int,
    ) -> None:
        """Implement the force-flat / shutdown write contract.

        No-op when persistence is disabled (artifacts_root is None).
        On force_flat: write if strategy reports a non-None payload.
        On shutdown:   write only if strictly newer than on-disk.
        """
        from app.engine.live.indicator_state import maybe_write

        if self.artifacts_root is None:
            return
        maybe_write(
            strategy=strategy,
            artifacts_root=self.artifacts_root,
            reason=reason,
            code_sha=code_sha,
            strategy_spec_sha=strategy_spec_sha,
            last_consolidated_bar_end_ms=last_consolidated_bar_end_ms,
        )
```

- [ ] **Step 4: Add top-level `hydrate` and `maybe_write` functions to `indicator_state.py`**

Append to `PythonDataService/app/engine/live/indicator_state.py`:

```python
def hydrate(
    *,
    strategy,
    policy: HydratePolicy,
    artifacts_root: "Path",
    run_dir: "Path",
    session_start_ms: int,
) -> None:
    """Run the six-row validation ladder + write the hydration receipt.

    Side effects:
      * Writes <run_dir>/indicator_state_hydration.json (always).
      * Calls strategy.restore_state_from_persistence(payload) on success.
      * Raises IndicatorStateHydrationError if policy=REQUIRE and the
        ladder rejects (after writing the receipt).
    """
    import time as _time

    from app.engine.live.nyse_calendar import NoSessionError, previous_completed_nyse_session_close_ms

    strategy_key = strategy.STRATEGY_KEY
    symbol = strategy.ctx.symbols[0] if strategy.ctx is not None and strategy.ctx.symbols else getattr(strategy, "_symbol_name", "")
    period = strategy.CONSOLIDATOR_PERIOD_MIN
    receipt_path = run_dir / "indicator_state_hydration.json"
    global_path = stable_global_path(artifacts_root, strategy_key, symbol, period)
    repo = IndicatorStateRepo(global_path)

    def _write_receipt(receipt: HydrationReceipt) -> None:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(receipt.model_dump_json(indent=2))

    def _base_receipt(accepted: bool, validation: ValidationResult, sidecar_last_bar_ms: int | None = None,
                      expected_prev_close_ms: int | None = None, global_sha: str | None = None) -> HydrationReceipt:
        return HydrationReceipt(
            schema_version=1,
            hydrated_at_ms=int(_time.time() * 1000),
            policy=policy,
            global_path=str(global_path),
            global_sha256=global_sha,
            accepted=accepted,
            strategy_key=strategy_key,
            symbol=symbol,
            consolidator_period_min=period,
            sidecar_last_consolidated_bar_end_ms=sidecar_last_bar_ms,
            expected_prev_session_close_ms=expected_prev_close_ms,
            calendar="NYSE",
            validation=validation,
        )

    if policy is HydratePolicy.DISABLED:
        _write_receipt(_base_receipt(
            accepted=False,
            validation=ValidationResult.failed("disabled_by_operator"),
        ))
        return

    # Check #1: schema parse + existence.
    try:
        envelope = repo.read()
    except Exception:
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("schema_mismatch", schema_version_ok=False),
            global_sha=repo.sha256_of_on_disk(),
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return
    if envelope is None:
        receipt = _base_receipt(accepted=False, validation=ValidationResult.failed("missing"))
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    global_sha = repo.sha256_of_on_disk()

    # Check #2: identity.
    if (envelope.strategy_key != strategy_key
            or envelope.symbol.upper() != symbol.upper()
            or envelope.consolidator_period_min != period):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("identity_mismatch", identity_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #3: calendar — previous completed NYSE session.
    try:
        expected_prev_close_ms = previous_completed_nyse_session_close_ms(session_start_ms)
    except NoSessionError:
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("calendar_stale", calendar_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return
    if envelope.last_consolidated_bar_end_ms != expected_prev_close_ms:
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("calendar_stale", calendar_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #4: payload shape — strategy's own validator.
    shape_result: ValidationResult = strategy.validate_state_payload(envelope.payload)
    if shape_result.failure_reason is not None:
        receipt = _base_receipt(
            accepted=False,
            validation=shape_result,
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #5: indicators ready — samples >= period (RSI: period+1).
    if not _indicators_ready(envelope.payload):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("indicators_unready", indicators_ready_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #6: lifecycle flat.
    lifecycle = envelope.payload["lifecycle"]
    if (lifecycle.get("position_qty", 0) != 0
            or lifecycle.get("pending_orders_count", 0) != 0
            or lifecycle.get("open_insights", 0) != 0):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("lifecycle_not_flat", lifecycle_flat_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # All passed.
    strategy.restore_state_from_persistence(envelope.payload)
    _write_receipt(_base_receipt(
        accepted=True,
        validation=ValidationResult.all_passed(),
        sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
        expected_prev_close_ms=expected_prev_close_ms,
        global_sha=global_sha,
    ))


def _indicators_ready(payload: dict) -> bool:
    """Check each indicator block has samples >= period (RSI needs period+1).

    Per-strategy if-name-is-RSI behavior is intentional for PR1 — the
    SpyEma strategy is the only consumer, and RSI's is_ready predicate
    is genuinely different from SMA/EMA's. A generic refactor lives in
    a future base-class promotion.
    """
    for key in ("ema5", "ema10", "rsi14"):
        block = payload.get(key)
        if not isinstance(block, dict):
            return False
        samples = int(block.get("samples", 0))
        period = int(block.get("period", 0))
        threshold = period + 1 if key.startswith("rsi") else period
        if samples < threshold:
            return False
    return True


def maybe_write(
    *,
    strategy,
    artifacts_root: "Path",
    reason: str,
    code_sha: str,
    strategy_spec_sha: str,
    last_consolidated_bar_end_ms: int,
) -> None:
    """Force-flat or graceful-shutdown checkpoint write.

    Skip without write if:
      * strategy.report_state_for_persistence returns None
      * reason == 'shutdown' and an on-disk envelope already has
        an equal-or-newer last_consolidated_bar_end_ms (the
        "newer check" — protects force-flat's canonical write).
    """
    import time as _time

    payload = strategy.report_state_for_persistence()
    if payload is None:
        return

    if reason not in ("force_flat", "shutdown"):
        raise ValueError(f"unknown reason: {reason!r}")

    strategy_key = strategy.STRATEGY_KEY
    symbol = strategy.ctx.symbols[0] if strategy.ctx is not None and strategy.ctx.symbols else getattr(strategy, "_symbol_name", "")
    period = strategy.CONSOLIDATOR_PERIOD_MIN

    envelope = IndicatorStateEnvelope(
        schema_version=1,
        strategy_key=strategy_key,
        symbol=symbol,
        consolidator_period_min=period,
        last_consolidated_bar_end_ms=last_consolidated_bar_end_ms,
        captured_at_ms=int(_time.time() * 1000),
        captured_reason=reason,
        code_sha=code_sha,
        strategy_spec_sha=strategy_spec_sha,
        payload=payload,
    )
    repo = IndicatorStateRepo(stable_global_path(artifacts_root, strategy_key, symbol, period))
    if reason == "shutdown" and not repo.is_strictly_newer_than_on_disk(envelope):
        return
    repo.write(envelope)
```

- [ ] **Step 5: Run to verify pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_context_hydrate.py -v
```

Expected: all 13 tests pass.

- [ ] **Step 6: Confirm prior LiveContext tests still pass**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_context.py -v
```

- [ ] **Step 7: Ruff check**

```
ruff check PythonDataService/app/engine/live/live_context.py PythonDataService/app/engine/live/indicator_state.py PythonDataService/tests/engine/live/test_live_context_hydrate.py
```

- [ ] **Step 8: Commit**

```
git add PythonDataService/app/engine/live/live_context.py PythonDataService/app/engine/live/indicator_state.py PythonDataService/tests/engine/live/test_live_context_hydrate.py
git commit -m "$(cat <<'EOF'
feat(live): LiveContext hydrate + maybe_write; the six-row ladder + receipt

LiveContext gets hydrate_policy / run_dir / artifacts_root / session_start_ms
plus hydrate_indicator_state and maybe_write_indicator_state methods. The
ladder lives in indicator_state.hydrate(): schema parse, identity,
NYSE-previous-completed-session, payload shape, indicators ready, lifecycle
flat. Receipt always writes; REQUIRE raises IndicatorStateHydrationError
on failure; OPTIONAL cold-starts; DISABLED never reads.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: LiveEngine wiring — three call sites

**Files:**
- Modify: `PythonDataService/app/engine/live/live_engine.py`
- Test: `PythonDataService/tests/engine/live/test_live_engine_checkpoint.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/live/test_live_engine_checkpoint.py`:

```python
"""LiveEngine call sites: hydrate post-init, write at force-flat, write in finally."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.indicator_state import (
    HydratePolicy,
    IndicatorStateEnvelope,
    IndicatorStateRepo,
    stable_global_path,
)
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from tests.engine.live.fixtures.fake_broker import FakeBroker


async def _empty_bar_source():
    """Empty async iterator — used when we want LiveEngine.run() to exit immediately after init."""
    if False:
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_force_flat_write_writes_sidecar(tmp_path: Path) -> None:
    """A successful force-flat (after warmup, flat lifecycle) writes the sidecar."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    # Bar source that crosses the force-flat barrier exactly once.
    async def bar_source():
        t = datetime(2026, 5, 18, 15, 55, 0, tzinfo=UTC)
        yield TradeBar(symbol="SPY", time=t, end_time=t, open=Decimal("400"), high=Decimal("400"),
                       low=Decimal("400"), close=Decimal("400"), volume=Decimal("0"))

    # Pre-warm the strategy externally (we want force_flat write to find indicators ready & flat).
    strat = SpyEmaCrossoverAlgorithm()
    # ... configure LiveEngine with artifacts_root + run_dir + hydrate_policy=DISABLED
    # (so the hydrate call site doesn't trip on missing sidecar)
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=dtime(15, 55)),
    )

    # Implementation hint: this test is asserting wiring exists; it
    # exercises the call-site path. The actual sidecar contents depend
    # on the strategy's indicators being ready, which is non-trivial to
    # set up in a unit test. Adopt: warm the strategy externally via
    # the strategy's API before passing into engine.run, or assert the
    # call site was invoked via a spy on LiveContext.maybe_write_indicator_state.
    # Recommended: spy.

    from unittest.mock import patch
    with patch("app.engine.live.live_context.LiveContext.maybe_write_indicator_state") as spy:
        await engine.run(strat, bars=bar_source())
        # At least one call with reason='force_flat' should have happened.
        force_flat_calls = [c for c in spy.call_args_list if c.kwargs.get("reason") == "force_flat" or (len(c.args) >= 2 and c.args[1] == "force_flat")]
        assert force_flat_calls, f"no force_flat write call observed; calls={spy.call_args_list}"


@pytest.mark.asyncio
async def test_shutdown_path_invokes_write_with_reason_shutdown(tmp_path: Path) -> None:
    """Engine's finally block invokes maybe_write with reason='shutdown'."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    from unittest.mock import patch
    with patch("app.engine.live.live_context.LiveContext.maybe_write_indicator_state") as spy:
        await engine.run(strat, bars=_empty_bar_source())
        shutdown_calls = [c for c in spy.call_args_list if c.kwargs.get("reason") == "shutdown" or (len(c.args) >= 2 and c.args[1] == "shutdown")]
        assert shutdown_calls, f"no shutdown write call observed; calls={spy.call_args_list}"


@pytest.mark.asyncio
async def test_hydrate_policy_disabled_writes_receipt_at_init(tmp_path: Path) -> None:
    """The hydrate call site fires immediately after strategy.initialize()."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    await engine.run(strat, bars=_empty_bar_source())
    receipt_path = run_dir / "indicator_state_hydration.json"
    assert receipt_path.exists()


@pytest.mark.asyncio
async def test_hydrate_policy_require_with_missing_sidecar_raises_exit4_intent(tmp_path: Path) -> None:
    """REQUIRE policy with no sidecar raises IndicatorStateHydrationError before any bar runs."""
    from app.engine.live.indicator_state import IndicatorStateHydrationError

    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.REQUIRE,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    with pytest.raises(IndicatorStateHydrationError):
        await engine.run(strat, bars=_empty_bar_source())
```

- [ ] **Step 2: Run to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_engine_checkpoint.py -v
```

Expected: `TypeError: LiveEngine.__init__() got an unexpected keyword argument 'artifacts_root'`.

- [ ] **Step 3: Wire LiveEngine to LiveContext persistence**

Modify `PythonDataService/app/engine/live/live_engine.py`:

In `LiveEngine.__init__`, add new ctor params and store them:

```python
    def __init__(
        self,
        client: IbkrClient | None,
        config: LiveConfig | None = None,
        *,
        broker: BrokerAdapter | None = None,
        output_dir: Path | None = None,
        account_id: str = "",
        readonly: bool = False,
        max_orders_per_day: int | None = None,
        fill_window_ms: int | None = None,
        # NEW: indicator-state persistence.
        artifacts_root: Path | None = None,
        hydrate_policy: "HydratePolicy | None" = None,
        session_start_ms: int | None = None,
        code_sha: str = "",
        strategy_spec_sha: str = "",
    ) -> None:
        # ... existing body ...
        self._artifacts_root = artifacts_root
        self._hydrate_policy = hydrate_policy
        self._session_start_ms = session_start_ms
        self._code_sha = code_sha
        self._strategy_spec_sha = strategy_spec_sha
```

In `LiveEngine.run`, replace the `LiveContext` construction on line ~326 with one that passes the persistence params:

```python
        ctx = LiveContext(
            portfolio=portfolio,
            hydrate_policy=self._hydrate_policy,
            run_dir=self._output_dir,
            artifacts_root=self._artifacts_root,
            session_start_ms=self._session_start_ms,
        )
        strategy.ctx = ctx
        strategy.initialize()

        # NEW: hydrate call site (after initialize, before bar loop).
        if self._hydrate_policy is not None:
            ctx.hydrate_indicator_state(strategy)
```

In the force-flat barrier (the existing `if force_flat_at is not None and ...` block), after `strategy.on_force_flat()` and `last_force_flat_date = minute_bar.time.date()`, add the write call:

```python
                    strategy.on_force_flat()
                    last_force_flat_date = minute_bar.time.date()
                    # NEW: indicator-state checkpoint at force-flat.
                    ctx.maybe_write_indicator_state(
                        strategy,
                        reason="force_flat",
                        code_sha=self._code_sha,
                        strategy_spec_sha=self._strategy_spec_sha,
                        last_consolidated_bar_end_ms=int(minute_bar.end_time.timestamp() * 1000),
                    )
```

In `LiveEngine.run`'s `finally` block (line ~602), add the shutdown write just before writers close:

```python
        finally:
            # NEW: indicator-state checkpoint at graceful shutdown.
            if last_bar is not None:
                try:
                    ctx.maybe_write_indicator_state(
                        strategy,
                        reason="shutdown",
                        code_sha=self._code_sha,
                        strategy_spec_sha=self._strategy_spec_sha,
                        last_consolidated_bar_end_ms=int(last_bar.end_time.timestamp() * 1000),
                    )
                except Exception:
                    logger.exception("indicator-state shutdown checkpoint failed; continuing finally cleanup")
            if started_event_stream and isinstance(self._broker, IbkrEventAdapter):
                await self._broker.stop_event_stream()
            if writers is not None:
                writers.close_all()
```

- [ ] **Step 4: Run to verify pass**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_engine_checkpoint.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Run the existing LiveEngine + replay parity tests as regression**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_engine.py /app/tests/engine/live/test_live_engine_replay.py /app/tests/engine/live/test_live_engine_collapse.py -v
```

Expected: every prior test passes. The replay parity test must remain at `Decimal("0")` tolerance — confirm `hydrate_policy` defaults to `None` so replay paths skip persistence entirely.

- [ ] **Step 6: Ruff check**

```
ruff check PythonDataService/app/engine/live/live_engine.py PythonDataService/tests/engine/live/test_live_engine_checkpoint.py
```

- [ ] **Step 7: Commit**

```
git add PythonDataService/app/engine/live/live_engine.py PythonDataService/tests/engine/live/test_live_engine_checkpoint.py
git commit -m "$(cat <<'EOF'
feat(live): LiveEngine wires hydrate (post-init) + write (force-flat, finally)

Three call sites:
  * after strategy.initialize() — ctx.hydrate_indicator_state(strategy)
  * after force-flat barrier completes — ctx.maybe_write_indicator_state(strategy, 'force_flat')
  * in run() finally block — ctx.maybe_write_indicator_state(strategy, 'shutdown')

hydrate_policy=None disables all three (default — replay tests untouched,
parity gate stays at Decimal('0') tolerance). The shutdown write swallows
exceptions to preserve graceful-shutdown invariants on broker errors.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `run.py` — `--hydrate-policy` flag + exit code 4

**Files:**
- Modify: `PythonDataService/app/engine/live/run.py`
- Modify: `PythonDataService/tests/engine/live/test_run_cli.py`

- [ ] **Step 1: Read the existing run.py to find the `start` subcommand**

Open `PythonDataService/app/engine/live/run.py` and locate `cmd_start` and its arg parser. Note where existing flags like `--readonly` are defined.

- [ ] **Step 2: Write failing tests**

Add these tests to `PythonDataService/tests/engine/live/test_run_cli.py`:

```python
"""(Append to file) Tests for --hydrate-policy / --allow-cold-start / exit 4."""

# At the top of the file or in existing imports:
import subprocess
import sys


def test_start_accepts_hydrate_policy_require(tmp_path, monkeypatch):
    """--hydrate-policy require parses and threads to LiveEngine."""
    # Build a minimal valid run_dir + run_ledger.json then invoke start.
    # Use the test's existing helper if there is one.
    # Assert the engine constructor received hydrate_policy=HydratePolicy.REQUIRE.
    ...  # see implementation note below


def test_start_accepts_allow_cold_start_alias(tmp_path):
    """--allow-cold-start is an alias for --hydrate-policy disabled."""
    ...


def test_start_default_hydrate_policy_is_require():
    """No --hydrate-policy flag => HydratePolicy.REQUIRE."""
    ...


def test_hydration_failure_exits_code_4(tmp_path):
    """A REQUIRE policy with missing sidecar exits 4 (not 1, 2, or 3)."""
    ...
```

**Implementation note:** the existing `test_run_cli.py` already drives `cmd_start` either as a subprocess or by calling the function directly with mocked args. Match the style of the existing tests in that file. The four tests above should:
- For the parse-only tests: invoke `cmd_start`'s argparser directly (factor it out of `cmd_start` if not already, into a `build_start_parser()` helper).
- For the exit-code-4 test: invoke `cmd_start` end-to-end against a `tmp_path` run-dir with no sidecar and assert it returns exit 4. Mock the IbkrClient / broker so the test doesn't actually try to connect.

Look at the existing `test_run_cli.py` and `test_run_cli_shutdown.py` for the mocking pattern. Copy that style.

- [ ] **Step 3: Run tests to verify failure**

Run:
```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_run_cli.py -v -k "hydrate or cold_start or code_4"
```

Expected: AttributeErrors / failures because the flags aren't wired.

- [ ] **Step 4: Add the CLI flags + exit-code-4 handling**

In `cmd_start` (or wherever the `start` subcommand's parser is built), add:

```python
    start_parser.add_argument(
        "--hydrate-policy",
        choices=["require", "optional", "disabled"],
        default="require",
        help="Indicator-state hydrate policy. 'require' is the default for the B2 dry-run gate "
             "and paper-week operation; failure to validate the prior session's sidecar exits 4 "
             "before any bar runs. 'optional' is the seed-day mode that cold-starts when no "
             "sidecar exists. 'disabled' skips the read entirely (still writes at end-of-session).",
    )
    start_parser.add_argument(
        "--allow-cold-start",
        action="store_const",
        const="disabled",
        dest="hydrate_policy",
        help="Alias for --hydrate-policy disabled. The operator escape hatch.",
    )
```

In `cmd_start`'s engine construction, thread the policy through:

```python
    from app.engine.live.indicator_state import HydratePolicy, IndicatorStateHydrationError

    policy = HydratePolicy(args.hydrate_policy)

    # ... existing artifacts/output dir setup ...

    engine = LiveEngine(
        client=client,
        config=live_config,
        broker=broker,
        output_dir=run_dir,
        account_id=account_id,
        readonly=args.readonly,
        artifacts_root=Path("PythonDataService/artifacts"),
        hydrate_policy=policy,
        session_start_ms=ledger.start_date_ms,  # field on LiveRunLedger
        code_sha=ledger.code_sha,
        strategy_spec_sha=ledger.strategy_spec_sha,
    )

    try:
        await engine.run(strategy, ibkr_bars=...)
    except IndicatorStateHydrationError as exc:
        logger.error("indicator-state hydrate failed (%s); see %s",
                     exc.receipt.validation.failure_reason, run_dir / "indicator_state_hydration.json")
        return 4
    # ... rest of existing exception handling ...
```

(Adjust field names — `start_date_ms`, `code_sha`, etc. — to match `LiveRunLedger`'s actual schema; check `run_ledger.py` for the exact names.)

- [ ] **Step 5: Run to verify pass**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_run_cli.py -v -k "hydrate or cold_start or code_4"
```

Expected: all 4 new tests pass.

- [ ] **Step 6: Run full run-cli regression**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_run_cli.py /app/tests/engine/live/test_run_cli_shutdown.py -v
```

Expected: every prior test still passes.

- [ ] **Step 7: Ruff check**

```
ruff check PythonDataService/app/engine/live/run.py PythonDataService/tests/engine/live/test_run_cli.py
```

- [ ] **Step 8: Commit**

```
git add PythonDataService/app/engine/live/run.py PythonDataService/tests/engine/live/test_run_cli.py
git commit -m "$(cat <<'EOF'
feat(live): start --hydrate-policy / --allow-cold-start; exit code 4 on hydrate failure

Default policy is 'require' for the B2 dry-run gate. --allow-cold-start is
the operator escape hatch (alias for 'disabled'). IndicatorStateHydrationError
caught in cmd_start exits 4 (distinct from existing 1/2/3) with a log line
pointing at indicator_state_hydration.json for post-mortem.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `reconcile.py` — hash manifest includes the hydration receipt

**Files:**
- Modify: `PythonDataService/app/engine/live/reconcile.py`
- Modify: `PythonDataService/tests/engine/live/test_reconcile.py`

- [ ] **Step 1: Read existing reconcile.py to find the hash manifest builder**

Open `PythonDataService/app/engine/live/reconcile.py` and grep for `_build_day_hashes_manifest` (or whatever the function that produces `day-N.hashes.json` is named). Note the keys it currently lists.

- [ ] **Step 2: Write a failing test**

Add to `PythonDataService/tests/engine/live/test_reconcile.py`:

```python
def test_day_hashes_manifest_includes_hydration_receipt_if_present(tmp_path):
    """When <run_dir>/indicator_state_hydration.json exists, its SHA-256 appears in day-N.hashes.json."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    receipt_text = '{"schema_version":1,"hydrated_at_ms":1700000000000,"accepted":true}'
    (run_dir / "indicator_state_hydration.json").write_text(receipt_text)

    # ... call the existing reconcile.write_day_report or whatever produces
    # the hashes.json; assert the manifest contains an indicator_state_hydration.json
    # key whose value is the SHA-256 of receipt_text.
    import hashlib
    expected_sha = hashlib.sha256(receipt_text.encode("utf-8")).hexdigest()
    # ... assert ...
```

(Implementation detail: this test needs minimal QC artifacts + decisions/executions parquets to drive `write_day_report`. Reuse the synthetic-fixture pattern from existing `test_reconcile.py` tests — there's almost certainly a helper that builds a tiny day. If not, the test can construct the manifest directly via the helper function.)

- [ ] **Step 3: Run to verify failure**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_reconcile.py -v -k "hydration"
```

- [ ] **Step 4: Extend the manifest builder**

Modify the manifest-building function in `reconcile.py` to look for `<run_dir>/indicator_state_hydration.json` and add it to the manifest dict if present:

```python
    hydration_receipt = run_dir / "indicator_state_hydration.json"
    if hydration_receipt.exists():
        manifest["indicator_state_hydration.json"] = _sha256_of_file(hydration_receipt)
```

Also extend the Markdown receipt's manifest-listing section to list this file alongside the existing entries — match the formatting of the existing rows.

- [ ] **Step 5: Run to verify pass**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_reconcile.py -v
```

Expected: new test passes; all prior reconcile tests still pass.

- [ ] **Step 6: Ruff check**

```
ruff check PythonDataService/app/engine/live/reconcile.py PythonDataService/tests/engine/live/test_reconcile.py
```

- [ ] **Step 7: Commit**

```
git add PythonDataService/app/engine/live/reconcile.py PythonDataService/tests/engine/live/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): day-N hash manifest includes indicator_state_hydration.json

When the run dir contains a hydration receipt (always present when
LiveEngine ran with a hydrate_policy other than None), its SHA-256 lands
in day-N.hashes.json and the committed day-N.md manifest section. Forensic
trail: "what state was this run hydrated from?" is one cat away.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Documentation updates

**Files:**
- Modify: `docs/runbooks/ibkr-paper-dry-run.md`
- Modify: `docs/ibkr-integration-authority.md`
- Modify: `PythonDataService/app/engine/live/README.md`

- [ ] **Step 1: Update the runbook (`docs/runbooks/ibkr-paper-dry-run.md`)**

Add a new subsection to Step 3 titled "Hydrate policy":

```markdown
### Hydrate policy

The `start` subcommand reads / validates the prior session's indicator
state sidecar before consuming any bars. Three modes:

| Flag                      | Behavior                                                       | When to use |
|---|---|---|
| `--hydrate-policy require` (default) | Validate sidecar; exit 4 on missing/stale/mismatched/unready/non-flat | B2 dry-run gate and paper week (the default — no flag needed) |
| `--hydrate-policy optional` | Cold-start when sidecar missing/invalid; write at end-of-session | Seed day (Monday of paper week, or first ever run) |
| `--hydrate-policy disabled` (alias: `--allow-cold-start`) | Never read sidecar; still write at end-of-session | Operator escape hatch ("I know yesterday's state is bad, warmup from scratch today") |

On exit 4 under require, inspect `<run_dir>/indicator_state_hydration.json`
for the failure reason (`missing`, `schema_mismatch`, `identity_mismatch`,
`calendar_stale`, `payload_mismatch`, `indicators_unready`,
`lifecycle_not_flat`).

State lives at `PythonDataService/artifacts/live_state/<strategy_key>/<symbol>_<period>m.json`
and is keyed by identity-tuple — every `run_id` reads / writes the same
file, so day-over-day continuity is automatic.
```

Add a Step 3a:

```markdown
## Step 3a — Seed day (first ever run)

The very first paper-week run has no prior sidecar. Use:

```bash
IBKR_HOST=127.0.0.1 \
  PYTHONPATH=PythonDataService python -m app.engine.live.run start \
  --run-dir PythonDataService/artifacts/live_runs/<run_id> \
  --readonly \
  --hydrate-policy optional
```

The runner cold-starts (3 h 45 m warmup), writes its first sidecar at
15:55 ET force-flat completion. From day 2 onward, `--hydrate-policy require`
(the default) accepts that sidecar and skips warmup.
```

Update Step 4's halt-flag cleanup language to reference both dry-run days, not just one.

- [ ] **Step 2: Update `docs/ibkr-integration-authority.md`**

Find §6 "Live runtime" surface table. Add two rows alphabetically:

```markdown
| `indicator_state.py` | Envelope/payload Pydantic models, HydratePolicy tri-state, IndicatorStateRepo (atomic write + advisory lock), the six-row validation ladder, top-level hydrate() and maybe_write() entry points. |
| `nyse_calendar.py` | `previous_completed_nyse_session_close_ms` — pandas_market_calendars NYSE schedule wrapper; consumed only by indicator-state validation (ladder check #3). |
```

In §11 "What does NOT ship today", flip the row "indicator-state-persistence across restarts":

```markdown
| Phase 10 prereq — indicator-state-persistence across restarts | **SHIPPED** (2026-05-15, PR #<TBD>) | Generic envelope + SpyEma-specific payload at `PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json`. Three policies: `require` (default — paper-week gate), `optional` (seed-day cold-start), `disabled` (operator escape hatch). NYSE previous-completed-session staleness check. Per-run hydration receipt rolled into reconcile hash manifest. Design: `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md`. |
```

In §12 "Operational checklist", expand item 3 to mention hydrate-policy expectations:

```markdown
3. **IB Gateway** ... (existing content)
   
   On day 1 of paper week, pass `--hydrate-policy optional` to seed the
   sidecar; on day 2+ omit the flag (default `require`). See the runbook
   Step 3 "Hydrate policy" subsection.
```

Bump "Last reviewed" to 2026-05-15.

- [ ] **Step 3: Update `PythonDataService/app/engine/live/README.md`**

Add a new section after the existing content:

```markdown
## Indicator state persistence

`SpyEmaCrossoverAlgorithm`'s indicators (EMA5, EMA10, RSI14) persist
across runs so the operator doesn't pay the 3 h 45 m warmup cost every
morning.

**Files:**
- Stable sidecar: `PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json`
- Per-run hydration receipt: `<run_dir>/indicator_state_hydration.json`

**Modules:**
- `indicator_state.py` — envelope, payload, policy enum, repo, validation ladder, hydrate() and maybe_write()
- `nyse_calendar.py` — previous-completed-session lookup (staleness check)

**Policy tri-state on `start`:**
- `require` (default) — exit 4 on any validation failure
- `optional` — cold-start on failure; useful for seed day
- `disabled` (alias `--allow-cold-start`) — never read; still write

**Write triggers:**
- Force-flat completion (15:55 ET) — first checkpoint
- Graceful-shutdown `finally` — "newer" check refuses to overwrite force-flat with earlier-Ctrl-C state

**Design doc:** `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md`
```

- [ ] **Step 4: No tests for docs; just commit**

```
git add docs/runbooks/ibkr-paper-dry-run.md docs/ibkr-integration-authority.md PythonDataService/app/engine/live/README.md
git commit -m "$(cat <<'EOF'
docs(authority): indicator-state persistence shipped; runbook + authority + live README

Runbook gets the --hydrate-policy subsection in Step 3, a new Step 3a for
seed day, and the halt-flag cleanup applied to both dry-run days. Authority
doc adds indicator_state.py / nyse_calendar.py to the §6 surface table,
flips the §11 prereq row to SHIPPED, and expands §12 item 3 with policy
expectations. live/README.md gets a dedicated section.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: PR1 verification — project-scope lint + tests + push + open PR

- [ ] **Step 1: Project-scope ruff**

Run from host:

```
ruff check PythonDataService/app/ PythonDataService/tests/
```

Expected: zero issues. (Per memory: run from host, not container, because container does not have ruff.toml in scope.)

- [ ] **Step 2: Establish a master baseline for pre-existing test failures**

This is critical per `.claude/rules/testing.md` "Pre-push test-suite hygiene." Before treating any failure as "not mine":

```
git stash
git checkout master
podman exec polygon-data-service python -m pytest /app/tests -k "not slow" 2>&1 | tee /tmp/baseline.log
git checkout feat/indicator-state-persistence-spy-ema
git stash pop || true
```

Record any pre-existing failures from `/tmp/baseline.log` for the PR description.

- [ ] **Step 3: Project-scope pytest on the feature branch**

```
podman exec polygon-data-service python -m pytest /app/tests -k "not slow" 2>&1 | tee /tmp/feature.log
```

Compare: every failure in `/tmp/feature.log` must either be in `/tmp/baseline.log` (pre-existing, surface in PR description) or be a known new test added in this branch and now passing. No new failures.

- [ ] **Step 4: Verify the replay parity gate still passes (locally — CI skips when lean-cache absent)**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_engine_replay.py -v
```

If `lean-cache/` is present locally, this must pass at `Decimal("0")` tolerance. If absent (CI behavior), it skips.

- [ ] **Step 5: Push the branch and open PR1**

```
git push -u origin feat/indicator-state-persistence-spy-ema
gh pr create --title "feat(live): SPY EMA indicator-state persistence (B2 dry-run prereq)" --body "$(cat <<'EOF'
## Summary

Closes the "indicator-state-persistence across restarts" Phase 10 prereq listed in `docs/ibkr-integration-authority.md` §11. Ships:

- Generic envelope + `SpyEmaCrossoverAlgorithm`-specific payload at `PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json`
- `HydratePolicy` tri-state (`require | optional | disabled`) + `--allow-cold-start` alias
- Six-row validation ladder (schema, identity, NYSE previous-session calendar, payload shape, indicators ready, lifecycle flat)
- Per-run hydration receipt at `<run_dir>/indicator_state_hydration.json`, rolled into the reconcile hash manifest
- Write triggers: force-flat (canonical) + graceful-shutdown (newer-check refuses to overwrite)
- Exit code 4 for hydrate validation failure under `require`

Design: `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md`
Plan: `docs/superpowers/plans/2026-05-15-spy-ema-paper-dry-run.md`

## Test plan

- [x] All new unit / integration tests pass under `pytest -k "not slow"`
- [x] Existing `test_live_engine_replay.py` parity gate untouched (skips on CI without lean-cache; passes locally at `Decimal("0")` tolerance)
- [x] `ruff check PythonDataService/app/ PythonDataService/tests/` clean from host
- [x] Pre-existing failures (if any) listed below; no new failures introduced
- [ ] PR2 (producer-consumer CI test) cuts off master after this merges
- [ ] Operator seeds sidecar on Mon 2026-05-18 (`--hydrate-policy optional`)
- [ ] Operator runs B2 gate on Tue 2026-05-19 (`--hydrate-policy require`); deliverable is `docs/references/reconciliations/dry-run-2026-05-19/day-0.md`

## Pre-existing test failures (baseline against master)

<paste from /tmp/baseline.log if any; otherwise "none">

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Hand off to PR monitor**

Per memory ("PR workflow — commits, review, parallel progress" and "Autonomous PR merge and pull"): the PR-monitor agent handles review and merge autonomously. Claude keeps moving forward on PR2.

---

## Task 13: PR2 — producer-consumer CI test (`LiveEngine → reconcile`)

**This task runs on a separate branch off master, cut AFTER PR1 merges.**

**Files:**
- Create: `PythonDataService/tests/engine/live/test_live_engine_to_reconcile_producer.py`

- [ ] **Step 1: After PR1 merges, branch off master**

```
git checkout master
git pull origin master
git checkout -b test/live-to-reconcile-producer
```

- [ ] **Step 2: Write the test**

Create `PythonDataService/tests/engine/live/test_live_engine_to_reconcile_producer.py`:

```python
"""Producer-consumer CI test: LiveEngine artifacts feed reconcile cleanly.

The dry-run gate is the integration test; this is the cheap CI version
that proves the schema contract without needing a Gateway. Spec
reference: docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md
§5.2.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.indicator_state import HydratePolicy, stable_global_path
from app.engine.live.live_engine import LiveEngine
from app.engine.live.reconcile import write_day_report  # adjust import to actual symbol
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from tests.engine.live.fixtures.fake_broker import FakeBroker


async def _make_short_bar_source(start: datetime, count: int = 30):
    """Yield ``count`` 1-minute SPY bars starting at ``start``."""
    for i in range(count):
        t = start + timedelta(minutes=i)
        yield TradeBar(
            symbol="SPY",
            time=t,
            end_time=t + timedelta(minutes=1),
            open=Decimal("400") + Decimal(i),
            high=Decimal("400") + Decimal(i),
            low=Decimal("400") + Decimal(i),
            close=Decimal("400") + Decimal(i),
            volume=Decimal("0"),
        )


@pytest.mark.asyncio
async def test_live_engine_produces_artifacts_reconcile_consumes_them(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,  # no prior sidecar in test
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),  # no force-flat during the 30 minute test window
        code_sha="abc",
        strategy_spec_sha="def",
    )

    bar_source = _make_short_bar_source(datetime(2026, 5, 18, 14, 0, tzinfo=UTC), count=30)
    await engine.run(strat, bars=bar_source)

    # Assert hydration receipt exists with accepted=false / disabled.
    receipt_path = run_dir / "indicator_state_hydration.json"
    assert receipt_path.exists()
    receipt_text = receipt_path.read_text()
    assert '"accepted":false' in receipt_text  # disabled policy

    # Build a synthetic QC dir for reconcile (self-consistent — strategy didn't fire so this is empty).
    qc_dir = tmp_path / "qc-dry-run" / "2026-05-18"
    qc_dir.mkdir(parents=True)

    docs_dir = tmp_path / "docs-out"
    docs_dir.mkdir()
    write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="producer-test-2026-05-18",
        day_n=0,
        day_date="2026-05-18",
    )

    # Assert all four artifacts written.
    assert (docs_dir / "day-0.md").exists()
    assert (docs_dir / "day-0.json").exists()
    assert (run_dir / "reconcile" / "day-0.parquet").exists() or (docs_dir / "day-0.parquet").exists()
    hashes_path = (run_dir / "reconcile" / "day-0.hashes.json") if (run_dir / "reconcile").exists() else (docs_dir / "day-0.hashes.json")
    assert hashes_path.exists()

    # Hash manifest must include the hydration receipt with the correct SHA-256.
    import json
    hashes = json.loads(hashes_path.read_text())
    assert "indicator_state_hydration.json" in hashes
    expected_sha = hashlib.sha256(receipt_text.encode("utf-8")).hexdigest()
    assert hashes["indicator_state_hydration.json"] == expected_sha

    # Markdown manifest section must mention it too.
    md = (docs_dir / "day-0.md").read_text()
    assert "indicator_state_hydration.json" in md
```

(Adjust `write_day_report` signature and output paths to match `reconcile.py`'s actual API — the test should be a thin wrapper around the existing reconcile entry point.)

- [ ] **Step 3: Run to verify pass**

```
podman exec polygon-data-service python -m pytest /app/tests/engine/live/test_live_engine_to_reconcile_producer.py -v
```

Expected: passes. If the artifact paths or `write_day_report` signature surprise you, adjust the test to match — the goal is "engine wrote, reconcile consumed, hydration receipt SHA round-trips."

- [ ] **Step 4: Project-scope ruff + pytest baseline**

```
ruff check PythonDataService/app/ PythonDataService/tests/
podman exec polygon-data-service python -m pytest /app/tests -k "not slow"
```

- [ ] **Step 5: Commit + push + open PR2**

```
git add PythonDataService/tests/engine/live/test_live_engine_to_reconcile_producer.py
git commit -m "$(cat <<'EOF'
test(live): producer-consumer CI test for LiveEngine -> reconcile contract

Drives a 30-bar minimal LiveEngine session against FakeBroker; asserts
reconcile.write_day_report consumes the produced artifacts and that the
hydration receipt's SHA-256 round-trips through the hash manifest and the
committed Markdown receipt. Closes the §11 "no end-to-end producer test"
prereq from ibkr-integration-authority.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push -u origin test/live-to-reconcile-producer
gh pr create --title "test(live): LiveEngine -> reconcile producer-consumer CI test" --body "$(cat <<'EOF'
## Summary

Closes the §11 Phase 10 prereq "end-to-end producer test (LiveEngine → reconcile)" from `docs/ibkr-integration-authority.md`. Drives a 30-bar minimal session against `FakeBroker`, then runs `reconcile.write_day_report` against the artifacts and asserts the hydration receipt's SHA-256 round-trips through the hash manifest and the committed Markdown receipt.

Cheap regression guard: a future refactor that breaks the artifact-schema contract now fails CI instead of waiting for an operator dry-run to surface it.

## Test plan

- [x] Test passes locally
- [x] Ruff clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Post-merge: operator dry-run (Mon 2026-05-18 + Tue 2026-05-19)

This is operator work, not Claude work. Reference §9 of the design doc for the per-day timeline. The deliverable is `docs/references/reconciliations/dry-run-2026-05-19/day-0.md` committed in a small receipt PR after Tuesday's session.

---

## Self-review of this plan against the spec

(Plan author's checklist — pass before declaring the plan complete.)

**Spec coverage:**
- §1 goal & pass criteria → covered by Tasks 7–12 collectively; pass criteria 1 (exit 4 on REQUIRE failure) is Task 9; pass criteria 3 (decisions.parquet populated from 09:45 ET) is operator-day, gated on the test stack being green; pass criterion 5 (receipt in hash manifest) is Task 10.
- §2 architecture module list → Task 1 (nyse_calendar), Task 2 (envelope/payload/policy/receipt), Task 3 (repo), Task 4 (Indicator base), Task 5 (SMA/EMA/RSI), Task 6 (strategy hooks), Task 7 (LiveContext + ladder), Task 8 (LiveEngine wiring), Task 9 (CLI), Task 10 (reconcile), Task 11 (docs). ✓
- §3 data flow → Task 7 (hydrate flow), Task 8 (force-flat / shutdown call sites). ✓
- §4 validation ladder → Task 7 (six checks in `hydrate`). ✓
- §4.3 exit code 4 → Task 9. ✓
- §5 tests → distributed across each task; PR2 is Task 13. ✓
- §6 calendar helper → Task 1. ✓
- §7 docs → Task 11. ✓
- §8 PR sequencing → Tasks 12 (PR1 push) and Task 13 (PR2 cut). ✓
- §9 operator timeline → post-merge, intentionally out-of-plan. ✓

**Placeholder scan:** no `TODO`, `TBD`, "implement later" in any step. Step 9.4 has an `<paste from /tmp/baseline.log if any; otherwise "none">` template marker in the PR body — that is an operator action, not a plan placeholder, and it is explicit about what to fill in.

**Type consistency:** `HydratePolicy.REQUIRE` / `OPTIONAL` / `DISABLED` used consistently; `IndicatorStateEnvelope` / `IndicatorStatePayload` / `HydrationReceipt` / `ValidationResult` consistent across Tasks 2, 3, 6, 7. Strategy methods `report_state_for_persistence` / `restore_state_from_persistence` / `validate_state_payload` consistent across Tasks 6 and 7. `STRATEGY_KEY` and `CONSOLIDATOR_PERIOD_MIN` class constants introduced in Task 6 are consumed in Tasks 7 (`indicator_state.hydrate`) and 8 (`LiveEngine` doesn't read them; LiveContext does via strategy ref) — consistent.

**Scope check:** Twelve PR1 tasks + one PR2 task → reasonable for one design-doc cycle. No subsystem coupling that would benefit from decomposition; the layered build order (calendar → models → repo → indicators → strategy → context → engine → CLI → reconcile → docs) is mostly bottom-up dependency order and each task produces a green-tests checkpoint.

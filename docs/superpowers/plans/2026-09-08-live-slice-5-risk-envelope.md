# ADR 0059 Slice 5 — Risk Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every live-world ENTER is admitted against the ADR 0059 risk envelope — the cash bound and the daily loss hold — inside `accept_enter`, with the loss hold as a durable account-wide hold that a guarded operator action clears, and the verdict reporting both.

**Architecture:** A pure envelope module owns the values, the sha, the agreement rule, the cash rule and the loss rule. A dedicated fixed-cadence `LiveEnvelopeSync` (modelled on `StreamHealthHoldSync`) reads the live account and positions, publishes an in-memory `AccountObservation`, composes the day-P&L fact, and raises the loss hold as a third registered account-hold cause on the existing uncertainty path (blocks new exposure, allows reduction). `accept_enter` gains an envelope sibling of `require_admission` that reads the fresh observation plus durable per-ENTER cash reservations and writes its own reservation atomically with `ENTER_ACCEPTED`. The guarded clear re-observes and refuses while the breach stands.

**Tech Stack:** Python 3.12, FastAPI, SQLite custody spine (`app/broker/alpaca/clerk/sqlite`), Pydantic v2, Angular 22 (Vitest), OpenAPI contract + codegen.

**Spec:** `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` Decision 4 (the envelope), Decision 8 (verdict fields `envelope_agreement`, `envelope_state`), and `CONTEXT.md` § "Live account, shadow, and risk envelope". The design rulings below are the owner's answers of 2026-09-08 and the controller's rulings; they bind where the ADR is silent.

## Design rulings (binding)

- **R1 (owner 2026-09-08) — cash rule.** An ENTER is admitted iff `notional + reserved ≤ cash_available`, where `notional = quantity × price`, `reserved` = the unfilled remainder of every working ENTER across all instances (priced at each ENTER's own reference price) plus fills the observed cash cannot yet reflect, and `cash_available` is broker-observed `cash`. Not "settled positions + new order ≤ cash". Never buying power.
- **R2 (owner 2026-09-08) — shadow.** The envelope is evaluated under the shadow authority exactly as it will be under `real_live`. Under simulated custody, `cash_available = broker cash − net cash the Clerk's own fills would have spent` (Σ BUY notional − Σ SELL notional over the Clerk's effective fills), so synthesized fills consume rehearsal cash the way real fills consume real cash.
- **R3 (owner 2026-09-08) — start-of-session equity.** `last_equity` from the broker account snapshot (Alpaca's own previous-close equity). `None` ⇒ the loss limit is unknown ⇒ no hold is raised and every ENTER is refused `LIVE_ENVELOPE_UNOBSERVED` (fail closed).
- **R4 — loss limit.** `loss_limit_usd = min(loss_fraction × last_equity, loss_usd)`; breached iff `day_pnl ≤ −loss_limit_usd`.
- **R5 — day P&L.** `realized` = Clerk FIFO realized P&L over every custody subject in `[ET midnight of the current ET date, now]` (`account_pnl_attribution`), `fees` = the attribution's `fee_total` when reported else 0 with `fee_fidelity` carried, `unrealized` = Σ broker-observed `unrealized_pl` over every open position. Day P&L is **unknown** when an external (non-Clerk) order was observed today — its realized P&L is not journaled. Unknown raises no hold and refuses ENTER `LIVE_ENVELOPE_UNOBSERVED`.
- **R6 — the loss hold is a registered account-hold cause** (`LIVE_ENVELOPE_LOSS_HOLD`, ADR 0048 D2 path): `blocks_new_exposure=True`, `allows_reduction=True`, `age=CauseCleared()`. Raised only by `LiveEnvelopeSync`; never released by it; released only by `clear_loss_hold`. `decide_capability`'s REDUCE branch admits every reduction under it.
- **R7 — every envelope refusal is TRANSIENT** at the runner (`TRANSIENT_ADMISSION_REASON_CODES`): a refused bot records a `blocked` receipt and retries next clock; it is never halted or paused (ADR 0059 rejected "pause the bot that tripped").
- **R8 — no broker contact in `accept_enter`.** The observation is in-memory on `LiveEnvelopeGate`, published by the sync from `repo.clock()`; older than `OBSERVATION_MAX_AGE_MS = 45_000` (3 × the 15 s cadence) ⇒ `LIVE_ENVELOPE_UNOBSERVED`. A restart refuses until the first tick.
- **R9 — reservations are durable, outside the hash chain.** Table `envelope_reservations`, written in the same SQLite transaction as `ENTER_ACCEPTED` (the decision-receipt precedent), never in `facts_json`, never in the transition payload, so every row hash is unchanged. Schema v13.
- **R10 — reference price.** LIMIT leg ⇒ `leg.limit_price`; MARKET leg ⇒ the decision bar close the runtime already holds (`retained_source_bar.close`); none ⇒ `LIVE_ENVELOPE_UNOBSERVED`. Market slippage above the decision close is a disclosed residual; Alpaca's own cash-account check is the backstop.
- **R11 — envelope agreement.** `LiveEnvelopeValues` (six `ALPACA_LIVE_*` values) and its `canonical_sha256`; `envelope_agreement(configured, sealed)` is `unsealed` (no arming record yet — every slice-5 path), `agreed`, or `disagreed` ⇒ `LIVE_ENVELOPE_DISAGREEMENT` refuses ENTER. Slice 6 supplies `sealed`.
- **R12 — the guarded clear** is `POST /api/brokers/alpaca/live-envelope/loss-hold/clear` behind the data-plane control secret (the manual-order precedent). It re-observes, refuses while breached or unknown, resolves the hold otherwise. No CLI (an out-of-process writer cannot hold the execution lease). No Frontend button this slice; the banner shows the hold and the detail names the action.
- **R13 — fail closed at composition.** A shadow authority composed without `LiveEnvelopeValues` is `unavailable` (`LIVE_ENVELOPE_MISSING`). Paper and synthetic authorities carry no envelope.
- **R14 — verdict.** `AlpacaLiveVerdict` gains `envelope_agreement` and `loss_hold` (required fields; contract regenerated). Copy is backend-authored.

## Global Constraints

- Never commit secrets; `.env` only. `.env.example` already lists every `ALPACA_LIVE_*` name — do not add values.
- Never edit sealed artifacts: `app/lean_sidecar/trading_calendar.py`, `app/utils/timestamps.py`, `app/utils/session_anchors.py`, `app/engine/consolidators/trade_bar_consolidator.py`, anything in `registry.py`'s `artifact_paths`. Importing from them is fine.
- No `facts.py` / hash-chained custody model changes. `TransitionInput` (unhashed planner input) may gain a field; the transition payload and every `facts_json` shape stay byte-identical.
- The live TRADE port is never bound to any authority (shadow keeps `NoSubmitAlpacaTradePort`).
- Explicit-path staging only (never `git add -A`; shared checkout). Never self-review. Pushing/PRs allowed; merging `origin/master` is the owner's.
- No silent exception handlers. Structured logging (`extra={"action": ...}`), no `print`.
- Temporal rigor: `int64 ms UTC`; every stamp from `repo.clock()` or the caller's `now_ms`; calendar/ET helpers only from `app/utils/session_anchors.py` (`et_date_at_ms`, `et_midnight_ms`); no wall-clock reads in tests; `MAX_TIMESTAMP_MS` for schema ceilings.
- No file may cross 1,000 lines. `economic_projection.py` (1508) and `sqlite_panel_source.py` (1049) are already over: minimal additions only (one short method).
- Reason codes SCREAMING_SNAKE, prefixed `LIVE_ENVELOPE_`.
- Python commands: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <paths> -q -p no:cacheprovider`; lint `.venv/bin/ruff check app/ tests/ scripts/`; contract `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check` (regenerate without `--check` after a schema change, then `cd /Users/inkant/learn-ai/Frontend && npm run codegen:openapi`). Frontend specs: `npx ng test --include='<exact spec path>'` (never a glob).
- Known pre-existing flake to ignore: none on master after #2009.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `app/broker/alpaca/clerk/live_envelope.py` (new) | Pure envelope model: values + sha, agreement, observation, reservation, cash rule, loss rule, reason codes, `LiveEnvelopeGate`. |
| `app/broker/alpaca/clerk/et_day.py` (new) | `et_day_window_ms(at_ms)` — the ET calendar day window, lifted from `alpaca_fee_reconciliation.py`. |
| `app/broker/alpaca/clerk/sqlite/uncertainty_causes.py` | `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE`, `LossHoldCause`, `HOLD_REASON_CODES` membership. |
| `app/broker/alpaca/clerk/sqlite/uncertainty_policies.py` | The loss-hold `ReasonPolicy`. |
| `app/broker/alpaca/clerk/sqlite/uncertainty_folds.py` | Loss-hold operator envelope copy. |
| `app/broker/alpaca/clerk/sqlite/uncertainty.py` | `raise_account_hold(cause_facts=)`, REDUCE admission under the loss hold, transient codes. |
| `app/broker/alpaca/clerk/sqlite/schema.py` | `envelope_reservations` table, v13 migration. |
| `app/broker/alpaca/clerk/sqlite/envelope_reservations.py` (new) | Reservation row write + `reserved_cash_usd`. |
| `app/broker/alpaca/clerk/sqlite/models.py` | `TransitionInput.envelope_reservation`. |
| `app/broker/alpaca/clerk/sqlite/repository.py`, `repository_read_api.py`, `reads.py` | Atomic reservation insert; `reserved_cash_usd`, `external_orders_observed_since`. |
| `app/broker/alpaca/clerk/sqlite/envelope_admission.py` (new) | `require_envelope_admission`. |
| `app/broker/alpaca/clerk/sqlite/enter.py` | `accept_enter(envelope=, reference_price=)`. |
| `app/broker/alpaca/clerk/sqlite/day_pnl.py` (new) | `DayPnl`, `day_pnl_at`. |
| `app/broker/alpaca/clerk/sqlite/economic_projection.py` | `account_net_cash_spent_usd` (minimal). |
| `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py` (new) | `LiveEnvelopeSync`, `EnvelopeReading`. |
| `app/broker/alpaca/clerk/sqlite/runtime.py`, `active_runtime.py`, `shadow_authority.py`, `active_authority.py`, `app/main.py` | Composition. |
| `app/services/alpaca_live_envelope.py` (new), `app/schemas/alpaca_live_envelope.py` (new), `app/routers/brokers.py` | Guarded clear. |
| `app/schemas/alpaca_live_verdict.py`, `app/services/alpaca_live_verdict.py`, `app/routers/brokers.py` | Verdict widening. |
| `Frontend/src/app/shell/alpaca-live-banner.component.ts` (+spec), `Frontend/src/app/services/alpaca-live-verdict.service.spec.ts`, `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-emergency-copy.ts`, `…/broker-v2-vocabulary.snapshot.json` | Display + closed copy. |
| `docs/references/alpaca-live-envelope.md` (new), `docs/math-sources-of-truth.md`, `docs/architecture/engine-authority-map.md`, `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`, `CONTEXT.md` | Docs. |

---

### Task 1: Pure envelope model and the ET-day helper

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/live_envelope.py`
- Create: `PythonDataService/app/broker/alpaca/clerk/et_day.py`
- Modify: `PythonDataService/app/services/alpaca_fee_reconciliation.py:69-72` (delete `_et_day_window_ms`, import `et_day_window_ms`; update its two callers at ~129 and ~343)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_live_envelope.py`, `PythonDataService/tests/broker/alpaca/clerk/test_et_day.py`

**Interfaces:**
- Consumes: `app.broker.alpaca.clerk.sealed_ledger.canonical_sha256(obj) -> str`; `app.utils.session_anchors.et_date_at_ms`, `et_midnight_ms`; `app.broker.alpaca.config.AlpacaSettings` (fields `live_loss_fraction`, `live_loss_usd`, `live_shadow_sessions`, `live_arming_max_sessions`, `live_xh_entry_bps`, `live_xh_exit_bps`, all `| None`).
- Produces (later tasks rely on these exact names):
  - constants `LIVE_ENVELOPE_CASH_EXCEEDED`, `LIVE_ENVELOPE_DISAGREEMENT`, `LIVE_ENVELOPE_UNOBSERVED` (strings equal to their names), `ENVELOPE_ADMISSION_REASON_CODES: frozenset[str]` (those three plus `"LIVE_ENVELOPE_LOSS_HOLD"`), `ENVELOPE_SYNC_INTERVAL_S = 15.0`, `OBSERVATION_MAX_AGE_MS = 45_000`
  - `class LiveEnvelopeIncomplete(ValueError)`
  - `@dataclass(frozen=True) class LiveEnvelopeValues(loss_fraction: float, loss_usd: float, shadow_sessions: int, arming_max_sessions: int, xh_entry_bps: float, xh_exit_bps: float)` with `@classmethod from_settings(cls, settings) -> LiveEnvelopeValues`, `to_mapping() -> dict[str, float | int]`, `@property sha -> str`
  - `EnvelopeAgreement = Literal["unsealed", "agreed", "disagreed"]`; `envelope_agreement(configured: LiveEnvelopeValues, sealed: LiveEnvelopeValues | None) -> EnvelopeAgreement`
  - `@dataclass(frozen=True) class AccountObservation(observed_at_ms: int, broker_cash_usd: float, cash_available_usd: float, last_equity_usd: float | None, unrealized_pl_usd: float, position_count: int)`
  - `@dataclass(frozen=True) class EnvelopeReservation(quantity: float, reference_price: float)` with `@property notional_usd -> float`
  - `loss_limit_usd(values: LiveEnvelopeValues, *, last_equity_usd: float) -> float`; `loss_breached(*, day_pnl_usd: float, loss_limit_usd: float) -> bool`; `cash_bound_admits(*, cash_available_usd: float, reserved_usd: float, notional_usd: float) -> bool`
  - `class LiveEnvelopeGate` — `__init__(self, *, values, sealed=None, custody_is_simulated: bool, observation_max_age_ms: int = OBSERVATION_MAX_AGE_MS)`; attributes `values`, `sealed`, `custody_is_simulated`; `@property agreement -> EnvelopeAgreement`; `publish(observation: AccountObservation) -> None`; `latest_observation() -> AccountObservation | None`; `fresh_observation(now_ms: int) -> AccountObservation | None`
  - `et_day_window_ms(at_ms: int) -> tuple[int, int]` — `[ET midnight of the ET date containing at_ms, next ET midnight)`

- [ ] **Step 1: Write the failing tests**

`tests/broker/alpaca/clerk/test_live_envelope.py`:

```python
from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_ADMISSION_REASON_CODES,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    EnvelopeReservation,
    LiveEnvelopeGate,
    LiveEnvelopeIncomplete,
    LiveEnvelopeValues,
    cash_bound_admits,
    envelope_agreement,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.config import AlpacaSettings

VALUES = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=3,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)


def _observation(observed_at_ms: int, *, cash: float = 100_000.0) -> AccountObservation:
    return AccountObservation(
        observed_at_ms=observed_at_ms,
        broker_cash_usd=cash,
        cash_available_usd=cash,
        last_equity_usd=100_000.0,
        unrealized_pl_usd=0.0,
        position_count=0,
    )


def test_the_sha_is_stable_and_changes_with_any_value() -> None:
    same = LiveEnvelopeValues(**VALUES.to_mapping())
    assert same.sha == VALUES.sha
    assert len(VALUES.sha) == 64
    changed = LiveEnvelopeValues(**{**VALUES.to_mapping(), "loss_usd": 5_000.01})
    assert changed.sha != VALUES.sha


def test_from_settings_reads_every_live_value_and_names_the_missing_ones() -> None:
    settings = AlpacaSettings(
        ALPACA_API_KEY="k",
        ALPACA_SECRET_KEY="s",
        ALPACA_MODE="live",
        ALPACA_LIVE_LOSS_FRACTION=0.05,
        ALPACA_LIVE_LOSS_USD=5000,
        ALPACA_LIVE_SHADOW_SESSIONS=3,
        ALPACA_LIVE_ARMING_MAX_SESSIONS=20,
        ALPACA_LIVE_XH_ENTRY_BPS=10,
        ALPACA_LIVE_XH_EXIT_BPS=10,
    )
    assert LiveEnvelopeValues.from_settings(settings) == VALUES
    paper = AlpacaSettings(ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_MODE="paper")
    with pytest.raises(LiveEnvelopeIncomplete, match="live_loss_fraction"):
        LiveEnvelopeValues.from_settings(paper)


def test_agreement_is_unsealed_agreed_or_disagreed() -> None:
    assert envelope_agreement(VALUES, None) == "unsealed"
    assert envelope_agreement(VALUES, LiveEnvelopeValues(**VALUES.to_mapping())) == "agreed"
    other = LiveEnvelopeValues(**{**VALUES.to_mapping(), "loss_fraction": 0.04})
    assert envelope_agreement(VALUES, other) == "disagreed"


def test_the_loss_limit_is_the_tighter_of_fraction_and_usd() -> None:
    assert loss_limit_usd(VALUES, last_equity_usd=100_000.0) == pytest.approx(5_000.0)
    assert loss_limit_usd(VALUES, last_equity_usd=40_000.0) == pytest.approx(2_000.0)
    assert loss_breached(day_pnl_usd=-2_000.0, loss_limit_usd=2_000.0)
    assert not loss_breached(day_pnl_usd=-1_999.99, loss_limit_usd=2_000.0)


def test_the_cash_rule_counts_the_new_order_and_working_reservations_only() -> None:
    assert cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=0.0, notional_usd=10_000.0)
    assert not cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=0.01, notional_usd=10_000.0)
    assert cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=4_000.0, notional_usd=6_000.0)
    assert EnvelopeReservation(quantity=10, reference_price=12.5).notional_usd == pytest.approx(125.0)


def test_the_gate_serves_only_a_fresh_observation() -> None:
    gate = LiveEnvelopeGate(values=VALUES, custody_is_simulated=True)
    assert gate.agreement == "unsealed"
    assert gate.latest_observation() is None
    assert gate.fresh_observation(1_000) is None
    gate.publish(_observation(1_000))
    assert gate.fresh_observation(1_000 + OBSERVATION_MAX_AGE_MS) is not None
    assert gate.fresh_observation(1_000 + OBSERVATION_MAX_AGE_MS + 1) is None
    assert gate.latest_observation() == _observation(1_000)


def test_the_admission_reason_codes_are_the_four_envelope_refusals() -> None:
    assert ENVELOPE_ADMISSION_REASON_CODES == frozenset(
        {
            "LIVE_ENVELOPE_CASH_EXCEEDED",
            "LIVE_ENVELOPE_LOSS_HOLD",
            "LIVE_ENVELOPE_DISAGREEMENT",
            "LIVE_ENVELOPE_UNOBSERVED",
        }
    )
```

`tests/broker/alpaca/clerk/test_et_day.py`:

```python
from __future__ import annotations

from datetime import date

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.utils.session_anchors import et_midnight_ms, et_minute_of_day_ms


def test_the_window_is_the_et_calendar_day_containing_the_instant() -> None:
    noon_et = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
    start, end = et_day_window_ms(noon_et)
    assert start == et_midnight_ms(date(2026, 9, 8))
    assert end == et_midnight_ms(date(2026, 9, 9))
    # 23:30 ET on 2026-09-08 is 03:30 UTC on 2026-09-09 and still the ET 8th.
    late = et_minute_of_day_ms(date(2026, 9, 8), 23 * 60 + 30)
    assert et_day_window_ms(late) == (start, end)
```

Check `AlpacaSettings`'s constructor field aliases before writing the settings test: `sed -n 57,125p app/broker/alpaca/config.py`. If the model reads only from the environment (no alias construction), build it with `monkeypatch.setenv` for each `ALPACA_*` name and `AlpacaSettings()` instead.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_envelope.py tests/broker/alpaca/clerk/test_et_day.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: app.broker.alpaca.clerk.live_envelope`.

- [ ] **Step 3: Write `et_day.py` and switch the fee reconciliation to it**

```python
"""The ET calendar day that contains one instant (ADR 0022 anchors).

One helper, two consumers: fee reconciliation bills a trade date on its ET
calendar day, and the live envelope's day P&L is "today" on the same day.
"""

from __future__ import annotations

from datetime import timedelta

from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

_ONE_DAY = timedelta(days=1)


def et_day_window_ms(at_ms: int) -> tuple[int, int]:
    """``[ET midnight, next ET midnight)`` of the ET date containing ``at_ms``."""
    trade_date = et_date_at_ms(at_ms)
    return et_midnight_ms(trade_date), et_midnight_ms(trade_date + _ONE_DAY)


__all__ = ["et_day_window_ms"]
```

In `alpaca_fee_reconciliation.py`: delete `_et_day_window_ms` and `_ONE_DAY`, add `from app.broker.alpaca.clerk.et_day import et_day_window_ms`, replace both `_et_day_window_ms(session_open_ms)` calls with `et_day_window_ms(session_open_ms)`, and drop now-unused imports (`timedelta`, `et_midnight_ms`) — ruff will flag them.

- [ ] **Step 4: Write `live_envelope.py`**

```python
"""The ADR 0059 risk envelope, as pure values and rules (Decision 4).

Formula: cash bound admits iff ``notional + reserved <= cash_available``;
  loss limit ``L = min(loss_fraction × last_equity, loss_usd)``; loss breached
  iff ``day_pnl <= −L``.
Reference: ADR 0059 Decision 4; owner rulings 2026-09-08 (plan R1–R4).
Canonical implementation: this file. Day P&L composition lives in
  ``app/broker/alpaca/clerk/sqlite/day_pnl.py``.
Validated against: ``tests/broker/alpaca/clerk/test_live_envelope.py``.

Nothing here touches a broker, a database, or a clock: the sync publishes
an ``AccountObservation`` it stamped with the repository clock, and the
admission seam asks the gate for one that is fresh at *its* ``now_ms``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
)

if TYPE_CHECKING:
    from app.broker.alpaca.config import AlpacaSettings

LIVE_ENVELOPE_CASH_EXCEEDED = "LIVE_ENVELOPE_CASH_EXCEEDED"
LIVE_ENVELOPE_DISAGREEMENT = "LIVE_ENVELOPE_DISAGREEMENT"
LIVE_ENVELOPE_UNOBSERVED = "LIVE_ENVELOPE_UNOBSERVED"
ENVELOPE_ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        LIVE_ENVELOPE_CASH_EXCEEDED,
        LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        LIVE_ENVELOPE_DISAGREEMENT,
        LIVE_ENVELOPE_UNOBSERVED,
    }
)
ENVELOPE_SYNC_INTERVAL_S = 15.0
# Three sync intervals: one missed tick is a blip, two is an outage the
# admission seam must not trade through.
OBSERVATION_MAX_AGE_MS = 45_000
_CASH_EPSILON_USD = 1e-9

EnvelopeAgreement = Literal["unsealed", "agreed", "disagreed"]

_SETTINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("loss_fraction", "live_loss_fraction"),
    ("loss_usd", "live_loss_usd"),
    ("shadow_sessions", "live_shadow_sessions"),
    ("arming_max_sessions", "live_arming_max_sessions"),
    ("xh_entry_bps", "live_xh_entry_bps"),
    ("xh_exit_bps", "live_xh_exit_bps"),
)


class LiveEnvelopeIncomplete(ValueError):
    """A live envelope cannot be built: at least one ``ALPACA_LIVE_*`` value is absent."""


@dataclass(frozen=True)
class LiveEnvelopeValues:
    loss_fraction: float
    loss_usd: float
    shadow_sessions: int
    arming_max_sessions: int
    xh_entry_bps: float
    xh_exit_bps: float

    @classmethod
    def from_settings(cls, settings: AlpacaSettings) -> LiveEnvelopeValues:
        missing = [name for _, name in _SETTINGS_FIELDS if getattr(settings, name) is None]
        if missing:
            raise LiveEnvelopeIncomplete(
                "the live envelope needs every ALPACA_LIVE_* value; missing: " + ", ".join(missing)
            )
        return cls(**{field: getattr(settings, name) for field, name in _SETTINGS_FIELDS})

    def to_mapping(self) -> dict[str, float | int]:
        return asdict(self)

    @property
    def sha(self) -> str:
        return canonical_sha256(self.to_mapping())


def envelope_agreement(
    configured: LiveEnvelopeValues, sealed: LiveEnvelopeValues | None
) -> EnvelopeAgreement:
    if sealed is None:
        return "unsealed"
    return "agreed" if sealed.sha == configured.sha else "disagreed"


@dataclass(frozen=True)
class AccountObservation:
    """One broker read the sync published, stamped with the repository clock."""

    observed_at_ms: int
    broker_cash_usd: float
    # ``broker_cash_usd`` less what the Clerk's own fills would have spent
    # under simulated custody (plan R2); equal to it under real custody.
    cash_available_usd: float
    last_equity_usd: float | None
    unrealized_pl_usd: float
    position_count: int


@dataclass(frozen=True)
class EnvelopeReservation:
    """The cash one accepted ENTER claims until its fills are observed."""

    quantity: float
    reference_price: float

    @property
    def notional_usd(self) -> float:
        return self.quantity * self.reference_price


def loss_limit_usd(values: LiveEnvelopeValues, *, last_equity_usd: float) -> float:
    return min(values.loss_fraction * last_equity_usd, values.loss_usd)


def loss_breached(*, day_pnl_usd: float, loss_limit_usd: float) -> bool:
    return day_pnl_usd <= -loss_limit_usd


def cash_bound_admits(*, cash_available_usd: float, reserved_usd: float, notional_usd: float) -> bool:
    return notional_usd + reserved_usd <= cash_available_usd + _CASH_EPSILON_USD


class LiveEnvelopeGate:
    """The envelope one authority admits ENTERs against.

    Holds the configured values, the sealed values once an arming record
    exists (slice 6), and the latest observation the sync published. A
    process-local cache of a broker read, never a custody fact (the
    ``_last_published`` precedent in ``runtime.py``).
    """

    def __init__(
        self,
        *,
        values: LiveEnvelopeValues,
        sealed: LiveEnvelopeValues | None = None,
        custody_is_simulated: bool,
        observation_max_age_ms: int = OBSERVATION_MAX_AGE_MS,
    ) -> None:
        self.values = values
        self.sealed = sealed
        self.custody_is_simulated = custody_is_simulated
        self._max_age_ms = observation_max_age_ms
        self._observation: AccountObservation | None = None

    @property
    def agreement(self) -> EnvelopeAgreement:
        return envelope_agreement(self.values, self.sealed)

    def publish(self, observation: AccountObservation) -> None:
        self._observation = observation

    def latest_observation(self) -> AccountObservation | None:
        return self._observation

    def fresh_observation(self, now_ms: int) -> AccountObservation | None:
        observation = self._observation
        if observation is None or now_ms - observation.observed_at_ms > self._max_age_ms:
            return None
        return observation


__all__ = [
    "ENVELOPE_ADMISSION_REASON_CODES",
    "ENVELOPE_SYNC_INTERVAL_S",
    "LIVE_ENVELOPE_CASH_EXCEEDED",
    "LIVE_ENVELOPE_DISAGREEMENT",
    "LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE",
    "LIVE_ENVELOPE_UNOBSERVED",
    "OBSERVATION_MAX_AGE_MS",
    "AccountObservation",
    "EnvelopeAgreement",
    "EnvelopeReservation",
    "LiveEnvelopeGate",
    "LiveEnvelopeIncomplete",
    "LiveEnvelopeValues",
    "cash_bound_admits",
    "envelope_agreement",
    "loss_breached",
    "loss_limit_usd",
]
```

`LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE` does not exist yet — Task 2 adds it. For this task add to `uncertainty_causes.py`, directly under `STREAM_HEALTH_HOLD_REASON_CODE`, the single line `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE = "LIVE_ENVELOPE_LOSS_HOLD"` and export it in that module's `__all__`; Task 2 does the rest (cause class, `HOLD_REASON_CODES`).

- [ ] **Step 5: Run the tests to verify they pass, plus the fee suite**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_envelope.py tests/broker/alpaca/clerk/test_et_day.py tests/services/test_alpaca_fee_reconciliation.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
(If the fee test module lives elsewhere, find it with `grep -rl "alpaca_fee_reconciliation" tests`.)
Expected: all pass; ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/live_envelope.py PythonDataService/app/broker/alpaca/clerk/et_day.py PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_causes.py PythonDataService/app/services/alpaca_fee_reconciliation.py PythonDataService/tests/broker/alpaca/clerk/test_live_envelope.py PythonDataService/tests/broker/alpaca/clerk/test_et_day.py && git commit -m "feat(envelope): the live envelope's values, agreement, cash rule and loss rule as pure code

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The loss hold as a registered account-hold cause

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_causes.py` (`LossHoldCause`, `HOLD_REASON_CODES`, `_HOLD_REASON_CODE_NORMALISATION`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_policies.py:150-235` (`_REASON_POLICIES` entry)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_folds.py:52-100` (`account_hold_envelope` branch + `cause_facts` parameter)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py:172-206` (`raise_account_hold(cause_facts=)`), `:520-590` (REDUCE branch), `:650-670` (`TRANSIENT_ADMISSION_REASON_CODES`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_loss_hold.py`

**Interfaces:**
- Consumes: `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE` (Task 1), `ENVELOPE_ADMISSION_REASON_CODES` (Task 1), `raise_account_hold`, `resolve_account_hold`, `decide_capability`, `Capability`, `ReductionIntent`, `classify_admission_refusal`, `RefusalClass`.
- Produces: `LossHoldCause(day_start_ms: int, day_pnl_usd: float, loss_limit_usd: float, last_equity_usd: float, observed_at_ms: int)` with `to_mapping()` / `from_mapping(value)` (strict: every key present, ints ≥ 0, floats finite; `ValueError` otherwise); `raise_account_hold(repo, *, reason_code, evidence_refs, provenance=..., cause_facts: Mapping[str, Any] | None = None) -> str`; the loss hold blocks `NEW_EXPOSURE` and admits every `REDUCE`; `LIVE_ENVELOPE_LOSS_HOLD` ∈ `HOLD_REASON_CODES`; every envelope code classifies `TRANSIENT`.

- [ ] **Step 1: Read the existing hold tests for the raise/decide harness**

Run: `sed -n 1,80p tests/broker/alpaca/clerk/sqlite/test_uncertainty.py` and `grep -n "raise_account_hold\|REDUCE\|ReductionIntent(" tests/broker/alpaca/clerk/sqlite/test_uncertainty.py | head`. Reuse that file's repo fixture shape (a `ClerkSqliteRepository.initialize(account_id=..., artifacts_root=tmp_path, clock=...)` with a fixed clock) and its way of registering a strategy instance and a position for a REDUCE check.

- [ ] **Step 2: Write the failing tests**

```python
from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.live_envelope import ENVELOPE_ADMISSION_REASON_CODES
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    RefusalClass,
    classify_admission_refusal,
    decide_capability,
    raise_account_hold,
    resolve_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODES,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)

CAUSE = LossHoldCause(
    day_start_ms=1_788_000_000_000,
    day_pnl_usd=-5_250.0,
    loss_limit_usd=5_000.0,
    last_equity_usd=100_000.0,
    observed_at_ms=1_788_040_000_000,
)


def test_the_cause_round_trips_and_refuses_partial_or_non_finite_facts() -> None:
    assert LossHoldCause.from_mapping(CAUSE.to_mapping()) == CAUSE
    with pytest.raises(ValueError):
        LossHoldCause.from_mapping({**CAUSE.to_mapping(), "day_pnl_usd": float("nan")})
    with pytest.raises(ValueError):
        LossHoldCause.from_mapping({k: v for k, v in CAUSE.to_mapping().items() if k != "observed_at_ms"})


def test_the_loss_hold_is_a_hold_cause_and_every_envelope_refusal_is_transient() -> None:
    assert LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE in HOLD_REASON_CODES
    for code in ENVELOPE_ADMISSION_REASON_CODES:
        assert classify_admission_refusal(code) is RefusalClass.TRANSIENT


def test_a_raised_loss_hold_blocks_new_exposure_and_admits_every_reduction(repo, registered_long) -> None:
    # `registered_long`: a fixture from Step 1's harness giving
    # (strategy_instance_id, ReductionIntent that sells part of a long it holds).
    strategy_instance_id, intent = registered_long
    outcome = raise_account_hold(
        repo,
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        evidence_refs=[f"day-pnl:{CAUSE.day_start_ms}"],
        cause_facts=CAUSE.to_mapping(),
    )
    assert outcome == "raised"
    entry = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=strategy_instance_id)
    assert not entry.allowed and entry.reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
    reduce = decide_capability(
        repo, capability=Capability.REDUCE, strategy_instance_id=strategy_instance_id, reduction_intent=intent
    )
    assert reduce.allowed


def test_an_unchanged_raise_appends_nothing_and_the_clear_resolves_it(repo) -> None:
    raise_account_hold(
        repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, evidence_refs=["day-pnl:1"], cause_facts=CAUSE.to_mapping()
    )
    before = repo.control_meta_snapshot().control_revision
    assert (
        raise_account_hold(
            repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, evidence_refs=["day-pnl:1"], cause_facts=CAUSE.to_mapping()
        )
        == "unchanged"
    )
    assert repo.control_meta_snapshot().control_revision == before
    assert resolve_account_hold(
        repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, summary_code="LIVE_ENVELOPE_LOSS_HOLD_CLEARED"
    )
    assert repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is None


def test_raising_the_loss_hold_without_its_facts_is_a_programming_error(repo) -> None:
    with pytest.raises(ValueError, match="cause_facts"):
        raise_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, evidence_refs=["day-pnl:1"])
```

Write the `repo` and `registered_long` fixtures at the top of the file from the Step 1 harness (a registered instance with an ACTIVE run and a BUY fill folded so `repo.position(sid, "SPY") > 0`, and `ReductionIntent(symbol="SPY", side="SELL", quantity=1)` — check `ReductionIntent`'s fields with `grep -n "class ReductionIntent" -A 12 app/broker/alpaca/clerk/sqlite/uncertainty.py`).

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_loss_hold.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: LossHoldCause`).

- [ ] **Step 4: Implement**

`uncertainty_causes.py` — beside `StreamHealthHoldCause`:

```python
@dataclass(frozen=True)
class LossHoldCause:
    """The day-P&L breach that put the account in loss hold (ADR 0059 D4).

    Stamped once at the raise; the sync never refreshes a standing hold, so
    the stored cause is the breach the operator has to look at, not the
    latest tick.
    """

    day_start_ms: int
    day_pnl_usd: float
    loss_limit_usd: float
    last_equity_usd: float
    observed_at_ms: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "day_start_ms": self.day_start_ms,
            "day_pnl_usd": self.day_pnl_usd,
            "loss_limit_usd": self.loss_limit_usd,
            "last_equity_usd": self.last_equity_usd,
            "observed_at_ms": self.observed_at_ms,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> LossHoldCause:
        if not isinstance(value, dict):
            raise ValueError("loss hold cause must be a mapping")
        try:
            day_start_ms = value["day_start_ms"]
            observed_at_ms = value["observed_at_ms"]
            floats = {k: value[k] for k in ("day_pnl_usd", "loss_limit_usd", "last_equity_usd")}
        except KeyError as exc:
            raise ValueError(f"loss hold cause is missing {exc.args[0]!r}") from exc
        for name, stamp in (("day_start_ms", day_start_ms), ("observed_at_ms", observed_at_ms)):
            if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
                raise ValueError(f"loss hold cause {name} must be a non-negative int64 ms")
        for name, number in floats.items():
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(f"loss hold cause {name} must be a finite number")
        return cls(
            day_start_ms=day_start_ms,
            day_pnl_usd=float(floats["day_pnl_usd"]),
            loss_limit_usd=float(floats["loss_limit_usd"]),
            last_equity_usd=float(floats["last_equity_usd"]),
            observed_at_ms=observed_at_ms,
        )
```

Add `import math` if absent. Extend `HOLD_REASON_CODES` to the three codes and add the identity row `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE` to `_HOLD_REASON_CODE_NORMALISATION`. Export `LossHoldCause`.

`uncertainty_policies.py` — add beside the two hold policies:

```python
    # ADR 0059 D4: the loss hold refuses entries account-wide and lets every
    # program keep managing its own position. It clears only by the guarded
    # operator action, never on a timer and never at session rollover.
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_loss_hold_cause_is_valid,
        age=CauseCleared(),
    ),
```

with `_loss_hold_cause_is_valid` written like `_stream_health_hold_cause_is_valid` over `LossHoldCause.from_mapping`.

`uncertainty_folds.py` — `account_hold_envelope(*, reason_code: str, evidence_refs: list[str], cause_facts: Mapping[str, Any] | None = None)`; add the branch:

```python
    elif reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE:
        if cause_facts is None:
            raise ValueError("the loss hold needs cause_facts: the breach it was raised on")
        loss = LossHoldCause.from_mapping(cause_facts)
        cause = loss.to_mapping()
        headline = "The account is in loss hold"
        explanation = (
            f"Today's P&L reached {loss.day_pnl_usd:.2f} USD against a loss limit of "
            f"{loss.loss_limit_usd:.2f} USD. Every ENTER on the account is refused; every "
            "EXIT still runs, so each program keeps managing its own position."
        )
        operator_impact = "New submits are paused account-wide; exits are unaffected."
        next_step = (
            "When the account has recovered, clear the hold with the guarded operator action "
            "(POST /api/brokers/alpaca/live-envelope/loss-hold/clear). It does not clear at "
            "session rollover."
        )
```

`uncertainty.py`:
- `raise_account_hold(..., cause_facts: Mapping[str, Any] | None = None)` passes `cause_facts=cause_facts` to `account_hold_envelope`.
- In `decide_capability`'s REDUCE disjunction append, after the `EXIT_STUCK` branch and inside the same parenthesised `or` group: `or reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE`.
- `TRANSIENT_ADMISSION_REASON_CODES = frozenset({...}) | ENVELOPE_ADMISSION_REASON_CODES` (import from `app.broker.alpaca.clerk.live_envelope`), and extend the comment above it with one sentence: envelope refusals retry on the next decision clock because the ADR forbids halting or pausing a bot for an account-scoped fact.

Check `insert_account_hold_episode` and `hold_migration.py`: neither may call `account_hold_envelope` for the loss hold without facts — they only ever see the two legacy causes; leave them.

- [ ] **Step 5: Run the tests and the neighbouring suites**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_loss_hold.py tests/broker/alpaca/clerk/sqlite/test_uncertainty.py tests/broker/alpaca/clerk/sqlite/test_v12_holds_to_uncertainties.py tests/broker/alpaca/clerk/sqlite/test_stream_health_sync.py tests/broker/alpaca/clerk/test_hold_debounce.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass. If a test pins `len(HOLD_REASON_CODES) == 2` or the SQL placeholder count, update it to three with a one-line comment naming ADR 0059 D4.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_causes.py PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_policies.py PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty_folds.py PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_loss_hold.py && git add -u PythonDataService/tests/broker/alpaca/clerk/sqlite/ && git commit -m "feat(envelope): the loss hold is a registered account-hold cause that admits every exit

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Durable cash reservations (schema v13) written atomically with `ENTER_ACCEPTED`

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_reservations.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/schema.py:38` (`SCHEMA_VERSION = 13`), the base DDL (new table after `decision_receipts`), `SCHEMA_MIGRATIONS` (key `12`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/models.py:17-40` (`TransitionInput.envelope_reservation`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/repository.py:446-560` (`append_transition` → `_commit_transition_row`), `:905-945`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/repository_read_api.py` (+ `reads.py` if that file is where read SQL lives) — `reserved_cash_usd`
- Modify: `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` (DDL mirror + table count)
- Modify: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_schema_parity.py:62` (`twenty_one` → `twenty_two`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py`

**Interfaces:**
- Consumes: `EnvelopeReservation` (Task 1).
- Produces: `TransitionInput.envelope_reservation: EnvelopeReservation | None = None`; `append_envelope_reservation_row(conn, *, effect_operation_id: str, reservation: EnvelopeReservation, reserved_at_ms: int) -> None`; `reserved_cash_usd(conn, *, observed_at_ms: int) -> float`; repository method `ClerkSqliteRepository.reserved_cash_usd(*, observed_at_ms: int) -> float`.

- [ ] **Step 1: Learn how the pinned-contracts doc mirrors the DDL and how older-version databases are built in tests**

Run: `sed -n 40,100p tests/broker/alpaca/clerk/sqlite/test_schema_parity.py` and `sed -n 130,215p tests/broker/alpaca/clerk/sqlite/test_schema_parity.py`; `grep -n "decision_receipts" docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md | head`. Mirror exactly what `decision_receipts` did for its table: the doc block, the base DDL position, and the migration entry.

- [ ] **Step 2: Write the failing tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import EnvelopeReservation
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg, OrderSide

# Reuse tests/broker/alpaca/clerk/sqlite/test_enter.py's repo fixture, _leg(),
# the instance/run registration it uses, and its fold helpers for a fill and
# for a broker-state update (fold_order_evidence with a BrokerOrder whose
# status is 'filled' / 'canceled'). Read that file first.

T0 = 1_788_040_000_000  # a fixed int64 ms UTC; every stamp is repo.clock()


def test_a_fresh_authority_has_the_reservations_table_at_schema_v13(repo) -> None:
    assert schema.SCHEMA_VERSION == 13
    assert repo.control_meta_snapshot().schema_version == 13
    assert repo._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='envelope_reservations'"
    ).fetchone() is not None


def test_a_v12_authority_migrates_additively_to_v13(tmp_path: Path) -> None:
    # Build a v12 file the way test_schema_parity builds older versions
    # (initialize, then rewrite control_meta.schema_version and drop the
    # new table), reopen through ClerkSqliteRepository.open, and assert the
    # table exists and every pre-existing row survived.
    ...


def test_accepting_an_enter_with_a_reservation_writes_the_row_in_the_same_commit(repo, active_instance) -> None:
    sid, run_id = active_instance
    reservation = EnvelopeReservation(quantity=10, reference_price=100.0)
    accepted = accept_enter(
        repo, account_id=repo.account_id, strategy_instance_id=sid, decision_id="d1",
        lifecycle_run_id=run_id, leg=_leg(quantity=10), envelope_reservation=reservation,
    )
    row = repo._conn.execute(
        "SELECT quantity, reference_price, reserved_at_ms FROM envelope_reservations WHERE effect_operation_id = ?",
        (accepted.effect_operation_id,),
    ).fetchone()
    assert tuple(row) == (10.0, 100.0, T0)


def test_the_reservation_never_enters_the_hash_chain(tmp_path: Path) -> None:
    # Two repositories, same clock, same leg: one ENTER with a reservation,
    # one without. The custody_transitions row_hash of ENTER_ACCEPTED must be
    # identical (plan R9).
    ...


@pytest.mark.parametrize(
    ("broker_state", "fills", "observed_at_ms", "expected"),
    [
        (None, [], T0, 1_000.0),                      # working, unacked: full
        ("new", [(4, T0 - 1)], T0, 600.0),            # 4 filled before the observation: remainder
        ("new", [(4, T0 + 1)], T0, 1_000.0),          # filled after: cash cannot reflect it yet
        ("filled", [(10, T0 - 1)], T0, 0.0),          # done and observed
        ("filled", [(10, T0 + 1)], T0, 1_000.0),      # done, not yet observed
        ("canceled", [], T0, 0.0),                    # dead, nothing to reserve
        ("canceled", [(3, T0 + 1)], T0, 300.0),       # dead with a fill after the observation
    ],
)
def test_reserved_cash_prices_only_what_the_observation_cannot_see(repo, active_instance, broker_state, fills, observed_at_ms, expected) -> None:
    # quantity 10 at reference price 100: accept, then fold broker_state and
    # the given (qty, recorded_at_ms) fills through the same folds test_enter uses.
    ...
    assert repo.reserved_cash_usd(observed_at_ms=observed_at_ms) == pytest.approx(expected)


def test_reservations_sum_across_instances(repo, two_active_instances) -> None:
    ...  # two accepted ENTERs (600 and 400) → 1_000.0
```

Fill in the `...` bodies from the `test_enter.py` harness (register instance, start run, `accept_enter`, fold evidence). Note `accept_enter`'s `envelope_reservation` keyword exists only in this task's test seam — Task 4 replaces it with `envelope=`/`reference_price=`; for this task add the keyword to `accept_enter` as a pass-through onto `TransitionInput` so the write path is testable in isolation.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py -q -p no:cacheprovider`
Expected: FAIL (`schema.SCHEMA_VERSION == 12`; missing table).

- [ ] **Step 4: Schema**

Base DDL, after the `decision_receipts` block:

```sql
-- ============================================================
-- envelope_reservations — the cash one accepted ENTER claims until its
-- fills are observed (ADR 0059 D4). Product evidence outside the hash
-- chain, like decision_receipts: written in ENTER_ACCEPTED's transaction,
-- never in facts_json, never in the mirror.
-- ============================================================
CREATE TABLE envelope_reservations (
    effect_operation_id      TEXT PRIMARY KEY REFERENCES effect_operations(effect_operation_id),
    quantity                 REAL NOT NULL CHECK (quantity > 0),
    reference_price          REAL NOT NULL CHECK (reference_price > 0),
    reserved_at_ms           INTEGER NOT NULL
);
```

`SCHEMA_VERSION = 13`; `SCHEMA_MIGRATIONS[12] = ("CREATE TABLE IF NOT EXISTS envelope_reservations (…same columns…)",)`. Mirror the block in the pinned-contracts doc exactly as the parity test expects, and rename/adjust the twenty-one → twenty-two table test.

- [ ] **Step 5: `envelope_reservations.py`**

```python
"""Cash reservations for accepted ENTERs (ADR 0059 D4, plan R1/R9).

A reservation prices the part of an ENTER the latest cash observation
cannot see: the unfilled remainder of a working order, plus any fill the
Clerk recorded at or after the observation. A terminal order reserves only
its post-observation fills. Corrections (``event_kind='correction'``) are
ignored on purpose — they restate price or quantity of an execution that is
already counted, and over-reserving is the safe direction.
"""

from __future__ import annotations

import sqlite3

from app.broker.alpaca.clerk.live_envelope import EnvelopeReservation

_TERMINAL_ORDER_STATES = ("filled", "canceled", "expired", "rejected", "replaced")


def append_envelope_reservation_row(
    conn: sqlite3.Connection,
    *,
    effect_operation_id: str,
    reservation: EnvelopeReservation,
    reserved_at_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO envelope_reservations (effect_operation_id, quantity, reference_price, reserved_at_ms) "
        "VALUES (?, ?, ?, ?)",
        (effect_operation_id, reservation.quantity, reservation.reference_price, reserved_at_ms),
    )


def reserved_cash_usd(conn: sqlite3.Connection, *, observed_at_ms: int) -> float:
    rows = conn.execute(
        "SELECT r.quantity, r.reference_price, o.order_ref, LOWER(o.broker_state) AS state "
        "FROM envelope_reservations r "
        "JOIN orders o ON o.effect_operation_id = r.effect_operation_id AND o.role = 'ENTRY' "
        "WHERE o.broker_state IS NULL OR LOWER(o.broker_state) NOT IN (?, ?, ?, ?, ?) "
        "   OR o.updated_at_ms >= ?",
        (*_TERMINAL_ORDER_STATES, observed_at_ms),
    ).fetchall()
    total = 0.0
    for row in rows:
        filled_before = 0.0
        filled_after = 0.0
        for fill in conn.execute(
            "SELECT qty, recorded_at_ms FROM fills WHERE order_ref = ? AND event_kind = 'fill' AND is_correction = 0",
            (row["order_ref"],),
        ):
            if fill["recorded_at_ms"] < observed_at_ms:
                filled_before += fill["qty"]
            else:
                filled_after += fill["qty"]
        dead = row["state"] in _TERMINAL_ORDER_STATES
        open_quantity = filled_after if dead else max(0.0, row["quantity"] - filled_before)
        total += open_quantity * row["reference_price"]
    return total


__all__ = ["append_envelope_reservation_row", "reserved_cash_usd"]
```

`conn.row_factory` is `sqlite3.Row` on the repository connection — verify with `grep -n "row_factory" app/broker/alpaca/clerk/sqlite/repository.py`; if reads use tuple rows, index positionally.

- [ ] **Step 6: Wire the atomic write and the read API**

- `models.py`: `envelope_reservation: EnvelopeReservation | None = None` as the last field of `TransitionInput` (import `EnvelopeReservation` from `app.broker.alpaca.clerk.live_envelope`). Confirm the payload dict at `repository.py:477-500` does not iterate dataclass fields; it lists them by name, so the new field cannot leak. Add a comment on the field: "Not part of the hashed payload — a sibling row, see envelope_reservations.py."
- `repository.py`: `append_transition` passes `envelope_reservation=transition.envelope_reservation` to `_commit_transition_row`, which takes `envelope_reservation: EnvelopeReservation | None = None` and, after the fold and before `advance_control_revision`, does: `if envelope_reservation is not None: if not payload["effect_operation_id"]: raise ValueError("a cash reservation needs the effect it reserves for"); append_envelope_reservation_row(self._conn, effect_operation_id=payload["effect_operation_id"], reservation=envelope_reservation, reserved_at_ms=payload["recorded_at_ms"])`.
- `repository_read_api.py`: `def reserved_cash_usd(self: ClerkSqliteRepository, *, observed_at_ms: int) -> float: with self._write_lock: return envelope_reservations.reserved_cash_usd(self._conn, observed_at_ms=observed_at_ms)`.
- `enter.py`: temporary pass-through keyword `envelope_reservation: EnvelopeReservation | None = None` on `accept_enter`, forwarded onto `TransitionInput(...)` inside `build_transition` (Task 4 replaces the keyword).

- [ ] **Step 7: Run the tests and the schema/enter suites**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py tests/broker/alpaca/clerk/sqlite/test_schema_parity.py tests/broker/alpaca/clerk/sqlite/test_enter.py tests/broker/alpaca/clerk/sqlite/test_cutover.py tests/contracts -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass (cutover's exact-schema checks must still hold at v13; `tests/contracts` guards the doc).

- [ ] **Step 8: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_reservations.py PythonDataService/app/broker/alpaca/clerk/sqlite/schema.py PythonDataService/app/broker/alpaca/clerk/sqlite/models.py PythonDataService/app/broker/alpaca/clerk/sqlite/repository.py PythonDataService/app/broker/alpaca/clerk/sqlite/repository_read_api.py PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md PythonDataService/tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_schema_parity.py && git commit -m "feat(envelope): durable cash reservations written in the ENTER_ACCEPTED transaction (schema v13)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Envelope admission inside `accept_enter`

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_admission.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py:145-235` (`accept_enter`), `:238-270` (`submit_enter` pass-through)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_envelope_admission.py`

**Interfaces:**
- Consumes: `LiveEnvelopeGate`, `EnvelopeReservation`, `cash_bound_admits`, the three reason-code constants (Task 1); `ClerkSqliteRepository.reserved_cash_usd` (Task 3); `AdmissionBlockedError`, `CapabilityDecision`, `Capability` (`uncertainty.py`).
- Produces: `require_envelope_admission(repo, *, envelope: LiveEnvelopeGate, leg: BrokerOrderLeg, reference_price: float | None, now_ms: int) -> EnvelopeReservation`; `accept_enter(..., envelope: LiveEnvelopeGate | None = None, reference_price: float | None = None)`; `submit_enter(..., envelope=None, reference_price=None)`. The Task 3 `envelope_reservation=` keyword is removed.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType

VALUES = LiveEnvelopeValues(loss_fraction=0.05, loss_usd=5_000.0, shadow_sessions=1, arming_max_sessions=20, xh_entry_bps=10.0, xh_exit_bps=10.0)
T0 = 1_788_040_000_000


def _gate(*, cash: float = 100_000.0, observed_at_ms: int = T0, sealed=None) -> LiveEnvelopeGate:
    gate = LiveEnvelopeGate(values=VALUES, sealed=sealed, custody_is_simulated=True)
    gate.publish(AccountObservation(observed_at_ms=observed_at_ms, broker_cash_usd=cash, cash_available_usd=cash, last_equity_usd=cash, unrealized_pl_usd=0.0, position_count=0))
    return gate


def _accept(repo, sid, run_id, *, decision_id, leg, envelope, reference_price=100.0):
    return accept_enter(repo, account_id=repo.account_id, strategy_instance_id=sid, decision_id=decision_id, lifecycle_run_id=run_id, leg=leg, envelope=envelope, reference_price=reference_price)


def _refusal(exc_info) -> str:
    return exc_info.value.decision.reason_code


def test_an_affordable_market_enter_is_admitted_and_reserved(repo, active_instance) -> None:
    sid, run_id = active_instance
    accepted = _accept(repo, sid, run_id, decision_id="d1", leg=_leg(quantity=100), envelope=_gate())
    assert accepted.created
    assert repo.reserved_cash_usd(observed_at_ms=T0) == pytest.approx(10_000.0)


def test_a_market_enter_beyond_cash_is_refused_and_nothing_is_written(repo, active_instance) -> None:
    sid, run_id = active_instance
    before = repo.control_meta_snapshot().control_revision
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1_001), envelope=_gate())
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    assert "100100.00 USD" in exc_info.value.decision.why
    assert repo.control_meta_snapshot().control_revision == before
    assert repo.reserved_cash_usd(observed_at_ms=T0) == 0.0


def test_two_instances_cannot_spend_the_same_cash(repo, two_active_instances) -> None:
    (a, run_a), (b, run_b) = two_active_instances
    gate = _gate()
    _accept(repo, a, run_a, decision_id="d1", leg=_leg(quantity=600), envelope=gate)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(repo, b, run_b, decision_id="d2", leg=_leg(quantity=600), envelope=gate)
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    _accept(repo, b, run_b, decision_id="d3", leg=_leg(quantity=400), envelope=gate)


def test_a_limit_leg_is_priced_at_its_limit_not_the_reference(repo, active_instance) -> None:
    sid, run_id = active_instance
    leg = _leg(quantity=100, order_type=OrderType.LIMIT, limit_price=1_001.0, extended_hours=True)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(repo, sid, run_id, decision_id="d1", leg=leg, envelope=_gate(), reference_price=1.0)
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED


@pytest.mark.parametrize(
    ("gate", "reference_price"),
    [
        (LiveEnvelopeGate(values=VALUES, custody_is_simulated=True), 100.0),   # never observed
        (_gate(observed_at_ms=T0 - OBSERVATION_MAX_AGE_MS - 1), 100.0),         # stale
        (_gate(), None),                                                         # market leg, no decision bar
    ],
)
def test_unobservable_facts_refuse_closed(repo, active_instance, gate, reference_price) -> None:
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1), envelope=gate, reference_price=reference_price)
    assert _refusal(exc_info) == LIVE_ENVELOPE_UNOBSERVED


def test_a_sealed_envelope_that_disagrees_with_the_environment_refuses(repo, active_instance) -> None:
    sid, run_id = active_instance
    other = LiveEnvelopeValues(**{**VALUES.to_mapping(), "loss_usd": 4_999.0})
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1), envelope=_gate(sealed=other))
    assert _refusal(exc_info) == LIVE_ENVELOPE_DISAGREEMENT


def test_no_envelope_means_no_envelope_check(repo, active_instance) -> None:
    sid, run_id = active_instance
    accepted = _accept(repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1_000_000), envelope=None, reference_price=None)
    assert accepted.created
    assert repo.reserved_cash_usd(observed_at_ms=T0) == 0.0


def test_a_sell_leg_cannot_be_an_envelope_enter(repo, active_instance) -> None:
    sid, run_id = active_instance
    with pytest.raises(ValueError, match="BUY"):
        _accept(repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1, side=OrderSide.SELL), envelope=_gate())
```

The `repo` fixture's clock must return `T0` (see `test_enter.py`'s `_clock`); `_leg` and the instance fixtures come from the same harness as Task 3.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_envelope_admission.py -q -p no:cacheprovider`
Expected: FAIL (`accept_enter() got an unexpected keyword argument 'envelope'`).

- [ ] **Step 3: `envelope_admission.py`**

```python
"""The envelope's ENTER-time check — the sibling of ``require_admission`` (ADR 0059 D4).

Order of refusals, each fail-closed: a sealed envelope that disagrees with
the environment; no fresh observation; a market leg with no decision-bar
price; then the cash rule (plan R1). The loss hold is not checked here: it
is an account hold ``require_admission`` already refuses on.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    EnvelopeReservation,
    LiveEnvelopeGate,
    cash_bound_admits,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
)
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType


def _refuse(reason_code: str, why: str) -> AdmissionBlockedError:
    return AdmissionBlockedError(
        CapabilityDecision(allowed=False, capability=Capability.NEW_EXPOSURE, reason_code=reason_code, why=why)
    )


def require_envelope_admission(
    repo: ClerkSqliteRepository,
    *,
    envelope: LiveEnvelopeGate,
    leg: BrokerOrderLeg,
    reference_price: float | None,
    now_ms: int,
) -> EnvelopeReservation:
    if leg.side is not OrderSide.BUY:
        raise ValueError("the envelope admits BUY legs only; every program ENTER is a BUY")
    if envelope.agreement == "disagreed":
        raise _refuse(
            LIVE_ENVELOPE_DISAGREEMENT,
            "The ALPACA_LIVE_* environment values differ from the envelope sealed at arming; re-arm to change them.",
        )
    observation = envelope.fresh_observation(now_ms)
    if observation is None:
        raise _refuse(
            LIVE_ENVELOPE_UNOBSERVED,
            "No fresh broker cash observation exists; the envelope cannot bound this ENTER yet.",
        )
    price = leg.limit_price if leg.order_type is OrderType.LIMIT else reference_price
    if price is None:
        raise _refuse(
            LIVE_ENVELOPE_UNOBSERVED,
            "A market ENTER has no decision-bar price to bound it against cash.",
        )
    reserved = repo.reserved_cash_usd(observed_at_ms=observation.observed_at_ms)
    notional = leg.quantity * price
    if not cash_bound_admits(
        cash_available_usd=observation.cash_available_usd, reserved_usd=reserved, notional_usd=notional
    ):
        raise _refuse(
            LIVE_ENVELOPE_CASH_EXCEEDED,
            f"ENTER needs {notional:.2f} USD; {observation.cash_available_usd:.2f} USD cash "
            f"with {reserved:.2f} USD reserved by working entries.",
        )
    return EnvelopeReservation(quantity=leg.quantity, reference_price=price)


__all__ = ["require_envelope_admission"]
```

- [ ] **Step 4: Thread it through `accept_enter` / `submit_enter`**

Replace Task 3's `envelope_reservation` keyword with `envelope: LiveEnvelopeGate | None = None, reference_price: float | None = None` on both functions. Inside `build_transition`, immediately after `require_admission(...)`:

```python
        reservation = (
            None
            if envelope is None
            else require_envelope_admission(
                repo, envelope=envelope, leg=leg, reference_price=reference_price, now_ms=repo.clock()
            )
        )
```

and `TransitionInput(..., envelope_reservation=reservation)`. Extend the docstring's R6 sentence with one line: "The ADR 0059 envelope (cash bound; the loss hold arrives as an account hold) is checked here too, after admission, and its reservation commits with the same transition." Update Task 3's reservation test to accept through `envelope=`.

- [ ] **Step 5: Run the tests**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_envelope_admission.py tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py tests/broker/alpaca/clerk/sqlite/test_enter.py tests/broker/alpaca/clerk/sqlite/test_exit.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/envelope_admission.py PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_envelope_admission.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py && git commit -m "feat(envelope): accept_enter admits against the cash bound and reserves what it admits

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The day-P&L fact

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/sqlite/day_pnl.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/economic_projection.py` (one method `account_net_cash_spent_usd`, placed directly after `account_pnl_attribution`; file is already 1508 lines — add nothing else)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/reads.py` + `repository_read_api.py` (`external_orders_observed_since`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_day_pnl.py`

**Interfaces:**
- Consumes: `et_day_window_ms` (Task 1), `AccountObservation` (Task 1), `SqliteEconomicProjectionReader.from_repository(repo)`, `.account_pnl_attribution(from_ms=, to_ms=)` → `AccountPnlAttribution(realized_pnl_total, fee_total: float | None, fee_fidelity)`.
- Produces: `@dataclass(frozen=True) class DayPnl(day_start_ms: int, day_end_ms: int, realized_usd: float, fee_usd: float, fee_fidelity: Literal["reported","not_reported"], unrealized_usd: float, external_orders_today: int)` with `@property total_usd -> float` (`realized − fee + unrealized`) and `@property known -> bool` (`external_orders_today == 0`); `day_pnl_at(reader, repo, *, observation: AccountObservation, now_ms: int) -> DayPnl`; `SqliteEconomicProjectionReader.account_net_cash_spent_usd() -> float` (Σ BUY `qty×price` − Σ SELL `qty×price` over every effective fill of every subject, lifetime); `ClerkSqliteRepository.external_orders_observed_since(*, since_ms: int) -> int`.

- [ ] **Step 1: Learn the fill-seeding harness**

Run: `grep -n "def _fill\|def _seed\|fold_order_evidence\|_broker_order_fixture\|external_order\|observe_external_order" tests/broker/alpaca/clerk/sqlite/test_economic_projection.py tests/broker/alpaca/clerk/sqlite/conftest.py | head -30`. Reuse how that suite folds a BUY and a SELL fill with explicit `source_event_at_ms` / `recorded_at_ms`, and how `observe_external_order` (from `external_order_folds.py`) records a foreign order.

- [ ] **Step 2: Write the failing tests**

```python
from __future__ import annotations

from datetime import date

import pytest

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.sqlite.day_pnl import DayPnl, day_pnl_at
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.utils.session_anchors import et_minute_of_day_ms

NOON = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
YESTERDAY_NOON = et_minute_of_day_ms(date(2026, 9, 4), 12 * 60)  # Friday; the 7th is Labor Day


def _observation(*, unrealized: float) -> AccountObservation:
    return AccountObservation(observed_at_ms=NOON, broker_cash_usd=100_000.0, cash_available_usd=100_000.0, last_equity_usd=100_000.0, unrealized_pl_usd=unrealized, position_count=1)


def test_realized_counts_only_lots_closed_today_and_nets_reported_fees(repo, seeded_round_trip) -> None:
    # seeded_round_trip: BUY 10 @ 100 recorded YESTERDAY_NOON, SELL 10 @ 110 recorded NOON with fee 0.05 reported.
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=25.0), now_ms=NOON)
    assert (pnl.day_start_ms, pnl.day_end_ms) == et_day_window_ms(NOON)
    assert pnl.realized_usd == pytest.approx(100.0)
    assert pnl.fee_usd == pytest.approx(0.05) and pnl.fee_fidelity == "reported"
    assert pnl.unrealized_usd == 25.0
    assert pnl.total_usd == pytest.approx(124.95)
    assert pnl.known


def test_an_external_order_seen_today_makes_the_fact_unknown(repo, seeded_external_order_today) -> None:
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.external_orders_today == 1 and not pnl.known


def test_an_external_order_seen_yesterday_does_not(repo, seeded_external_order_yesterday) -> None:
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    assert day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON).known


def test_net_cash_spent_is_buys_less_sells_over_every_subject(repo, seeded_round_trip) -> None:
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    assert reader.account_net_cash_spent_usd() == pytest.approx(1_000.0 - 1_100.0)


def test_unreported_fees_net_nothing_and_say_so(repo, seeded_round_trip_without_fees) -> None:
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.fee_usd == 0.0 and pnl.fee_fidelity == "not_reported"
```

Write the seeding fixtures from the Step 1 harness. Every recorded stamp is an explicit int64 ms from `et_minute_of_day_ms`; the repo clock is a fixed `NOON`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_day_pnl.py -q -p no:cacheprovider`
Expected: FAIL (`ModuleNotFoundError: day_pnl`).

- [ ] **Step 4: Implement**

`reads.py`: `def external_orders_observed_since(conn, *, since_ms: int) -> int: return int(conn.execute("SELECT COUNT(*) FROM external_orders WHERE observed_at_ms >= ?", (since_ms,)).fetchone()[0])`; `repository_read_api.py` wraps it under `_write_lock` like its neighbours.

`economic_projection.py`, after `account_pnl_attribution`:

```python
    def account_net_cash_spent_usd(self) -> float:
        """Σ BUY notional − Σ SELL notional over every effective fill, lifetime.

        The cash the Clerk's own fills would have taken from the account.
        Under simulated custody (ADR 0059 D2) the broker's cash never moved,
        so the envelope subtracts this to rehearse the cash bound honestly
        (plan R2); under real custody the broker's cash already reflects it.
        """
        with self._read_transaction():
            self._verified_meta()
            rows = self._effective_fill_rows(strategy_instance_ids=None, from_ms=None, to_ms=None, cursor_key=None, limit=None)
        total = 0.0
        for record in (_to_fill_record(row, account_id=self._account_id, custody_subject_identity=True) for row in rows):
            notional = record.qty * record.price
            total += notional if record.side is OrderSide.BUY else -notional
        return total
```

(Confirm `FillRecord`'s field names with `grep -n "class FillRecord" -A 14 app/broker/alpaca/clerk/fifo_pnl.py`; use its exact `qty`/`price`/`side` spellings.)

`day_pnl.py`:

```python
"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

Formula: ``day_pnl = Σ realized FIFO closed-lot P&L in [ET midnight, now]
  − Σ reported fees on those fills + Σ broker-observed unrealized_pl``.
Reference: ADR 0059 Decision 4; ``CONTEXT.md`` § Day P&L.
Canonical implementation: this file, over ``fifo_pnl``'s FIFO through
  ``SqliteEconomicProjectionReader.account_pnl_attribution``.
Validated against: ``tests/broker/alpaca/clerk/sqlite/test_day_pnl.py``.

The fact is *unknown*, never zero, when an external order was observed
today: its realized P&L is not journaled (plan R5). Unrealized P&L is the
broker's own figure per position, so no marks are needed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@dataclass(frozen=True)
class DayPnl:
    day_start_ms: int
    day_end_ms: int
    realized_usd: float
    fee_usd: float
    fee_fidelity: Literal["reported", "not_reported"]
    unrealized_usd: float
    external_orders_today: int

    @property
    def total_usd(self) -> float:
        return self.realized_usd - self.fee_usd + self.unrealized_usd

    @property
    def known(self) -> bool:
        return self.external_orders_today == 0


def day_pnl_at(
    reader: SqliteEconomicProjectionReader,
    repo: ClerkSqliteRepository,
    *,
    observation: AccountObservation,
    now_ms: int,
) -> DayPnl:
    day_start_ms, day_end_ms = et_day_window_ms(now_ms)
    attribution = reader.account_pnl_attribution(from_ms=day_start_ms, to_ms=now_ms)
    return DayPnl(
        day_start_ms=day_start_ms,
        day_end_ms=day_end_ms,
        realized_usd=attribution.realized_pnl_total,
        fee_usd=attribution.fee_total if attribution.fee_total is not None else 0.0,
        fee_fidelity=attribution.fee_fidelity,
        unrealized_usd=observation.unrealized_pl_usd,
        external_orders_today=repo.external_orders_observed_since(since_ms=day_start_ms),
    )


__all__ = ["DayPnl", "day_pnl_at"]
```

- [ ] **Step 5: Run the tests and the projection suite**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_day_pnl.py tests/broker/alpaca/clerk/sqlite/test_economic_projection.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/day_pnl.py PythonDataService/app/broker/alpaca/clerk/sqlite/economic_projection.py PythonDataService/app/broker/alpaca/clerk/sqlite/reads.py PythonDataService/app/broker/alpaca/clerk/sqlite/repository_read_api.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_day_pnl.py && git commit -m "feat(envelope): the account-wide day-P&L fact, unknown when an external order was seen today

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `LiveEnvelopeSync` — observe, publish, raise the loss hold

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py`

**Interfaces:**
- Consumes: `LiveEnvelopeGate`, `AccountObservation`, `ENVELOPE_SYNC_INTERVAL_S`, `loss_limit_usd`, `loss_breached` (Task 1); `LossHoldCause`, `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE`, `raise_account_hold(cause_facts=)` (Task 2); `day_pnl_at`, `DayPnl`, `account_net_cash_spent_usd` (Task 5); `BrokerReadPort.get_account()/list_positions()`; `BrokerError`.
- Produces:
  - `@dataclass(frozen=True) class EnvelopeReading(observation: AccountObservation, day_pnl: DayPnl | None, loss_limit_usd: float | None)` with `@property breached -> bool | None` (`None` when `day_pnl is None or not day_pnl.known or loss_limit_usd is None`)
  - `EnvelopeSyncAction = Literal["observed", "hold_raised", "hold_stands", "unknown", "read_failed"]`
  - `class LiveEnvelopeSync` — `__init__(self, *, repo, read: BrokerReadPort, envelope: LiveEnvelopeGate, interval_s: float = ENVELOPE_SYNC_INTERVAL_S, sleep=asyncio.sleep, max_ticks: int | None = None)`; `async observe(self) -> EnvelopeReading` (one broker read, publishes the observation, raises nothing); `async tick(self) -> EnvelopeSyncAction`; `async run()`, `start()`, `async stop()`; attribute `envelope`.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from datetime import date

import pytest

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate, LiveEnvelopeValues, OBSERVATION_MAX_AGE_MS
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, LossHoldCause
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot, BrokerPosition
from app.utils.session_anchors import et_minute_of_day_ms

NOON = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
VALUES = LiveEnvelopeValues(loss_fraction=0.05, loss_usd=5_000.0, shadow_sessions=1, arming_max_sessions=20, xh_entry_bps=10.0, xh_exit_bps=10.0)


class _Read:
    """A read port whose account and positions the test sets per tick."""

    def __init__(self, *, cash: float = 100_000.0, last_equity: float | None = 100_000.0, unrealized: float = 0.0, fail: bool = False) -> None:
        self.cash, self.last_equity, self.unrealized, self.fail = cash, last_equity, unrealized, fail

    async def get_account(self) -> BrokerAccountSnapshot:
        if self.fail:
            raise BrokerUnavailable("account read timed out")
        return BrokerAccountSnapshot(broker="alpaca", account_id="9LIVE0001", account_mode="live", account_status="ACTIVE", currency="USD", cash=self.cash, equity=self.cash + self.unrealized, buying_power=self.cash, portfolio_value=self.cash, long_market_value=0.0, short_market_value=0.0, last_equity=self.last_equity, pattern_day_trader=False, trading_blocked=False, account_blocked=False, created_at_ms=None, observed_at_ms=NOON)

    async def list_positions(self) -> list[BrokerPosition]:
        if self.unrealized == 0.0:
            return []
        return [BrokerPosition(broker="alpaca", symbol="SPY", asset_id=None, asset_class=None, quantity=1, side="long", average_entry_price=100.0, market_value=100.0 + self.unrealized, cost_basis=100.0, current_price=None, unrealized_pl=self.unrealized, unrealized_plpc=None, observed_at_ms=NOON)]


def _sync(repo, read: _Read, *, simulated: bool = True) -> LiveEnvelopeSync:
    return LiveEnvelopeSync(repo=repo, read=read, envelope=LiveEnvelopeGate(values=VALUES, custody_is_simulated=simulated))


def _hold(repo):
    return repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None)


async def test_a_tick_publishes_a_fresh_observation_stamped_by_the_repo_clock(repo) -> None:
    sync = _sync(repo, _Read())
    assert await sync.tick() == "observed"
    observation = sync.envelope.fresh_observation(NOON)
    assert observation is not None and observation.observed_at_ms == NOON
    assert observation.cash_available_usd == 100_000.0 and observation.last_equity_usd == 100_000.0


async def test_simulated_custody_subtracts_what_the_clerks_own_fills_would_have_spent(repo, seeded_open_buy) -> None:
    # seeded_open_buy: one effective BUY fill, 10 @ 100, recorded before NOON.
    sync = _sync(repo, _Read(), simulated=True)
    await sync.tick()
    assert sync.envelope.latest_observation().cash_available_usd == pytest.approx(99_000.0)
    real = _sync(repo, _Read(), simulated=False)
    await real.tick()
    assert real.envelope.latest_observation().cash_available_usd == pytest.approx(100_000.0)


async def test_a_breach_raises_the_hold_once_and_the_sync_never_releases_it(repo) -> None:
    read = _Read(unrealized=-5_000.0)
    sync = _sync(repo, read)
    assert await sync.tick() == "hold_raised"
    hold = _hold(repo)
    assert hold is not None
    cause = LossHoldCause.from_mapping(__import__("json").loads(hold["facts_json"])["cause"])  # adapt to the stored envelope shape
    assert cause.day_pnl_usd == pytest.approx(-5_000.0) and cause.loss_limit_usd == pytest.approx(5_000.0)
    revision = repo.control_meta_snapshot().control_revision
    read.unrealized = -6_000.0
    assert await sync.tick() == "hold_stands"
    read.unrealized = 0.0
    assert await sync.tick() == "hold_stands"
    assert repo.control_meta_snapshot().control_revision == revision


async def test_an_unknown_fact_raises_nothing(repo, seeded_external_order_today) -> None:
    sync = _sync(repo, _Read(unrealized=-50_000.0))
    assert await sync.tick() == "unknown"
    assert _hold(repo) is None


async def test_a_missing_last_equity_is_unknown(repo) -> None:
    sync = _sync(repo, _Read(last_equity=None, unrealized=-50_000.0))
    assert await sync.tick() == "unknown"
    assert _hold(repo) is None


async def test_a_failed_read_keeps_the_loop_alive_and_lets_the_observation_age_out(repo) -> None:
    read = _Read()
    sync = _sync(repo, read)
    await sync.tick()
    read.fail = True
    assert await sync.tick() == "read_failed"
    assert sync.envelope.fresh_observation(NOON) is not None
    assert sync.envelope.fresh_observation(NOON + OBSERVATION_MAX_AGE_MS + 1) is None


async def test_the_loop_survives_a_failing_tick(repo) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    sync = LiveEnvelopeSync(repo=repo, read=_Read(fail=True), envelope=LiveEnvelopeGate(values=VALUES, custody_is_simulated=True), interval_s=15.0, sleep=sleep, max_ticks=3)
    await sync.run()
    assert slept == [15.0, 15.0, 15.0]
```

The `repo` fixture's clock returns `NOON`; check how `test_loss_hold.py` (Task 2) reads the stored cause (`facts_json` → `UncertaintyRaisedFacts` shape) and adapt the one decode line. The `seeded_*` fixtures come from Task 5's file — move the shared ones into `tests/broker/alpaca/clerk/sqlite/conftest.py` if both suites need them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py -q -p no:cacheprovider`
Expected: FAIL (`ModuleNotFoundError: live_envelope_sync`).

- [ ] **Step 3: Implement**

```python
"""Independent fixed-cadence lifecycle for the live envelope (ADR 0059 D4).

Modelled on ``StreamHealthHoldSync``: one background tap produces the
account observation and the loss hold; ``accept_enter`` consumes them and
never contacts the broker. Decoupled from the reconciliation pass, whose
backoff reaches 300 s on failure — exactly when a loss hold matters most.

The sync raises the loss hold and never releases it: only the guarded
operator action does (plan R6, R12).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SYNC_INTERVAL_S,
    AccountObservation,
    LiveEnvelopeGate,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import DayPnl, day_pnl_at
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]
EnvelopeSyncAction = Literal["observed", "hold_raised", "hold_stands", "unknown", "read_failed"]


@dataclass(frozen=True)
class EnvelopeReading:
    observation: AccountObservation
    day_pnl: DayPnl | None
    loss_limit_usd: float | None

    @property
    def breached(self) -> bool | None:
        if self.day_pnl is None or not self.day_pnl.known or self.loss_limit_usd is None:
            return None
        return loss_breached(day_pnl_usd=self.day_pnl.total_usd, loss_limit_usd=self.loss_limit_usd)


class LiveEnvelopeSync:
    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        envelope: LiveEnvelopeGate,
        interval_s: float = ENVELOPE_SYNC_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
        max_ticks: int | None = None,
    ) -> None:
        self._repo = repo
        self._read = read
        self.envelope = envelope
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_ticks = max_ticks
        self._reader = SqliteEconomicProjectionReader.from_repository(repo)
        self._task: asyncio.Task[None] | None = None

    async def observe(self) -> EnvelopeReading:
        """One broker read → a published observation and the day-P&L fact. Raises ``BrokerError``."""
        account, positions = await asyncio.gather(self._read.get_account(), self._read.list_positions())
        observed_at_ms = self._repo.clock()
        spent = self._reader.account_net_cash_spent_usd() if self.envelope.custody_is_simulated else 0.0
        observation = AccountObservation(
            observed_at_ms=observed_at_ms,
            broker_cash_usd=account.cash,
            cash_available_usd=account.cash - spent,
            last_equity_usd=account.last_equity,
            unrealized_pl_usd=sum(position.unrealized_pl for position in positions),
            position_count=len(positions),
        )
        self.envelope.publish(observation)
        limit = (
            None
            if account.last_equity is None
            else loss_limit_usd(self.envelope.values, last_equity_usd=account.last_equity)
        )
        day_pnl = day_pnl_at(self._reader, self._repo, observation=observation, now_ms=observed_at_ms)
        return EnvelopeReading(observation=observation, day_pnl=day_pnl, loss_limit_usd=limit)

    async def tick(self) -> EnvelopeSyncAction:
        try:
            reading = await self.observe()
        except BrokerError as exc:
            logger.warning(
                "live envelope could not observe the account; the observation will age out",
                extra={"action": "live_envelope_read_failed", "account_id": self._repo.account_id, "why": str(exc)},
            )
            return "read_failed"
        if self._repo.active_uncertainty(
            scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None
        ) is not None:
            return "hold_stands"
        if reading.breached is None:
            return "unknown"
        if not reading.breached:
            return "observed"
        assert reading.day_pnl is not None and reading.loss_limit_usd is not None
        assert reading.observation.last_equity_usd is not None
        cause = LossHoldCause(
            day_start_ms=reading.day_pnl.day_start_ms,
            day_pnl_usd=reading.day_pnl.total_usd,
            loss_limit_usd=reading.loss_limit_usd,
            last_equity_usd=reading.observation.last_equity_usd,
            observed_at_ms=reading.observation.observed_at_ms,
        )
        outcome = raise_account_hold(
            self._repo,
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            evidence_refs=[f"day-pnl:{cause.day_start_ms}"],
            cause_facts=cause.to_mapping(),
        )
        logger.warning(
            "live envelope loss hold %s",
            outcome,
            extra={"action": f"live_envelope_loss_hold_{outcome}", "account_id": self._repo.account_id, **cause.to_mapping()},
        )
        return "hold_raised"

    async def run(self) -> None:
        ticks = 0
        while self._max_ticks is None or ticks < self._max_ticks:
            try:
                await self.tick()
            except Exception:
                logger.exception("live envelope sync tick failed", extra={"action": "live_envelope_sync_failed"})
            ticks += 1
            await self._sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="alpaca-live-envelope-sync")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._reader.close()


__all__ = ["EnvelopeReading", "EnvelopeSyncAction", "LiveEnvelopeSync"]
```

`self._repo.active_uncertainty` and `self._repo.clock` exist on the repository (`repository_read_api.py:585`, the ctor's `clock`); if `active_uncertainty` is named differently on the repository facade, use the exact name you find.

- [ ] **Step 4: Run the tests**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py tests/broker/alpaca/clerk/sqlite/test_loss_hold.py tests/broker/alpaca/clerk/sqlite/test_day_pnl.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py && git add -u PythonDataService/tests/broker/alpaca/clerk/sqlite/conftest.py && git commit -m "feat(envelope): a fixed-cadence sync observes cash, composes day P&L and raises the loss hold

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Composition — the envelope rides the shadow authority, fail-closed

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:207-262` (`SqliteAlpacaClerkFacade.__init__(live_envelope=)`), the ENTER branch of `_execute_effect` (~`:800-840`, the `accept_enter(...)` call)
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_runtime.py:90-130` (`ActiveClerkRuntime.envelope_sync`, `start_hold_sync`, `close`), `:176-300` (`_ComposedAuthority.envelope_sync`, `compose_repository_runtime(live_envelope=)`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/shadow_authority.py:48-160` (`select_shadow_clerk_runtime(live_envelope_values=)`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py:87-140` (`select_active_clerk_runtime(live_envelope_values=)`)
- Modify: `PythonDataService/app/main.py:240-247`
- Modify: `PythonDataService/app/services/alpaca_sqlite_synthetic_state_drills.py` only if its selector call must change (it composes synthetic; it should not)
- Modify tests that compose a shadow authority: `tests/broker/v2panel/test_shadow_operator_surfaces.py`, `tests/broker/alpaca/clerk/test_active_authority.py`, `tests/contracts/test_alpaca_active_authority_wiring.py`, `tests/broker/alpaca/clerk/sqlite/test_cutover.py` (only where the account is live)
- Create: `PythonDataService/tests/broker/alpaca/clerk/live_envelope_fixtures.py` (`TEST_ENVELOPE_VALUES`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py`

**Interfaces:**
- Consumes: `LiveEnvelopeValues`, `LiveEnvelopeGate` (Task 1); `LiveEnvelopeSync` (Task 6); `accept_enter(envelope=, reference_price=)` (Task 4); `unavailable_runtime`.
- Produces: `SqliteAlpacaClerkFacade(..., live_envelope: LiveEnvelopeGate | None = None)` + `@property live_envelope -> LiveEnvelopeGate | None`; `compose_repository_runtime(..., live_envelope: LiveEnvelopeGate | None = None)`; `_ComposedAuthority.envelope_sync: LiveEnvelopeSync | None`; `ActiveClerkRuntime.envelope_sync: LiveEnvelopeSync | None = None`; `select_shadow_clerk_runtime(..., live_envelope_values: LiveEnvelopeValues | None)` returning `unavailable_runtime("LIVE_ENVELOPE_MISSING", ...)` when `None`; `select_active_clerk_runtime(..., live_envelope_values: LiveEnvelopeValues | None = None)`; `TEST_ENVELOPE_VALUES: LiveEnvelopeValues`.

- [ ] **Step 1: Read the composition sites**

Run: `grep -n "ActiveClerkRuntime(" app/broker/alpaca/clerk/shadow_authority.py app/broker/alpaca/clerk/active_authority.py` (where `_ComposedAuthority` becomes a runtime); `grep -n "get_alpaca_settings\|alpaca_settings" app/main.py | head`; `sed -n 236,300p tests/broker/v2panel/test_shadow_operator_surfaces.py` (how a shadow ENTER is driven through the facade in slice 4's test — reuse its instance registration and run start).

- [ ] **Step 2: Write the failing tests**

`tests/broker/alpaca/clerk/live_envelope_fixtures.py`:

```python
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues

TEST_ENVELOPE_VALUES = LiveEnvelopeValues(
    loss_fraction=0.05, loss_usd=5_000.0, shadow_sessions=1, arming_max_sessions=20, xh_entry_bps=10.0, xh_exit_bps=10.0
)
```

`tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py` — compose the REAL shadow authority the way `test_shadow_operator_surfaces.py`'s `shadow_app` fixture does (`activate_shadow_clerk_authority` → `select_active_clerk_runtime(read=broker, trade=broker, artifacts_root=tmp_path, live_envelope_values=TEST_ENVELOPE_VALUES)`), with a `_LiveBroker` whose `get_account` returns the cash the test sets and whose `submit`/`cancel` raise:

```python
async def test_a_shadow_authority_without_envelope_values_is_unavailable(tmp_path) -> None:
    await activate_shadow_clerk_authority(live_account_id=LIVE_ACCT, artifacts_root=tmp_path)
    runtime = await select_active_clerk_runtime(read=_LiveBroker(), trade=_LiveBroker(), artifacts_root=tmp_path)
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None and runtime.startup_failure.reason_code == "LIVE_ENVELOPE_MISSING"


async def test_the_composed_shadow_runtime_carries_a_simulated_custody_envelope_and_its_sync(shadow_runtime) -> None:
    runtime, broker = shadow_runtime
    assert runtime.envelope_sync is not None
    assert runtime.clerk.live_envelope is runtime.envelope_sync.envelope
    assert runtime.envelope_sync.envelope.custody_is_simulated is True
    assert runtime.envelope_sync.envelope.values == TEST_ENVELOPE_VALUES


async def test_an_unobserved_envelope_refuses_the_enter_as_a_rejected_receipt_not_an_exception(shadow_runtime, registered_running_bot) -> None:
    runtime, broker = shadow_runtime
    receipt = await _enter(runtime, registered_running_bot, quantity=1, bar_close="100")
    assert receipt.state.value == "rejected"
    assert receipt.reason_code == "LIVE_ENVELOPE_UNOBSERVED"


async def test_after_one_tick_the_cash_bound_admits_what_cash_covers_and_refuses_what_it_does_not(shadow_runtime, registered_running_bot) -> None:
    runtime, broker = shadow_runtime
    broker.cash = 10_000.0
    assert await runtime.envelope_sync.tick() == "observed"
    refused = await _enter(runtime, registered_running_bot, quantity=101, bar_close="100")
    assert refused.state.value == "rejected" and refused.reason_code == "LIVE_ENVELOPE_CASH_EXCEEDED"
    admitted = await _enter(runtime, registered_running_bot, quantity=100, bar_close="100", decision_id="d2")
    assert admitted.state.value != "rejected"
    assert runtime.sqlite_repository.reserved_cash_usd(observed_at_ms=runtime.sqlite_repository.clock()) == pytest.approx(10_000.0)


async def test_a_paper_authority_carries_no_envelope_even_when_values_are_offered(tmp_path) -> None:
    # Compose the real-paper authority the way tests/broker/alpaca/clerk/test_active_authority.py does,
    # passing live_envelope_values=TEST_ENVELOPE_VALUES; assert runtime.envelope_sync is None
    # and runtime.clerk.live_envelope is None.
    ...
```

`_enter(...)` calls `runtime.clerk.execute_for_instance(strategy_instance_id=..., run_id=..., decision_id=..., purpose=<the ENTER purpose>, action_plan=..., quantity=..., use_rth=True, retained_source_bar=<a RetainedSourceBar whose close is bar_close>)` exactly as the slice-4 surfaces test drives an ENTER; copy its helpers for the binding, the registration, the run start, and the retained bar. `receipt.reason_code` — confirm the receipt's field name for the refusal code with `grep -n "class EffectOperationReceipt" -A 20 app/broker/alpaca/clerk/sqlite/runtime.py`; use the exact field.

Update the four existing shadow-composing test files to pass `live_envelope_values=TEST_ENVELOPE_VALUES` wherever the observed account is live (leave paper compositions alone).

- [ ] **Step 3: Run the new test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py -q -p no:cacheprovider`
Expected: FAIL (`unexpected keyword argument 'live_envelope_values'`).

- [ ] **Step 4: Implement**

- `runtime.py`: ctor gains `live_envelope: LiveEnvelopeGate | None = None`, stored as `self._live_envelope`; `@property live_envelope`. In `_execute_effect`'s ENTER branch the `accept_enter(...)` call adds `envelope=self._live_envelope, reference_price=None if retained_source_bar is None else float(retained_source_bar.close)`. Nothing else in the runtime changes: an `AdmissionBlockedError` there already returns `rejected(reason_code=..., explanation=...)`.
- `active_runtime.py`: `compose_repository_runtime(..., live_envelope: LiveEnvelopeGate | None = None)`; pass `live_envelope=live_envelope` to the facade; after `hold_sync = StreamHealthHoldSync(...)`: `envelope_sync = None if live_envelope is None else LiveEnvelopeSync(repo=repository, read=guarded_read, envelope=live_envelope)`; add it to `_ComposedAuthority` and to the failure cleanup (`await envelope_sync.stop()`). `ActiveClerkRuntime.envelope_sync: LiveEnvelopeSync | None = None`; `start_hold_sync()` also starts it (docstring: "…and the live envelope sync, which needs only the read port and could start earlier but shares this seam so main.py has one start call"); `close()` stops it first.
- `shadow_authority.py`: `select_shadow_clerk_runtime(..., live_envelope_values: LiveEnvelopeValues | None)`; before composing: `if live_envelope_values is None: return unavailable_runtime("LIVE_ENVELOPE_MISSING", account_id=shadow_account_id_for_live_account(account.account_id), recovery="Set every ALPACA_LIVE_* value; the shadow authority rehearses the live envelope and refuses to run without it (ADR 0059 D4).")`; pass `live_envelope=LiveEnvelopeGate(values=live_envelope_values, custody_is_simulated=True)` into `compose_repository_runtime`, and `envelope_sync=composed.envelope_sync` into the `ActiveClerkRuntime(...)` it builds.
- `active_authority.py`: `select_active_clerk_runtime(..., live_envelope_values: LiveEnvelopeValues | None = None)` forwards to the shadow selector; the paper/synthetic paths never pass an envelope.
- `main.py`: next to the selector call, `live_envelope_values = None if alpaca_settings.is_paper else LiveEnvelopeValues.from_settings(alpaca_settings)` using whatever name main.py already holds the settings under (Step 1), and pass `live_envelope_values=live_envelope_values`.

- [ ] **Step 5: Run the suites**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py tests/broker/v2panel/test_shadow_operator_surfaces.py tests/broker/alpaca/clerk/test_active_authority.py tests/contracts/test_alpaca_active_authority_wiring.py tests/broker/alpaca/clerk/sqlite/test_cutover.py tests/broker/alpaca/clerk/sqlite/test_runtime_program_leg.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main" && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass; `import app.main` clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py PythonDataService/app/broker/alpaca/clerk/active_runtime.py PythonDataService/app/broker/alpaca/clerk/shadow_authority.py PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/app/main.py PythonDataService/tests/broker/alpaca/clerk/live_envelope_fixtures.py PythonDataService/tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py && git add -u PythonDataService/tests/ && git commit -m "feat(envelope): the shadow authority rehearses the live envelope and is unavailable without it

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: The guarded clear

**Files:**
- Create: `PythonDataService/app/schemas/alpaca_live_envelope.py`
- Create: `PythonDataService/app/services/alpaca_live_envelope.py`
- Modify: `PythonDataService/app/routers/brokers.py` (after `get_live_verdict`, ~`:715`)
- Test: `PythonDataService/tests/services/test_alpaca_live_envelope.py`, `PythonDataService/tests/routers/test_brokers_live_envelope.py`

**Interfaces:**
- Consumes: `ActiveClerkRuntime.envelope_sync` (Task 7), `LiveEnvelopeSync.observe() -> EnvelopeReading` (Task 6), `resolve_account_hold`, `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE`, `require_data_plane_control_secret`.
- Produces: `class LossHoldClearOutcome(BaseModel)`: `outcome: Literal["cleared", "no_hold", "refused"]`, `reason_code: str | None`, `day_pnl_usd: float | None`, `loss_limit_usd: float | None`, `observed_at_ms: EpochMs`, `detail: str`; `class LiveEnvelopeNotInstalled(Exception)`; `async clear_loss_hold(runtime: ActiveClerkRuntime, *, now_ms: int) -> LossHoldClearOutcome`; `POST /api/brokers/{broker}/live-envelope/loss-hold/clear` (control secret; 404 unsupported broker; 503 `live_envelope_not_installed`).

- [ ] **Step 1: Learn how router tests satisfy the control secret**

Run: `grep -rn "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL\|X-Data-Plane-Control-Secret" tests/routers/*.py tests/conftest.py | head -5`. Use the same mechanism.

- [ ] **Step 2: Write the failing tests**

Service test, on the composed shadow runtime from Task 7's harness (import its `_LiveBroker`, fixtures and `TEST_ENVELOPE_VALUES`):

```python
async def test_the_clear_refuses_while_the_breach_stands_then_clears_once_it_has_lifted(shadow_runtime) -> None:
    runtime, broker = shadow_runtime
    broker.unrealized = -5_000.0
    assert await runtime.envelope_sync.tick() == "hold_raised"
    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert refused.outcome == "refused" and refused.reason_code == "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
    assert refused.day_pnl_usd == pytest.approx(-5_000.0) and refused.loss_limit_usd == pytest.approx(5_000.0)
    assert _hold(runtime.sqlite_repository) is not None
    broker.unrealized = -4_999.0
    cleared = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert cleared.outcome == "cleared" and cleared.reason_code is None
    assert _hold(runtime.sqlite_repository) is None


async def test_the_clear_refuses_an_unknown_fact(shadow_runtime, seeded_external_order_today) -> None:
    runtime, broker = shadow_runtime
    broker.unrealized = -5_000.0
    # raise the hold first with the fact still known, then make it unknown
    ...
    refused = await clear_loss_hold(runtime, now_ms=NOW_MS)
    assert refused.outcome == "refused" and refused.reason_code == "LIVE_ENVELOPE_UNOBSERVED"


async def test_no_hold_is_reported_not_invented(shadow_runtime) -> None:
    runtime, _ = shadow_runtime
    assert (await clear_loss_hold(runtime, now_ms=NOW_MS)).outcome == "no_hold"


async def test_a_runtime_without_an_envelope_cannot_clear(paper_runtime) -> None:
    with pytest.raises(LiveEnvelopeNotInstalled):
        await clear_loss_hold(paper_runtime, now_ms=NOW_MS)
```

Router test (ASGI, `httpx.AsyncClient` + `ASGITransport(app=app)`, the `shadow_app` fixture shape from `test_shadow_operator_surfaces.py`): `POST /api/brokers/alpaca/live-envelope/loss-hold/clear` → 200 with `outcome == "no_hold"`; `/api/brokers/ibkr/...` → 404 with `detail.reason == "live_envelope_unsupported_broker"`; with the active runtime set to `None` → 503 with `detail.reason == "live_envelope_not_installed"`; without the control secret when one is configured → the same status the manual-order routes return (assert whatever `require_data_plane_control_secret` yields, read from the security module).

- [ ] **Step 3: Run to verify failure**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_envelope.py tests/routers/test_brokers_live_envelope.py -q -p no:cacheprovider`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Implement**

`app/schemas/alpaca_live_envelope.py`:

```python
"""The guarded loss-hold clear's outcome (ADR 0059 D4, ADR 0011 §6 shape)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs


class LossHoldClearOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["cleared", "no_hold", "refused"]
    reason_code: str | None
    day_pnl_usd: float | None
    loss_limit_usd: float | None
    observed_at_ms: EpochMs
    detail: str = Field(min_length=1)
```

`app/services/alpaca_live_envelope.py`:

```python
"""Operator actions on the live envelope (ADR 0059 D4).

``clear_loss_hold`` is the ADR 0011 §6 shape: it re-reads the fact the
hold was raised on and refuses while the breach still stands. The hold
never clears on a timer or at session rollover; this is the only release.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.sqlite.uncertainty import resolve_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.contract.errors import BrokerError
from app.schemas.alpaca_live_envelope import LossHoldClearOutcome

LIVE_ENVELOPE_LOSS_HOLD_STANDS = "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
LIVE_ENVELOPE_LOSS_HOLD_CLEARED = "LIVE_ENVELOPE_LOSS_HOLD_CLEARED"


class LiveEnvelopeNotInstalled(Exception):
    """The active authority carries no live envelope (paper, synthetic, or none)."""


async def clear_loss_hold(runtime: ActiveClerkRuntime, *, now_ms: int) -> LossHoldClearOutcome:
    repo = runtime.sqlite_repository
    sync = runtime.envelope_sync
    if repo is None or sync is None:
        raise LiveEnvelopeNotInstalled("no live envelope is installed on the active authority")
    if repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is None:
        return LossHoldClearOutcome(outcome="no_hold", reason_code=None, day_pnl_usd=None, loss_limit_usd=None, observed_at_ms=now_ms, detail="The account is not in loss hold.")
    try:
        reading = await sync.observe()
    except BrokerError as exc:
        return LossHoldClearOutcome(outcome="refused", reason_code=LIVE_ENVELOPE_UNOBSERVED, day_pnl_usd=None, loss_limit_usd=None, observed_at_ms=now_ms, detail=f"The account could not be re-observed: {exc}. The hold stands.")
    day_pnl = None if reading.day_pnl is None else reading.day_pnl.total_usd
    if reading.breached is None:
        return LossHoldClearOutcome(outcome="refused", reason_code=LIVE_ENVELOPE_UNOBSERVED, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail="Day P&L is unknown (an external order was seen today, or the broker reported no previous-close equity). The hold stands.")
    if reading.breached:
        return LossHoldClearOutcome(outcome="refused", reason_code=LIVE_ENVELOPE_LOSS_HOLD_STANDS, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail=f"Day P&L {day_pnl:.2f} USD is still at or below the {reading.loss_limit_usd:.2f} USD loss limit. The hold stands.")
    released = resolve_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, summary_code=LIVE_ENVELOPE_LOSS_HOLD_CLEARED)
    if not released:
        return LossHoldClearOutcome(outcome="no_hold", reason_code=None, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail="The hold was already released.")
    return LossHoldClearOutcome(outcome="cleared", reason_code=None, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail=f"Loss hold cleared: day P&L {day_pnl:.2f} USD is above the {reading.loss_limit_usd:.2f} USD loss limit. New entries are admitted again.")


__all__ = ["LIVE_ENVELOPE_LOSS_HOLD_CLEARED", "LIVE_ENVELOPE_LOSS_HOLD_STANDS", "LiveEnvelopeNotInstalled", "clear_loss_hold"]
```

Router, after `get_live_verdict`:

```python
@router.post(
    "/{broker}/live-envelope/loss-hold/clear",
    response_model=LossHoldClearOutcome,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def clear_live_loss_hold(broker: str) -> LossHoldClearOutcome:
    """The guarded loss-hold clear (ADR 0059 D4; ADR 0011 §6 shape): re-observes, refuses while the breach stands."""
    if broker != "alpaca":
        raise HTTPException(status_code=404, detail={"reason": "live_envelope_unsupported_broker", "message": f"No live envelope for broker '{broker}'."})
    runtime = get_active_clerk_runtime()
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "live_envelope_not_installed", "message": "No Alpaca Clerk authority is installed."})
    try:
        return await clear_loss_hold(runtime, now_ms=now_ms_utc())
    except LiveEnvelopeNotInstalled as exc:
        raise HTTPException(status_code=503, detail={"reason": "live_envelope_not_installed", "message": str(exc)}) from exc
```

- [ ] **Step 5: Run the tests, regenerate the OpenAPI contract for the new route**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_envelope.py tests/routers/test_brokers_live_envelope.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check && cd /Users/inkant/learn-ai/Frontend && npm run codegen:openapi && npm run codegen:check && cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/ruff check app/ tests/ scripts/`
Expected: tests pass; contract regenerated and `--check` clean; codegen check clean. (Task 9 regenerates again; that is fine.)

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/schemas/alpaca_live_envelope.py PythonDataService/app/services/alpaca_live_envelope.py PythonDataService/app/routers/brokers.py PythonDataService/tests/services/test_alpaca_live_envelope.py PythonDataService/tests/routers/test_brokers_live_envelope.py contracts/openapi/python-data-service.openapi.json Frontend/src/app/api/broker.types.ts && git commit -m "feat(envelope): the guarded loss-hold clear re-observes and refuses while the breach stands

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: The verdict reports the envelope

**Files:**
- Modify: `PythonDataService/app/schemas/alpaca_live_verdict.py` (two new literals and two required fields)
- Modify: `PythonDataService/app/services/alpaca_live_verdict.py` (`observe_loss_hold`, `envelope_agreement` derivation, copy)
- Modify: `PythonDataService/app/routers/brokers.py:690-715` (`get_live_verdict` passes `loss_hold=`)
- Modify: `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts` (regenerated)
- Test: `PythonDataService/tests/services/test_alpaca_live_verdict.py` (extend), `PythonDataService/tests/broker/v2panel/test_shadow_operator_surfaces.py` (one new ASGI test)

**Interfaces:**
- Consumes: `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE`; `ActiveClerkRuntime.sqlite_repository`, `.clerk.live_envelope` (Task 7); `LiveEnvelopeGate.agreement` (Task 1).
- Produces: `EnvelopeAgreement = Literal["not_applicable", "unsealed", "agreed", "disagreed"]`; `LossHoldState = Literal["not_applicable", "clear", "held"]`; `AlpacaLiveVerdict.envelope_agreement`, `.loss_hold` (required); `observe_loss_hold(runtime: ActiveClerkRuntime | None) -> LossHoldState`; `alpaca_live_verdict(..., loss_hold: LossHoldState | None = None)`.

- [ ] **Step 1: Write the failing tests**

Extend `tests/services/test_alpaca_live_verdict.py` (read it first for its runtime/settings doubles):

```python
def test_paper_reports_the_envelope_as_not_applicable(paper_settings, sqlite_runtime) -> None:
    verdict = alpaca_live_verdict(settings=paper_settings, runtime=sqlite_runtime, now_ms=NOW_MS)
    assert (verdict.envelope_agreement, verdict.loss_hold) == ("not_applicable", "not_applicable")


def test_an_agreed_live_account_is_unsealed_until_arming_and_shows_the_hold(live_settings, shadow_runtime_double) -> None:
    clear = alpaca_live_verdict(settings=live_settings, runtime=shadow_runtime_double, now_ms=NOW_MS, shadow_state="none", loss_hold="clear")
    assert (clear.envelope_agreement, clear.loss_hold) == ("unsealed", "clear")
    held = alpaca_live_verdict(settings=live_settings, runtime=shadow_runtime_double, now_ms=NOW_MS, shadow_state="none", loss_hold="held")
    assert held.loss_hold == "held"
    assert "loss hold" in held.headline
    assert "POST /api/brokers/alpaca/live-envelope/loss-hold/clear" in held.detail


def test_observe_loss_hold_reads_the_durable_hold_on_a_composed_shadow_runtime(shadow_runtime) -> None:
    # Task 7's composed fixture; raise the hold through the sync, then:
    runtime, broker = shadow_runtime
    assert observe_loss_hold(runtime) == "clear"
    broker.unrealized = -5_000.0
    await runtime.envelope_sync.tick()
    assert observe_loss_hold(runtime) == "held"
    assert observe_loss_hold(None) == "not_applicable"
```

New ASGI test in `test_shadow_operator_surfaces.py`: raise the hold via `runtime.envelope_sync.tick()` after setting the broker double's unrealized loss, `GET /api/brokers/alpaca/live-verdict` → `loss_hold == "held"`, `envelope_agreement == "unsealed"`, `final_verdict == "live-unarmed"`.

- [ ] **Step 2: Run to verify failure**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py -q -p no:cacheprovider`
Expected: FAIL (`unexpected keyword argument 'loss_hold'`).

- [ ] **Step 3: Implement**

Schema: add `EnvelopeAgreement = Literal["not_applicable", "unsealed", "agreed", "disagreed"]`, `LossHoldState = Literal["not_applicable", "clear", "held"]`; fields `envelope_agreement: EnvelopeAgreement` directly after `envelope_state`, `loss_hold: LossHoldState` after it. Update the module docstring's slice note ("Slice 5 fills `envelope_agreement` and `loss_hold` from the live envelope and the durable loss hold").

Service:

```python
def observe_loss_hold(runtime: ActiveClerkRuntime | None) -> LossHoldState:
    """Whether the durable loss hold stands on the installed authority (read-only)."""
    if runtime is None or runtime.authority_kind != "shadow":
        return "not_applicable"
    repo = runtime.sqlite_repository
    if repo is None:
        return "not_applicable"
    active = repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None)
    return "held" if active is not None else "clear"
```

In `alpaca_live_verdict(...)`: every return under `settings is None`, disagreement/unobserved, and paper sets `envelope_agreement="not_applicable", loss_hold="not_applicable"`. The agreed-live return sets `envelope_agreement=(runtime.clerk.live_envelope.agreement if runtime is not None and runtime.clerk is not None and runtime.clerk.live_envelope is not None else "unsealed")` and `loss_hold=(loss_hold if loss_hold is not None else "not_applicable")`. When `loss_hold == "held"`, the headline gains ` — loss hold` and the detail gains the sentence `" The account is in loss hold: every ENTER is refused until an operator clears it with POST /api/brokers/alpaca/live-envelope/loss-hold/clear; exits still run."`

Router: `loss_hold=observe_loss_hold(runtime)` alongside `shadow_state=`.

- [ ] **Step 4: Regenerate the contract and run the suites**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check && cd /Users/inkant/learn-ai/Frontend && npm run codegen:openapi && npm run codegen:check && cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py tests/broker/v2panel/test_shadow_operator_surfaces.py tests/routers -q -p no:cacheprovider -k "verdict or live_envelope or brokers" && .venv/bin/ruff check app/ tests/ scripts/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/schemas/alpaca_live_verdict.py PythonDataService/app/services/alpaca_live_verdict.py PythonDataService/app/routers/brokers.py PythonDataService/tests/services/test_alpaca_live_verdict.py PythonDataService/tests/broker/v2panel/test_shadow_operator_surfaces.py contracts/openapi/python-data-service.openapi.json Frontend/src/app/api/broker.types.ts && git commit -m "feat(envelope): the live verdict reports envelope agreement and the loss hold

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Frontend — the banner shows the hold; closed copy for the hold code

**Files:**
- Modify: `Frontend/src/app/shell/alpaca-live-banner.component.ts`, `Frontend/src/app/shell/alpaca-live-banner.component.spec.ts`
- Modify: `Frontend/src/app/services/alpaca-live-verdict.service.spec.ts` (fixture gains the two fields)
- Modify: `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-emergency-copy.ts`, `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-vocabulary.snapshot.json`

**Interfaces:**
- Consumes: the regenerated `AlpacaLiveVerdict` type (`envelope_agreement`, `loss_hold`).
- Produces: a `.alpaca-banner__hold` chip rendered only when `loss_hold === 'held'`; the closed-vocabulary entry `LIVE_ENVELOPE_LOSS_HOLD`.

- [ ] **Step 1: Find how the vocabulary snapshot is regenerated and asserted**

Run: `cd /Users/inkant/learn-ai/Frontend && grep -rn "vocabulary.snapshot" src package.json | head -5`. Follow that mechanism (a spec that compares the copy map to the snapshot, and a script or `--update` path that rewrites it).

- [ ] **Step 2: Write the failing spec**

In `alpaca-live-banner.component.spec.ts`, beside the existing live-unarmed case (copy its verdict fixture and add `envelope_agreement: 'unsealed', loss_hold: 'clear'` to every fixture in both spec files):

```ts
it('shows the loss hold on a live account and nothing when clear', async () => {
  await renderWithVerdict({ ...LIVE_UNARMED, loss_hold: 'held' });
  expect(screen.getByRole('status')).toHaveTextContent('loss hold');
  await renderWithVerdict({ ...LIVE_UNARMED, loss_hold: 'clear' });
  expect(screen.getByRole('status')).not.toHaveTextContent('loss hold');
});
```

(`renderWithVerdict` / `LIVE_UNARMED` are whatever helper and fixture names the spec already uses — reuse, do not invent parallel ones.)

- [ ] **Step 3: Run to verify failure**

Run: `cd /Users/inkant/learn-ai/Frontend && npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts'`
Expected: FAIL (type error on the fixture or missing text).

- [ ] **Step 4: Implement**

Template, after the armed-count span:

```html
        @if (v.loss_hold === 'held') {
          <span class="alpaca-banner__hold">· loss hold</span>
        }
```

Style: `.alpaca-banner__hold { font-weight: 700; color: var(--bear); }`. Copy map entry:

```ts
  LIVE_ENVELOPE_LOSS_HOLD: {
    label: 'Loss hold',
    explanation:
      "Today's loss reached the account's limit. New entries are refused account-wide until an operator clears the hold; exits still run.",
  },
```

Regenerate the vocabulary snapshot by the Step 1 mechanism.

- [ ] **Step 5: Run the specs and lint**

Run: `cd /Users/inkant/learn-ai/Frontend && npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts' && npx ng test --include='src/app/services/alpaca-live-verdict.service.spec.ts' && npx ng test --include='<the vocabulary snapshot spec path from Step 1>' && npx eslint src/ --max-warnings 0 && npm run codegen:check`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add Frontend/src/app/shell/alpaca-live-banner.component.ts Frontend/src/app/shell/alpaca-live-banner.component.spec.ts Frontend/src/app/services/alpaca-live-verdict.service.spec.ts Frontend/src/app/components/broker/v2-panel/lib/broker-v2-emergency-copy.ts Frontend/src/app/components/broker/v2-panel/lib/broker-v2-vocabulary.snapshot.json && git commit -m "feat(envelope): the live banner names the loss hold; closed copy for LIVE_ENVELOPE_LOSS_HOLD

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Docs and registries

**Files:**
- Create: `docs/references/alpaca-live-envelope.md`
- Modify: `docs/math-sources-of-truth.md` (one row), `docs/architecture/engine-authority-map.md` (one row), `CONTEXT.md:502-515` (Cash bound, Day P&L entries)
- Modify: `docs/references/alpaca-shadow-authority.md` (one paragraph: shadow rehearses the envelope; a refusal is a twin decision mismatch)

- [ ] **Step 1: Read the neighbours**

Run: `sed -n 1,60p docs/references/alpaca-shadow-authority.md`; `grep -n "alpaca-regulatory-fees\|Shadow gate\|fill_models" docs/math-sources-of-truth.md docs/architecture/engine-authority-map.md`. Match their row shapes exactly.

- [ ] **Step 2: Write `docs/references/alpaca-live-envelope.md`**

Sections, in this order: **What it is** (the two rules, in the owner's words from the plan's R1–R5); **Where it runs** (`accept_enter`, sibling of `require_admission`; the sync; shadow rehearsal); **The facts** (cash observation, reservations with the fills-aware rule, day P&L with the unknown rule, `last_equity`); **Refusals** (a table of the four reason codes, when each fires, and that all are transient at the runner); **Loss hold** (raised by the sync only, never released by it, projected as a hold, exits still run); **Clearing it** — the exact `curl -X POST -H "X-Data-Plane-Control-Secret: $DATA_PLANE_CONTROL_SECRET" http://localhost:8000/api/brokers/alpaca/live-envelope/loss-hold/clear` and the three outcomes; **Residuals** (market slippage above the decision close; unrealized P&L under shadow is the live account's, not the synthesized positions'; external orders today make the fact unknown; no Frontend button yet); **Decision record**: ADR 0059 D4, owner rulings 2026-09-08. Link every code path by repo-root-relative path.

- [ ] **Step 3: Registries and glossary**

- `docs/math-sources-of-truth.md`: a row for "Day P&L / loss limit" → canonical `PythonDataService/app/broker/alpaca/clerk/sqlite/day_pnl.py` + `PythonDataService/app/broker/alpaca/clerk/live_envelope.py`, validated by `tests/broker/alpaca/clerk/sqlite/test_day_pnl.py` and `tests/broker/alpaca/clerk/test_live_envelope.py`, reference ADR 0059 D4.
- `docs/architecture/engine-authority-map.md`: a row for the envelope (owner: `live_envelope.py` + `envelope_admission.py` + `live_envelope_sync.py`; consumers: `enter.py`, `runtime.py`, the verdict; the hold code).
- `CONTEXT.md` **Cash bound** entry becomes: "the envelope rule that a new ENTER's notional plus every working ENTER's unfilled notional may not exceed broker-observed cash. It reads cash, not buying power, so it is the same on a cash account and a margin account. _Avoid_: no margin, 1× leverage, cash-only". **Day P&L** entry gains: "It is unknown, not zero, when an external order was seen today or the broker reports no previous-close equity."
- `docs/references/alpaca-shadow-authority.md`: one paragraph under its gate section: the shadow authority rehearses the envelope on the live account's real cash (net of what its own synthesized fills would have spent); an envelope refusal in shadow is a twin decision mismatch, so that day does not count.

- [ ] **Step 4: Run the docs contract**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q -p no:cacheprovider`
Expected: pass (links resolve from the repo root).

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add docs/references/alpaca-live-envelope.md docs/references/alpaca-shadow-authority.md docs/math-sources-of-truth.md docs/architecture/engine-authority-map.md CONTEXT.md && git commit -m "docs(envelope): the live envelope reference, registries, and the sharpened glossary

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Gates on the final tree

- [ ] **Step 1: Lint at project scope**

Run: `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/ruff check app/ tests/ scripts/ && cd /Users/inkant/learn-ai/Frontend && npx eslint src/ --max-warnings 0`
Expected: both exit 0.

- [ ] **Step 2: Targeted Python suites (every surface touched and every consumer of a shared helper)**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca tests/broker/v2panel tests/services tests/routers tests/contracts -q -p no:cacheprovider`
Expected: all pass. Report the exact `N passed` line.

- [ ] **Step 3: Contracts and import**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main" && cd /Users/inkant/learn-ai/Frontend && npm run codegen:check`
Expected: all clean.

- [ ] **Step 4: Frontend specs touched**

Run: `cd /Users/inkant/learn-ai/Frontend && npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts' && npx ng test --include='src/app/services/alpaca-live-verdict.service.spec.ts'`
Expected: pass.

- [ ] **Step 5: Diffstat sanity**

Run: `cd /Users/inkant/learn-ai && git diff --stat master...HEAD | tail -3 && git status --short | wc -l`
Expected: only slice-5 paths; a clean tree.

No commit in this task. The controller then runs the thermo review and the whole-branch review, one fix wave each at most, then pushes and opens the PR.

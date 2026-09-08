# Live Slice 3 — Extended Hours Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A sealed instance can decide and submit outside the regular session on the Alpaca paper account: the order leg carries the session, the decision clock fires across the broker's declared extended window, every extended-session program leg is a marketable limit anchored to the decision bar, unfilled orders resolve through the machinery that exists, and a `limit_touch` fill model is ready for the shadow port — ADR 0059 slice 3.

**Architecture:** The extended window is **broker capability data** (`BrokerCapabilities.extended_hours_window`), never calendar authority and never a literal in session logic; the canonical session authority (`app/services/session_authority.py`) learns to resolve `PRE`/`RTH`/`POST` from the calendar's regular session plus that window. The decision clock (`app/services/decision_clock.py`) gains an `extended` trigger set; the continuity policy offers it to `use_rth=False` bindings. One pure module (`app/broker/alpaca/clerk/program_leg.py`) shapes every program leg from the decision bar, the session, and the environment allowances; the Clerk runtime uses it for ENTER and threads the shape into the EXIT machine's durable reducing-order facts. Unfilled handling is pinned by tests against existing folds and uncertainties. The `limit_touch` fill model is a pure function the slice-4 shadow port will call; the `sim:` world stops mislabelling limit legs.

**Tech Stack:** Python 3.12, Pydantic v2, `decimal`, `zoneinfo`, pytest (`asyncio_mode=auto`), the SQLite Clerk test harness under `tests/broker/alpaca/clerk/sqlite/`.

**Spec:** `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` — Decision 5 (five numbered points) and Consequences slice 3 ("the leg flag and validator, the adapter field, the session-wide decision clock, the marketable-limit anchor, the unfilled handling, and the `limit_touch` shadow fill model. Landed and exercised on the paper account."). Vocabulary: `CONTEXT.md` § "Live account, shadow, and risk envelope" (**Marketable limit anchor**).

## Global Constraints

- **ADR 0059 D5.1:** `extended_hours=True` on a leg requires `order_type=LIMIT` and `time_in_force ∈ {DAY, GTC}`, enforced by the contract validator so a vendor 422 for this cause is impossible by construction.
- **ADR 0059 D5.2:** the regular session's boundaries come only from `app/lean_sidecar/trading_calendar.py`; the 04:00–20:00 ET window is a broker capability fact carried on `BrokerCapabilities` — **no `time(4, 0)` / `time(20, 0)` / `240` / `1200` literal may appear in session logic**; the numbers live once, on the Alpaca capability constant, with the vendor citation.
- **ADR 0059 D5.3:** outside the regular session a program leg is `LIMIT`, `time_in_force=DAY`, `extended_hours=True`, priced at `close × (1 + entry_bps/10⁴)` for a buy and `close × (1 − exit_bps/10⁴)` for a sell; inside the regular session a program leg stays market `DAY`. `GTC` is never used for program orders. The allowances come from `ALPACA_LIVE_XH_ENTRY_BPS` / `ALPACA_LIVE_XH_EXIT_BPS` and have **no default in code** (ADR 0059 D4).
- **ADR 0059 D5.4:** an unfilled extended-hours ENTER cancelled by the vendor folds through existing terminal handling with no exposure; an unfilled extended-hours EXIT is an **uncertainty** — surfaced, never silently re-priced.
- **ADR 0059 D5.5:** `limit_touch` eligibility starts with the first bar **after** the decision bar; a bar that reaches the limit fills at the limit.
- **Sealed artifacts — do not edit:** `app/lean_sidecar/trading_calendar.py`, `app/utils/timestamps.py`, `app/engine/consolidators/trade_bar_consolidator.py` are in every program's `artifact_paths` (`app/engine/strategy/registry.py`); editing them breaks every golden qualification receipt. Import them; never change them.
- **Temporal rigor:** every wire/storage temporal value is `int64 ms UTC`; wall-clock → UTC goes through `ZoneInfo("America/New_York")`; sessions are half-open `[open, close)`; a bar `[start_ms, end_ms)` is labelled by its close.
- **Hash-chained facts:** any optional field added to a durable facts dataclass (`app/broker/alpaca/clerk/sqlite/facts.py`) is **omitted from `to_facts_json` when it holds its default** and defaulted in `from_facts_json`, so existing rows re-parse and canonical JSON for unchanged shapes is byte-identical. The manual-ticket instruction hash is computed over a leg payload that omits `extended_hours` when `False`.
- **Math Provenance Contract** (`.claude/skills/learn-ai-validation`): `marketable_limit_price`, `extended_trigger_instants` and `limit_touch_fill` carry `Formula` / `Reference` / `Canonical implementation` / `Validated against`; `docs/math-sources-of-truth.md` gets a row each; `docs/architecture/engine-authority-map.md` gets one row for the slice (AGENTS.md requires both registries per new engine path).
- **Repo rules:** `from __future__ import annotations`; type hints on every signature; Pydantic v2 only; no `print`; no silent `except`; structured logging with `extra={"action": ...}`; `ruff check app/ tests/` clean at project scope; tests via `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <paths> -q -p no:cacheprovider`; ruff via `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff`. Use absolute paths in every command.
- **Contracts:** a schema change visible on the wire (`BrokerOrder`, admission facts) requires `.venv/bin/python scripts/export_openapi_contract.py` (from `PythonDataService/`) and `npm run codegen:openapi` (from `Frontend/`), committed with the code (Task 10). Docs edits are guarded by `tests/contracts` (links resolve from the repository root: `docs/references/...`).
- **Commits:** stage explicit paths (never `git add -A`). Every commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Podman is not required.** The host venv is the runner. Never `podman exec`.

## Vendor facts (pinned 2026-09-08; the plan's single source for every number below)

| Fact | Value | Source |
|---|---|---|
| Pre-market | 04:00–09:30 ET, Mon–Fri | Alpaca "Orders at Alpaca" — Extended Hours Trading (`https://docs.alpaca.markets/docs/orders-at-alpaca`) |
| After-hours | 16:00–20:00 ET, Mon–Fri | same |
| Overnight | 20:00–04:00 ET, Sun–Fri (Blue Ocean ATS; asset eligibility via the assets endpoint) | same — **out of this slice's decision clock (Ruling R1)** |
| Extended-hours order shape | `type=limit` with `limit_price`; `time_in_force ∈ {day, gtc}`; `extended_hours=true`. "Any other type of orders will be rejected with an error." | same |
| Day order lifetime | "A day order is eligible for execution only on the day it is live … If unfilled after the closing auction, it is automatically canceled." With extended eligibility "the order can also execute during supported extended hours." | same — Time in Force |
| Market order outside RTH | "Orders not eligible for extended hours submitted after 4:00pm ET will be queued up for release the next trading day." | same |
| Early-close days | **Not documented by Alpaca.** The declared window is applied unchanged; an order the venue cancels earlier folds through D5.4. | Ruling R2 |
| `/v2/calendar` `session_open` / `session_close` | Unexplained legacy values (`0700` / `1900`) that never matched the extended window; rejected as a source. | Alpaca community forum thread 2400 (2020-08 → 2023, unanswered) |

## File structure

- Modify `PythonDataService/app/broker/contract/capabilities.py` — `ExtendedHoursWindow`; `BrokerCapabilities.extended_hours_window`.
- Modify `PythonDataService/app/broker/contract/models.py` — `BrokerOrderLeg.extended_hours` + validator; `BrokerOrder.extended_hours`.
- Modify `PythonDataService/app/broker/alpaca/broker.py` — `ALPACA_EXTENDED_HOURS_WINDOW`; capability constants carry it.
- Modify `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py` — capability window; honest limit-leg fills (Task 8).
- Modify `PythonDataService/app/broker/alpaca/adapter.py` — forward and ingest `extended_hours`.
- Modify `PythonDataService/app/broker/alpaca/clerk/sqlite/facts.py` — manual-ticket leg rule + instruction payload helper; `ExitReducingOrderCreatedFacts` shape fields.
- Create `PythonDataService/app/broker/alpaca/marketable_limit.py` — `marketable_limit_price`, `ExtendedHoursAllowances`.
- Modify `PythonDataService/app/services/session_authority.py` — declared-window resolution (`extended_window` parameter, `extended_session_bounds_ms`, `decision_session_phase`).
- Modify `PythonDataService/app/services/market_data_capability_service.py`, `bot_start_admission.py`, `bot_resume_admission.py`, `broker_v2_panel/market_pulse.py`, `app/schemas/run_admission.py` — thread the window; widen the source literal; extended-hours admission fact.
- Modify `PythonDataService/app/marketdata/feed.py` — `DecisionSession = Literal["rth", "extended"]`.
- Modify `PythonDataService/app/services/decision_clock.py` — `extended_trigger_instants`, generalised `next_trigger_ms` / `next_trigger_function`, `decision_session_close_ms`.
- Modify `PythonDataService/app/services/feed_continuity_policy.py` — offer `extended`.
- Modify `PythonDataService/app/services/bot_trade_strategy.py` — window-aware bar filter, session-close flush, policy wiring.
- Create `PythonDataService/app/broker/alpaca/clerk/program_leg.py` — `ProgramLegPolicy`, `ProgramLegRefused`, `LegShape`, `shape_program_leg`.
- Modify `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py`, `exit.py`, `exit_resolution.py`, `active_protocol.py`, `active_authority.py` — leg shaping on ENTER; shape threaded into EXIT; policy construction.
- Create `PythonDataService/app/broker/alpaca/clerk/fill_models.py` — `limit_touch_fill`.
- Create `docs/references/alpaca-extended-hours.md`; modify `docs/math-sources-of-truth.md`, `docs/architecture/engine-authority-map.md`, `CONTEXT.md`.
- Tests beside each module (named per task). Regenerate `contracts/openapi/python-data-service.openapi.json` and `Frontend/src/app/api/broker.types.ts`.

---

### Task 1: The leg carries the session; the capability carries the window

**Files:**
- Modify: `PythonDataService/app/broker/contract/capabilities.py`
- Modify: `PythonDataService/app/broker/contract/models.py` (`BrokerOrderLeg` at ~68–110, `BrokerOrder` at ~199–224)
- Modify: `PythonDataService/app/broker/alpaca/broker.py` (~33–48)
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py` (~44–53)
- Modify: `PythonDataService/app/broker/alpaca/adapter.py` (`to_alpaca_order_request` ~304–320, `from_alpaca_order` ~330–360)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/facts.py` (`validate_manual_order_accepted_facts` ~868–890)
- Test: `PythonDataService/tests/broker/contract/test_models.py`, `tests/broker/contract/test_capabilities_window.py` (new), `tests/broker/alpaca/test_adapter_orders.py`, `tests/broker/alpaca/test_capabilities.py`, `tests/broker/alpaca/clerk/sqlite/test_manual_ticket_legs.py` (new)

**Interfaces:**
- Consumes: `OrderType`, `TimeInForce`, `OrderSide` (`app.broker.contract.models`).
- Produces (later tasks rely on these exact names):
  - `app.broker.contract.capabilities.ExtendedHoursWindow(open_minute_et: int, close_minute_et: int)` (frozen Pydantic model) and `BrokerCapabilities.extended_hours_window: ExtendedHoursWindow | None`.
  - `app.broker.alpaca.broker.ALPACA_EXTENDED_HOURS_WINDOW`.
  - `BrokerOrderLeg.extended_hours: bool = False`; `BrokerOrder.extended_hours: bool = False`.
  - `app.broker.alpaca.clerk.sqlite.facts.leg_instruction_payload(leg: BrokerOrderLeg) -> dict[str, object]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/broker/contract/test_models.py`:

```python
def test_extended_hours_leg_requires_a_day_or_gtc_limit() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=100.25, extended_hours=True
    )

    assert leg.extended_hours is True
    assert leg.time_in_force is TimeInForce.DAY


def test_extended_hours_market_leg_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extended-hours orders must be limit orders"):
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, extended_hours=True)


def test_regular_leg_defaults_to_not_extended() -> None:
    assert BrokerOrderLeg(symbol="SPY", side="buy", quantity=1).extended_hours is False
```

Create `tests/broker/contract/test_capabilities_window.py`:

```python
"""The extended window is capability data, and it agrees with the support flag."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow


def _capabilities(**overrides: object) -> BrokerCapabilities:
    base: dict[str, object] = {
        "broker": "x",
        "paper_only": True,
        "supports_fractional": True,
        "supports_extended_hours": True,
        "extended_hours_window": ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60),
        "supported_order_types": ("market", "limit"),
        "data_feed": "test",
        "bars_may_gap": False,
        "max_stream_symbols": 1,
        "max_concurrent_streams": 1,
        "rest_rate_limit_per_min": 1,
    }
    base.update(overrides)
    return BrokerCapabilities(**base)


def test_window_must_open_before_it_closes() -> None:
    with pytest.raises(ValidationError):
        ExtendedHoursWindow(open_minute_et=20 * 60, close_minute_et=4 * 60)


def test_supported_extended_hours_requires_a_window() -> None:
    with pytest.raises(ValidationError, match="extended_hours_window"):
        _capabilities(extended_hours_window=None)


def test_unsupported_extended_hours_forbids_a_window() -> None:
    with pytest.raises(ValidationError, match="extended_hours_window"):
        _capabilities(supports_extended_hours=False)


def test_unsupported_extended_hours_without_a_window_is_valid() -> None:
    assert _capabilities(supports_extended_hours=False, extended_hours_window=None).extended_hours_window is None
```

Append to `tests/broker/alpaca/test_capabilities.py`:

```python
from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.synthetic_broker import SYNTHETIC_CAPABILITIES


@pytest.mark.parametrize("capabilities", [*_DESCRIPTORS, SYNTHETIC_CAPABILITIES])
def test_every_descriptor_declares_the_alpaca_extended_window(capabilities) -> None:
    assert capabilities.supports_extended_hours is True
    assert capabilities.extended_hours_window == ALPACA_EXTENDED_HOURS_WINDOW


def test_the_declared_window_is_alpacas_documented_session() -> None:
    # 04:00–20:00 ET, Alpaca "Orders at Alpaca" § Extended Hours Trading (see docs/references/alpaca-extended-hours.md).
    assert ALPACA_EXTENDED_HOURS_WINDOW.open_minute_et == 4 * 60
    assert ALPACA_EXTENDED_HOURS_WINDOW.close_minute_et == 20 * 60
```

In `tests/broker/alpaca/test_adapter_orders.py`, change the two body-equality tests so the expected dict carries `"extended_hours": False`, and add:

```python
def test_to_alpaca_order_request_forwards_extended_hours_on_a_limit_leg() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY", side="buy", quantity=2, order_type="limit", limit_price=100.25, extended_hours=True
    )

    body = to_alpaca_order_request(leg, client_order_id="learn-ai/spy/v1:xh1")

    assert body["extended_hours"] is True
    assert body["type"] == "limit"
    assert body["time_in_force"] == "day"


def test_from_alpaca_order_reads_extended_hours_and_defaults_it_false() -> None:
    payload = _order_payload()  # reuse the module's existing raw-order fixture helper
    assert from_alpaca_order(payload, observed_at_ms=_OBSERVED).extended_hours is False
    assert from_alpaca_order({**payload, "extended_hours": True}, observed_at_ms=_OBSERVED).extended_hours is True
```

(If the module's fixture helper has another name, use that name; do not add a second raw-order fixture.)

Create `tests/broker/alpaca/clerk/sqlite/test_manual_ticket_legs.py`:

```python
"""Manual tickets stay regular-session, and their instruction hash survives the new leg field."""

from __future__ import annotations

import hashlib

from app.broker.alpaca.clerk.sqlite.facts import leg_instruction_payload
from app.broker.contract.models import BrokerOrderLeg
from app.utils.canonical_json import canonicalize


def test_instruction_payload_omits_the_flag_for_a_regular_leg() -> None:
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)

    payload = leg_instruction_payload(leg)

    assert "extended_hours" not in payload
    # Byte-identical to the pre-slice-3 hash input for the same leg.
    legacy = {k: v for k, v in leg.model_dump(mode="json").items() if k != "extended_hours"}
    assert hashlib.sha256(canonicalize(payload).encode("utf-8")).hexdigest() == (
        hashlib.sha256(canonicalize(legacy).encode("utf-8")).hexdigest()
    )


def test_instruction_payload_keeps_the_flag_for_an_extended_leg() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=100.25, extended_hours=True
    )

    assert leg_instruction_payload(leg)["extended_hours"] is True
```

Also add, in the existing manual-order facts test module (grep `validate_manual_order_accepted_facts` under `tests/broker/alpaca/clerk/sqlite/`), one case asserting that an accepted-facts record whose `leg` has `extended_hours=True` is rejected with `match="regular-session"`. Follow that module's fixture pattern for building the facts.

(`canonicalize` lives wherever `facts.py` imports it from — check the import at the top of `facts.py` and use the same module path.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/contract tests/broker/alpaca/test_adapter_orders.py tests/broker/alpaca/test_capabilities.py tests/broker/alpaca/clerk/sqlite/test_manual_ticket_legs.py -q -p no:cacheprovider`
Expected: FAIL — `extended_hours` unknown field / `ExtendedHoursWindow` import error / `leg_instruction_payload` missing.

- [ ] **Step 3: Implement**

`app/broker/contract/capabilities.py` — add above `BrokerCapabilities`:

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator

_MINUTES_PER_DAY = 24 * 60


class ExtendedHoursWindow(BaseModel):
    """The broker's extended session as minutes past midnight, America/New_York.

    Capability data (ADR 0059 D5.2), not calendar authority: the regular
    session's bounds still come only from the canonical calendar module.
    Resolving an instant against this window happens in
    ``app.services.session_authority``; nothing else may read the minutes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    open_minute_et: int = Field(ge=0, lt=_MINUTES_PER_DAY)
    close_minute_et: int = Field(gt=0, le=_MINUTES_PER_DAY)

    @model_validator(mode="after")
    def _opens_before_it_closes(self) -> ExtendedHoursWindow:
        if self.open_minute_et >= self.close_minute_et:
            raise ValueError("extended_hours_window must open before it closes")
        return self
```

and on `BrokerCapabilities`, after `supports_extended_hours: bool`:

```python
    # The declared extended session (ADR 0059 D5.2); present iff supported.
    extended_hours_window: ExtendedHoursWindow | None = None
```

plus:

```python
    @model_validator(mode="after")
    def _window_agrees_with_support(self) -> BrokerCapabilities:
        if self.supports_extended_hours and self.extended_hours_window is None:
            raise ValueError("supports_extended_hours requires an extended_hours_window")
        if not self.supports_extended_hours and self.extended_hours_window is not None:
            raise ValueError("extended_hours_window is only meaningful when extended hours are supported")
        return self
```

`app/broker/alpaca/broker.py` — above `ALPACA_PAPER_CAPABILITIES`:

```python
# Alpaca's documented extended session, 04:00–20:00 ET ("Orders at Alpaca" §
# Extended Hours Trading, verified 2026-09-08; docs/references/alpaca-extended-hours.md).
# The overnight session (20:00–04:00) is a separate venue and is not part of
# the decision clock in slice 3 (ADR 0059 D5.2; ruling R1 in the slice-3 plan).
ALPACA_EXTENDED_HOURS_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
```

and `extended_hours_window=ALPACA_EXTENDED_HOURS_WINDOW,` in `ALPACA_PAPER_CAPABILITIES` (the live constant is a `model_copy` and inherits it). Import `ExtendedHoursWindow` from `app.broker.contract.capabilities`.

`app/broker/alpaca/clerk/synthetic_broker.py` — `SYNTHETIC_CAPABILITIES` gains `extended_hours_window=ALPACA_EXTENDED_HOURS_WINDOW` (import from `app.broker.alpaca.broker`; the synthetic world is a paper Alpaca environment by construction, so it declares the same window).

`app/broker/contract/models.py` — on `BrokerOrderLeg`, after `time_in_force`:

```python
    # ADR 0059 D5.1: the leg carries the session. True only for a LIMIT leg
    # with DAY or GTC — enforced below so the vendor's 422 is unreachable.
    extended_hours: bool = False
```

and extend `_limit_price_matches_order_type` (keep its name) with, before `return self`:

```python
        if self.extended_hours and self.order_type is not OrderType.LIMIT:
            raise ValueError("Alpaca extended-hours orders must be limit orders with a limit_price.")
        if self.extended_hours and self.time_in_force not in {TimeInForce.DAY, TimeInForce.GTC}:
            raise ValueError("Alpaca extended-hours orders must use DAY or GTC time in force.")
```

On `BrokerOrder`, after `stop_price: float | None`:

```python
    extended_hours: bool = False
```

`app/broker/alpaca/adapter.py` — in `to_alpaca_order_request`, add `"extended_hours": leg.extended_hours,` to `body` (after `time_in_force`, before `client_order_id`); update the docstring's sentence about S2 to mention that the session flag is always forwarded explicitly. In `from_alpaca_order`, add `extended_hours=bool(payload.get("extended_hours") or False),` after `stop_price=...`.

`app/broker/alpaca/clerk/sqlite/facts.py` — add a module-level helper next to the manual validators:

```python
def leg_instruction_payload(leg: BrokerOrderLeg) -> dict[str, object]:
    """The canonical leg payload a manual instruction hash is computed over.

    ``extended_hours`` is omitted when ``False`` so every ticket accepted
    before the field existed keeps validating against its stored hash
    (the hash-chained schema-evolution rule).
    """
    payload = leg.model_dump(mode="json")
    if not leg.extended_hours:
        payload.pop("extended_hours", None)
    return payload
```

In `validate_manual_order_accepted_facts`: replace `canonicalize(leg.model_dump(mode="json"))` with `canonicalize(leg_instruction_payload(leg))`, and extend the shape check so `leg.extended_hours` is rejected with the message `"manual tickets accept only regular-session BUY or SELL market/limit DAY/GTC equity legs"`. Then grep the repo for every other producer of a manual `instruction_hash` (`grep -rn "instruction_hash=" app/`) and route each through `leg_instruction_payload` — there must be exactly one payload rule.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command, plus `tests/broker/alpaca/clerk/sqlite` (manual-ticket suites hash legs) and `tests/broker/alpaca/test_schema_drift.py`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/contract/capabilities.py PythonDataService/app/broker/contract/models.py PythonDataService/app/broker/alpaca/broker.py PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py PythonDataService/app/broker/alpaca/adapter.py PythonDataService/app/broker/alpaca/clerk/sqlite/facts.py PythonDataService/tests/broker/contract PythonDataService/tests/broker/alpaca/test_adapter_orders.py PythonDataService/tests/broker/alpaca/test_capabilities.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_manual_ticket_legs.py <the manual-facts test module you edited> && git commit -m "feat(broker): the order leg carries the session and the capability carries the extended window (ADR 0059 D5.1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The marketable-limit anchor (canonical math)

**Files:**
- Create: `PythonDataService/app/broker/alpaca/marketable_limit.py`
- Test: `PythonDataService/tests/broker/alpaca/test_marketable_limit.py`

**Interfaces:**
- Consumes: `OrderSide` (`app.broker.contract.models`); `AlpacaSettings` (`app.broker.alpaca.config`).
- Produces:
  - `marketable_limit_price(*, side: OrderSide, close: Decimal, allowance_bps: Decimal) -> Decimal`
  - `ExtendedHoursAllowances(entry_bps: Decimal, exit_bps: Decimal)` (frozen dataclass) with `from_settings(settings: AlpacaSettings) -> ExtendedHoursAllowances | None` and `from_environment() -> ExtendedHoursAllowances | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""Marketable-limit anchor: the decision bar's close moved by the allowance in the trade's direction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.models import OrderSide


@pytest.mark.parametrize(
    ("side", "close", "bps", "expected"),
    [
        # 10 bps on a $100 close: buy up to 100.10, sell down to 99.90 — exact.
        (OrderSide.BUY, Decimal("100.00"), Decimal("10"), Decimal("100.10")),
        (OrderSide.SELL, Decimal("100.00"), Decimal("10"), Decimal("99.90")),
        # Rounding is in the marketable direction: 7 bps on 123.45 is 123.536415 → buy 123.54, sell 123.363585 → 123.36.
        (OrderSide.BUY, Decimal("123.45"), Decimal("7"), Decimal("123.54")),
        (OrderSide.SELL, Decimal("123.45"), Decimal("7"), Decimal("123.36")),
        # Sub-dollar prices allow four decimals (Alpaca tick rule).
        (OrderSide.BUY, Decimal("0.5000"), Decimal("10"), Decimal("0.5005")),
        (OrderSide.SELL, Decimal("0.5000"), Decimal("10"), Decimal("0.4995")),
        # A zero allowance anchors exactly at the close.
        (OrderSide.BUY, Decimal("55.55"), Decimal("0"), Decimal("55.55")),
        # A buy that crosses the $1 band rounds up on the band it lands in: 1.00039995 → 1.01.
        (OrderSide.BUY, Decimal("0.9999"), Decimal("5"), Decimal("1.01")),
    ],
)
def test_marketable_limit_price(side: OrderSide, close: Decimal, bps: Decimal, expected: Decimal) -> None:
    assert marketable_limit_price(side=side, close=close, allowance_bps=bps) == expected


def test_a_negative_allowance_is_refused() -> None:
    with pytest.raises(ValueError, match="allowance_bps"):
        marketable_limit_price(side=OrderSide.BUY, close=Decimal("10"), allowance_bps=Decimal("-1"))


def test_a_non_positive_close_is_refused() -> None:
    with pytest.raises(ValueError, match="close"):
        marketable_limit_price(side=OrderSide.BUY, close=Decimal("0"), allowance_bps=Decimal("1"))


def test_allowances_come_from_settings_and_are_absent_when_unset() -> None:
    both = AlpacaSettings(api_key_id="k", api_secret_key="s", live_xh_entry_bps=12.5, live_xh_exit_bps=8.0)
    neither = AlpacaSettings(api_key_id="k", api_secret_key="s")
    one = AlpacaSettings(api_key_id="k", api_secret_key="s", live_xh_entry_bps=12.5)

    assert ExtendedHoursAllowances.from_settings(both) == ExtendedHoursAllowances(
        entry_bps=Decimal("12.5"), exit_bps=Decimal("8.0")
    )
    assert ExtendedHoursAllowances.from_settings(neither) is None
    assert ExtendedHoursAllowances.from_settings(one) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_marketable_limit.py -q -p no:cacheprovider`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `app/broker/alpaca/marketable_limit.py`**

```python
"""Marketable-limit anchor for program legs outside the regular session (ADR 0059 D5.3).

Formula:
    buy:  ceil_tick( close × (1 + allowance_bps / 10⁴) )
    sell: floor_tick( close × (1 − allowance_bps / 10⁴) )
    where the tick is 0.01 for a price ≥ 1 and 0.0001 below 1 (Alpaca's
    limit-price precision rule, mirrored by ``BrokerOrderLeg``'s validator).
    Rounding is always in the marketable direction, so the anchor never
    understates the allowance the operator set.
Reference:
    ADR 0059 Decision 5.3; CONTEXT.md "Marketable limit anchor"; Alpaca
    "Orders at Alpaca" § Extended Hours Trading (limit-only) — see
    docs/references/alpaca-extended-hours.md.
Canonical implementation: this file.
Validated against:
    tests/broker/alpaca/test_marketable_limit.py::test_marketable_limit_price
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import ValidationError

from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.contract.models import OrderSide

logger = logging.getLogger(__name__)

_BPS_PER_UNIT = Decimal(10_000)
_DOLLAR_TICK = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")


def marketable_limit_price(*, side: OrderSide, close: Decimal, allowance_bps: Decimal) -> Decimal:
    """The limit price a program leg carries outside the regular session."""
    if close <= 0:
        raise ValueError(f"close must be positive; got {close}")
    if allowance_bps < 0:
        raise ValueError(f"allowance_bps must be non-negative; got {allowance_bps}")
    fraction = allowance_bps / _BPS_PER_UNIT
    if side is OrderSide.BUY:
        raw = close * (1 + fraction)
        rounding = ROUND_CEILING
    else:
        raw = close * (1 - fraction)
        rounding = ROUND_FLOOR
    tick = _DOLLAR_TICK if raw >= 1 else _SUB_DOLLAR_TICK
    return raw.quantize(tick, rounding=rounding)


@dataclass(frozen=True)
class ExtendedHoursAllowances:
    """The operator's extended-session allowances, in basis points (ADR 0059 D4).

    Both values come from the environment file; neither has a default in
    code. ``None`` from the constructors means "not configured", which the
    leg policy and Start admission refuse explicitly — never zero.
    """

    entry_bps: Decimal
    exit_bps: Decimal

    @classmethod
    def from_settings(cls, settings: AlpacaSettings) -> ExtendedHoursAllowances | None:
        if settings.live_xh_entry_bps is None or settings.live_xh_exit_bps is None:
            return None
        return cls(
            entry_bps=Decimal(str(settings.live_xh_entry_bps)),
            exit_bps=Decimal(str(settings.live_xh_exit_bps)),
        )

    @classmethod
    def from_environment(cls) -> ExtendedHoursAllowances | None:
        """Read the process settings; absent credentials mean absent allowances.

        A synthetic (``sim:``) authority can be built in a process that has
        no Alpaca credentials at all; that is not an error, it is "no
        allowances", and it is logged so an operator can see why an
        extended-hours leg was refused.
        """
        try:
            settings = get_alpaca_settings()
        except ValidationError as exc:
            logger.info(
                "Extended-hours allowances are unavailable: Alpaca settings did not load",
                extra={"action": "extended_hours_allowances_unavailable", "error": str(exc)},
            )
            return None
        return cls.from_settings(settings)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/marketable_limit.py PythonDataService/tests/broker/alpaca/test_marketable_limit.py && git commit -m "feat(broker): the marketable-limit anchor and the environment allowances (ADR 0059 D5.3)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The session authority resolves the declared window

**Files:**
- Modify: `PythonDataService/app/services/session_authority.py`
- Modify: `PythonDataService/app/services/market_data_capability_service.py` (`extended_phase_proven_at_ms`, ~110–140)
- Modify: `PythonDataService/app/services/bot_start_admission.py` (`market_data_admission_fact`, ~518–620)
- Modify: `PythonDataService/app/services/broker_v2_panel/market_pulse.py` (`build_market_pulse`)
- Modify: `PythonDataService/app/schemas/run_admission.py` (`MarketDataAdmissionFact.session_authority_source`, ~54)
- Test: `PythonDataService/tests/services/test_session_authority_declared_window.py` (new)

**Interfaces:**
- Consumes: `ExtendedHoursWindow` (Task 1); `is_trading_day`, `next_trading_day`, `session_window_for_date` (`app.lean_sidecar.trading_calendar` — sealed, import only); `to_ms_utc` (`app.utils.timestamps` — sealed, import only).
- Produces:
  - `session_authority.et_minute_of_day_ms(day: date, minute_of_day: int) -> int`
  - `session_authority.ExtendedSessionBounds(open_ms, rth_open_ms, rth_close_ms, close_ms)` and `extended_session_bounds_ms(session_date: date, *, window: ExtendedHoursWindow) -> ExtendedSessionBounds`
  - `session_state_at_ms(..., extended_window: ExtendedHoursWindow | None = None)`; `SessionAuthoritySource` gains `"broker_declared_window"`.
  - `extended_phase_proven_at_ms(*, now_ms, symbol, account_id, extended_window=None)`; `market_data_admission_fact(..., extended_window=None)`; `build_market_pulse(..., extended_window=None)`.

- [ ] **Step 1: Write the failing tests** — `tests/services/test_session_authority_declared_window.py`

```python
"""The broker's declared window resolves PRE/RTH/POST around the calendar's regular session."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.session_authority import (
    et_minute_of_day_ms,
    extended_session_bounds_ms,
    session_state_at_ms,
)
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_REGULAR = date(2026, 9, 2)  # Wednesday
_EARLY = date(2026, 11, 27)  # 13:00 ET close
_SUNDAY = date(2026, 9, 6)


def _et(d: date, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET))


@pytest.mark.parametrize(
    ("day", "expected_utc_hour"),
    [(date(2026, 3, 6), 9), (date(2026, 3, 9), 8)],  # EST Friday, EDT Monday around 2026-03-08
)
def test_et_minute_of_day_ms_follows_dst(day: date, expected_utc_hour: int) -> None:
    ms = et_minute_of_day_ms(day, 4 * 60)
    assert datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC")).hour == expected_utc_hour


def test_extended_bounds_enclose_the_regular_session() -> None:
    bounds = extended_session_bounds_ms(_REGULAR, window=_WINDOW)
    assert bounds.open_ms == _et(_REGULAR, 4, 0)
    assert bounds.rth_open_ms == session_open_ms_utc(_REGULAR)
    assert bounds.rth_close_ms == session_close_ms_utc(_REGULAR)
    assert bounds.close_ms == _et(_REGULAR, 20, 0)


def test_extended_bounds_refuse_a_non_trading_day() -> None:
    with pytest.raises(ValueError, match="not a trading day"):
        extended_session_bounds_ms(_SUNDAY, window=_WINDOW)


def test_extended_bounds_refuse_a_window_inside_the_regular_session() -> None:
    with pytest.raises(ValueError, match="enclose"):
        extended_session_bounds_ms(_REGULAR, window=ExtendedHoursWindow(open_minute_et=10 * 60, close_minute_et=15 * 60))


@pytest.mark.parametrize(
    ("hour", "minute", "phase", "next_hour", "next_minute"),
    [
        (3, 59, "CLOSED", 4, 0),
        (4, 0, "PRE", 9, 30),
        (9, 29, "PRE", 9, 30),
        (9, 30, "RTH", 16, 0),
        (15, 59, "RTH", 16, 0),
        (16, 0, "POST", 20, 0),
        (19, 59, "POST", 20, 0),
    ],
)
def test_declared_window_phases_on_a_regular_day(hour: int, minute: int, phase: str, next_hour: int, next_minute: int) -> None:
    state = session_state_at_ms(now_ms=_et(_REGULAR, hour, minute), extended_window=_WINDOW)

    assert state.phase == phase
    assert state.source == "broker_declared_window"
    assert state.extended_phase_proven is True
    assert state.next_transition_ms == _et(_REGULAR, next_hour, next_minute)


def test_after_the_declared_close_the_next_transition_is_the_next_sessions_open() -> None:
    state = session_state_at_ms(now_ms=_et(_REGULAR, 20, 0), extended_window=_WINDOW)
    assert state.phase == "CLOSED"
    assert state.next_transition_ms == _et(date(2026, 9, 3), 4, 0)


def test_a_non_trading_day_is_closed_until_the_next_declared_open() -> None:
    state = session_state_at_ms(now_ms=_et(_SUNDAY, 12, 0), extended_window=_WINDOW)
    assert state.phase == "CLOSED"
    # Labor Day 2026-09-07 → next session Tuesday 2026-09-08.
    assert state.next_transition_ms == _et(date(2026, 9, 8), 4, 0)


def test_an_early_close_starts_post_at_the_calendar_close() -> None:
    state = session_state_at_ms(now_ms=_et(_EARLY, 13, 0), extended_window=_WINDOW)
    assert state.phase == "POST"
    assert state.next_transition_ms == _et(_EARLY, 20, 0)


def test_without_a_window_the_calendar_still_proves_only_rth_or_closed() -> None:
    state = session_state_at_ms(now_ms=_et(_REGULAR, 18, 0))
    assert state.phase == "CLOSED"
    assert state.source == "nyse_calendar"
    assert state.extended_phase_proven is False
```

Also add, in the existing `market_data_admission_fact` test module (grep `market_data_admission_fact(` under `tests/services/`), one case: a connected, non-stale feed, `use_rth=False`, `extended_window=_WINDOW`, observed at 18:00 ET on a trading day → `scheduled_phase == "POST"`, `session_authority_source == "broker_declared_window"`, `extended_phase_proven is True`, `state == "AVAILABLE"`; and the same instant **without** a window → `state == "UNKNOWN"` (the existing "cannot be proven" branch). Follow that module's fake-feed pattern.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_session_authority_declared_window.py -q -p no:cacheprovider`
Expected: FAIL — import errors.

- [ ] **Step 3: Implement**

`app/services/session_authority.py`:

```python
from datetime import UTC, date, datetime, timedelta

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import is_trading_day, next_trading_day, session_window_for_date
from app.utils.timestamps import to_ms_utc

SessionAuthoritySource = Literal["ibkr_capability", "nyse_calendar", "broker_declared_window"]


def et_minute_of_day_ms(day: date, minute_of_day: int) -> int:
    """``minute_of_day`` past midnight on ``day`` in America/New_York, as int64 ms UTC.

    Wall-clock arithmetic on an aware datetime, so the UTC offset is the one
    in force at that wall time — a fixed offset would be an hour wrong across
    a DST boundary (temporal-rigor rule). The minutes are capability data;
    this function holds no session literal of its own.
    """
    wall = datetime(day.year, day.month, day.day, tzinfo=_NY) + timedelta(minutes=minute_of_day)
    return to_ms_utc(wall)


@dataclass(frozen=True)
class ExtendedSessionBounds:
    """One trading day's declared extended session around its regular session."""

    open_ms: int
    rth_open_ms: int
    rth_close_ms: int
    close_ms: int


def extended_session_bounds_ms(session_date: date, *, window: ExtendedHoursWindow) -> ExtendedSessionBounds:
    """The declared window applied to ``session_date``'s calendar session (ADR 0059 D5.2)."""
    if not is_trading_day(session_date):
        raise ValueError(f"{session_date.isoformat()} is not a trading day")
    regular = session_window_for_date(session_date)
    open_ms = et_minute_of_day_ms(session_date, window.open_minute_et)
    close_ms = et_minute_of_day_ms(session_date, window.close_minute_et)
    if not (open_ms <= regular.open_ms_utc and regular.close_ms_utc <= close_ms):
        raise ValueError("the declared extended window must enclose the regular session")
    return ExtendedSessionBounds(
        open_ms=open_ms,
        rth_open_ms=regular.open_ms_utc,
        rth_close_ms=regular.close_ms_utc,
        close_ms=close_ms,
    )
```

Add `extended_window: ExtendedHoursWindow | None = None` to `session_state_at_ms` and, between the capability branch and the calendar fallback:

```python
    if extended_window is not None:
        return _session_from_declared_window(
            now_ms=now_ms,
            window=extended_window,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )
```

with:

```python
def _session_from_declared_window(
    *,
    now_ms: int,
    window: ExtendedHoursWindow,
    strategy_session_policy: Literal["rth_only"] | None,
    allowed_sessions: tuple[SessionKind, ...] | None,
) -> SessionAuthorityState:
    """PRE/RTH/POST from the calendar's regular session and the broker's declared window.

    Proven by declaration (the executing broker publishes the window as a
    capability), which is what ``extended_phase_proven`` means for a broker
    with no probe-based session capability.
    """
    day = _ny_dt(now_ms).date()

    def _state_for(phase: TradingSessionPhase, next_transition_ms: int) -> SessionAuthorityState:
        return _state(
            phase=phase,
            now_ms=now_ms,
            next_transition_ms=next_transition_ms,
            timezone="America/New_York",
            source="broker_declared_window",
            extended_phase_proven=True,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )

    def _next_open(after: date) -> int:
        return extended_session_bounds_ms(next_trading_day(after), window=window).open_ms

    if not is_trading_day(day):
        return _state_for("CLOSED", _next_open(day))
    bounds = extended_session_bounds_ms(day, window=window)
    if now_ms < bounds.open_ms:
        return _state_for("CLOSED", bounds.open_ms)
    if now_ms < bounds.rth_open_ms:
        return _state_for("PRE", bounds.rth_open_ms)
    if now_ms < bounds.rth_close_ms:
        return _state_for("RTH", bounds.rth_close_ms)
    if now_ms < bounds.close_ms:
        return _state_for("POST", bounds.close_ms)
    return _state_for("CLOSED", _next_open(day))
```

Update the module's docstring of `session_state_at_ms` ("PRE, POST, and OVERNIGHT require a current capability snapshot…") to say: or the executing broker's declared window, which proves PRE/POST by declaration; OVERNIGHT still needs a probe.

`market_data_capability_service.extended_phase_proven_at_ms`: add `extended_window: ExtendedHoursWindow | None = None` and pass it to the `session_state_at_ms` call (the capability lookup stays first; the window is the next source). Keep the `account_id is None → False` guard **only** for the capability path: when a window is given, resolve through it even with `account_id=None` (a declared window is not account-scoped).

`bot_start_admission.market_data_admission_fact`: add `extended_window: ExtendedHoursWindow | None = None`, pass to both `session_state_at_ms` calls. `market_pulse.build_market_pulse`: add `extended_window: ExtendedHoursWindow | None = None`, pass to `market_data_admission_fact` and to `extended_phase_proven_at_ms`. `app/schemas/run_admission.py`: `session_authority_source: Literal["ibkr_capability", "nyse_calendar", "broker_declared_window"] | None = None` (the OpenAPI snapshot is regenerated in Task 10).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_session_authority_declared_window.py tests/services/test_session_authority.py tests/services/test_bot_start_admission.py tests/services/test_market_liveness.py tests/broker/v2panel -q -p no:cacheprovider` (substitute the actual admission/liveness/pulse test module names if they differ — `ls tests/services | grep -i 'admission\|liveness\|session'`).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/services/session_authority.py PythonDataService/app/services/market_data_capability_service.py PythonDataService/app/services/bot_start_admission.py PythonDataService/app/services/broker_v2_panel/market_pulse.py PythonDataService/app/schemas/run_admission.py PythonDataService/tests/services/test_session_authority_declared_window.py <the admission test module you edited> && git commit -m "feat(session): the broker's declared window proves PRE and POST around the calendar session (ADR 0059 D5.2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The decision clock fires across the extended session

**Files:**
- Modify: `PythonDataService/app/marketdata/feed.py` (`DecisionSession` ~164; `ContinuityPolicy.__post_init__` ~262–268)
- Modify: `PythonDataService/app/services/decision_clock.py`
- Modify: `PythonDataService/app/services/feed_continuity_policy.py`
- Modify: `PythonDataService/app/services/bot_trade_strategy.py` (`_RetainedSourceBarFeed`, `_includes_session_phase`, the session-close flush ~505, the two `continuity_policy_for` call sites ~714 and ~996, `strategy_evaluations`)
- Test: `tests/services/test_decision_clock.py`, `tests/services/test_feed_continuity_policy.py`, `tests/marketdata/test_feed.py`, `tests/services/test_bot_trade_strategy_extended_bars.py` (new)

**Interfaces:**
- Consumes: `extended_session_bounds_ms`, `session_state_at_ms` (Task 3); `ExtendedHoursWindow` (Task 1).
- Produces:
  - `DecisionSession = Literal["rth", "extended"]` (the reserved `"all"` is retired — Ruling R7).
  - `decision_clock.extended_trigger_instants(session_date, *, timeframe_ms, window) -> list[int]`
  - `decision_clock.next_trigger_ms(last_delivered_end_ms, *, timeframe_ms, decision_session, window=None) -> int`
  - `decision_clock.next_trigger_function(timeframe_ms, *, decision_session, window=None) -> Callable[[int], int]` (replaces `rth_next_trigger_function`)
  - `decision_clock.decision_session_close_ms(session_date, *, decision_session, window=None) -> int`
  - `feed_continuity_policy.continuity_policy_for(binding, ledger, *, extended_window=None)`
  - `bot_trade_strategy._includes_decision_bar(bar, *, use_rth, extended_window)`; `_RetainedSourceBarFeed(..., extended_window=None)`; `strategy_evaluations(..., extended_window=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_decision_clock.py` (import `extended_trigger_instants`, `next_trigger_function`, `decision_session_close_ms` and `ExtendedHoursWindow`; define `_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)`):

```python
def test_extended_trigger_instants_regular_day() -> None:
    triggers = extended_trigger_instants(_REGULAR, timeframe_ms=_TF, window=_WINDOW)

    assert triggers[0] == _et(_REGULAR, 4, 16)  # bucket 04:00–04:15 closes on the 04:16 source minute
    assert _et(_REGULAR, 16, 1) in triggers  # the 15:45–16:00 bucket is not force-flushed at the RTH close
    assert triggers[-1] == _et(_REGULAR, 20, 0)  # the last bucket is force-flushed at the declared close
    assert len(triggers) == 16 * 4


def test_extended_trigger_instants_early_close_is_unchanged() -> None:
    # The declared window does not depend on the regular session's close: the
    # early-close day has the same wall-clock triggers as a regular day.
    regular = extended_trigger_instants(_REGULAR, timeframe_ms=_TF, window=_WINDOW)
    early = extended_trigger_instants(_EARLY, timeframe_ms=_TF, window=_WINDOW)
    shift = _et(_EARLY, 4, 0) - _et(_REGULAR, 4, 0)

    assert early == [t + shift for t in regular]
    assert _et(_EARLY, 13, 1) in early  # the 12:45–13:00 bucket fires on the next source minute, not at the early close


def test_extended_next_trigger_rolls_across_the_weekend() -> None:
    after_close = _et(_FRIDAY, 20, 0)
    assert next_trigger_ms(after_close, timeframe_ms=_TF, decision_session="extended", window=_WINDOW) == _et(date(2026, 9, 8), 4, 16)


def test_extended_next_trigger_before_the_declared_open_is_the_first_bucket() -> None:
    assert next_trigger_ms(_et(_REGULAR, 3, 0), timeframe_ms=_TF, decision_session="extended", window=_WINDOW) == _et(_REGULAR, 4, 16)


def test_extended_requires_a_window() -> None:
    with pytest.raises(ValueError, match="extended window"):
        next_trigger_ms(_et(_REGULAR, 5, 0), timeframe_ms=_TF, decision_session="extended")


def test_next_trigger_function_binds_the_session() -> None:
    rth = next_trigger_function(_TF, decision_session="rth")
    extended = next_trigger_function(_TF, decision_session="extended", window=_WINDOW)
    at_1700 = _et(_REGULAR, 17, 0)

    assert rth(at_1700) == _et(date(2026, 9, 3), 9, 46)
    assert extended(at_1700) == _et(_REGULAR, 17, 1)  # the 16:45–17:00 bucket fires on the 17:01 source minute


def test_decision_session_close() -> None:
    assert decision_session_close_ms(_REGULAR, decision_session="rth") == session_close_ms_utc(_REGULAR)
    assert decision_session_close_ms(_EARLY, decision_session="rth") == session_close_ms_utc(_EARLY)
    assert decision_session_close_ms(_EARLY, decision_session="extended", window=_WINDOW) == _et(_EARLY, 20, 0)
```

Replace the existing `"all"` refusal test at `test_decision_clock.py:~127` with a check that `"extended"` without a window raises (above), and rename every `rth_next_trigger_function` use to `next_trigger_function(_TF, decision_session="rth")`.

In `tests/services/test_feed_continuity_policy.py`, replace `test_continuity_policy_for_all_session_binding_gets_no_policy` with two tests:

```python
def test_continuity_policy_for_extended_binding_without_a_window_gets_no_policy(tmp_path, caplog) -> None:
    binding = _sealed_rth_binding().model_copy(update={"use_rth": False})
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        with caplog.at_level(logging.INFO, logger=module.__name__):
            assert continuity_policy_for(binding, ledger) is None
    finally:
        ledger.close()
    declined = [r for r in caplog.records if getattr(r, "action", None) == "feed_continuity_not_offered"]
    assert [r.reason for r in declined] == ["extended_window_unknown"]


def test_continuity_policy_for_extended_binding_with_a_window_schedules_the_extended_clock(tmp_path) -> None:
    binding = _sealed_rth_binding().model_copy(update={"use_rth": False})
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        policy = continuity_policy_for(binding, ledger, extended_window=_WINDOW)
    finally:
        ledger.close()
    assert policy is not None and policy.decision_session == "extended"
    assert policy.is_trigger_ms(_et(date(2026, 9, 2), 17, 16)) is True  # (define _et/_WINDOW as in the clock tests; timeframe from the sealed binding)
```

In `tests/marketdata/test_feed.py`, replace `test_continuity_policy_refuses_a_session_it_has_no_trigger_set_for` with `test_continuity_policy_accepts_the_extended_session` (construct with `decision_session="extended"`; assert `policy.decision_session == "extended"`).

Create `tests/services/test_bot_trade_strategy_extended_bars.py`:

```python
"""An extended binding decides only on bars inside the declared window; an RTH binding is unchanged."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.marketdata.feed import MarketDataBar
from app.services.bot_trade_strategy import _includes_decision_bar
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_DAY = date(2026, 9, 2)


def _bar(hour: int, minute: int, *, phase: str = "CLOSED") -> MarketDataBar:
    start = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    one = Decimal("1")
    return MarketDataBar(
        symbol="SPY", start_ms=start, end_ms=start + 60_000, open=one, high=one, low=one, close=one,
        volume=1, fetched_at_ms=start + 60_000, feed_id="ibkr", session_phase=phase,
    )


@pytest.mark.parametrize(
    ("hour", "minute", "included"),
    [(3, 59, False), (4, 0, True), (9, 29, True), (9, 30, True), (15, 59, True), (16, 0, True), (19, 59, True), (20, 0, False)],
)
def test_extended_binding_decides_inside_the_declared_window_only(hour: int, minute: int, included: bool) -> None:
    assert _includes_decision_bar(_bar(hour, minute), use_rth=False, extended_window=_WINDOW) is included


def test_extended_binding_without_a_window_decides_on_nothing() -> None:
    assert _includes_decision_bar(_bar(12, 0), use_rth=False, extended_window=None) is False


def test_rth_binding_still_trusts_the_feed_label() -> None:
    # Unchanged behaviour: RTH filtering is by the feed's label, so labelled test bars keep working.
    assert _includes_decision_bar(_bar(3, 0, phase="RTH"), use_rth=True, extended_window=_WINDOW) is True
    assert _includes_decision_bar(_bar(12, 0, phase="CLOSED"), use_rth=True, extended_window=_WINDOW) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_decision_clock.py tests/services/test_feed_continuity_policy.py tests/marketdata/test_feed.py tests/services/test_bot_trade_strategy_extended_bars.py -q -p no:cacheprovider`
Expected: FAIL — imports.

- [ ] **Step 3: Implement**

`app/marketdata/feed.py`: `DecisionSession = Literal["rth", "extended"]` with docstring "Which minutes the consumer's decision clock treats as decidable: the calendar's regular session, or the executing broker's declared extended session around it (ADR 0059 D5.2)." Delete `ContinuityPolicy.__post_init__` (nothing is left to refuse; its comment about ruling R1 goes with it).

`app/services/decision_clock.py` — refactor the loop out of `rth_trigger_instants` and add the extended set:

```python
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.services.session_authority import extended_session_bounds_ms


def _require_source_multiple(timeframe_ms: int) -> None:
    if timeframe_ms <= 0 or timeframe_ms % SOURCE_BAR_MS != 0:
        raise ValueError(
            f"timeframe_ms must be a positive multiple of the {SOURCE_BAR_MS} ms source bar; got {timeframe_ms}"
        )


def _trigger_instants(*, open_ms: int, close_ms: int, timeframe_ms: int) -> list[int]:
    triggers: list[int] = []
    bucket_start = floor_to_period_ms_et(open_ms, timeframe_ms)
    while bucket_start < close_ms:
        bucket_end = bucket_start + timeframe_ms
        triggers.append(close_ms if bucket_end >= close_ms else bucket_end + SOURCE_BAR_MS)
        bucket_start = bucket_end
    return triggers


def rth_trigger_instants(session_date: date, *, timeframe_ms: int) -> list[int]:
    """(keep the existing docstring)"""
    _require_source_multiple(timeframe_ms)
    return _trigger_instants(
        open_ms=session_open_ms_utc(session_date),
        close_ms=session_close_ms_utc(session_date),
        timeframe_ms=timeframe_ms,
    )


def extended_trigger_instants(session_date: date, *, timeframe_ms: int, window: ExtendedHoursWindow) -> list[int]:
    """Every instant on ``session_date`` at which an extended-session decision is due.

    Same bucket rule as the regular session, applied to the broker's declared
    window: the run force-flushes the day's last bucket at the declared close,
    and the regular close is an ordinary bucket boundary inside the day.

    Formula:
        for each bucket ``[b, b + timeframe_ms)`` from ``floor_et(xh_open)`` while
        ``b < xh_close``: ``xh_close`` if ``b + timeframe_ms >= xh_close`` else
        ``b + timeframe_ms + 60_000``, where ``[xh_open, xh_close)`` =
        ``extended_session_bounds_ms(session_date, window)``.
    Reference:
        ADR 0059 Decision 5.2 (the clock triggers on the timeframe within the
        phases the binding includes); bucket rule as ``rth_trigger_instants``.
    Canonical implementation: this file.
    Validated against:
        tests/services/test_decision_clock.py::test_extended_trigger_instants_regular_day,
        ::test_extended_trigger_instants_early_close_is_unchanged
    """
    _require_source_multiple(timeframe_ms)
    bounds = extended_session_bounds_ms(session_date, window=window)
    return _trigger_instants(open_ms=bounds.open_ms, close_ms=bounds.close_ms, timeframe_ms=timeframe_ms)


def _schedule(
    decision_session: DecisionSession, *, timeframe_ms: int, window: ExtendedHoursWindow | None
) -> Callable[[date], list[int]]:
    if decision_session == "rth":
        return lambda day: rth_trigger_instants(day, timeframe_ms=timeframe_ms)
    if window is None:
        raise ValueError("decision_session='extended' requires the broker's extended window")
    return lambda day: extended_trigger_instants(day, timeframe_ms=timeframe_ms, window=window)


def next_trigger_ms(
    last_delivered_end_ms: int,
    *,
    timeframe_ms: int,
    decision_session: DecisionSession,
    window: ExtendedHoursWindow | None = None,
) -> int:
    """(keep the existing docstring; drop the sentence refusing "all"; note that
    ``window`` is required for ``"extended"``)"""
    triggers_for = _schedule(decision_session, timeframe_ms=timeframe_ms, window=window)
    session_date = ny_datetime(last_delivered_end_ms).date()
    if not is_trading_day(session_date):
        session_date = next_trading_day(session_date)
    while True:
        for trigger in triggers_for(session_date):
            if trigger > last_delivered_end_ms:
                return trigger
        session_date = next_trading_day(session_date)


def next_trigger_function(
    timeframe_ms: int, *, decision_session: DecisionSession, window: ExtendedHoursWindow | None = None
) -> Callable[[int], int]:
    """Bind the clock's parameters into the single-argument callable the continuity loop schedules against."""
    _schedule(decision_session, timeframe_ms=timeframe_ms, window=window)  # fail at construction, not mid-run
    return lambda last_end: next_trigger_ms(
        last_end, timeframe_ms=timeframe_ms, decision_session=decision_session, window=window
    )


def decision_session_close_ms(
    session_date: date, *, decision_session: DecisionSession, window: ExtendedHoursWindow | None = None
) -> int:
    """The instant at which the run force-flushes ``session_date``'s last decision bucket."""
    if decision_session == "rth":
        return session_close_ms_utc(session_date)
    if window is None:
        raise ValueError("decision_session='extended' requires the broker's extended window")
    return extended_session_bounds_ms(session_date, window=window).close_ms
```

Delete `rth_next_trigger_function`; update the module docstring (the sentence "Only the regular session is supported … refused here (controller ruling R1)" becomes: both sessions are supported; the extended window is broker capability data resolved through `session_authority`).

`app/services/feed_continuity_policy.py`: `continuity_policy_for(binding, ledger, *, extended_window: ExtendedHoursWindow | None = None)`:

```python
    if binding.sealed_program is None:
        return _not_offered(binding, reason="unsealed_binding")
    decision_session: DecisionSession = "rth" if binding.use_rth else "extended"
    if decision_session == "extended" and extended_window is None:
        return _not_offered(binding, reason="extended_window_unknown")
    timeframe_ms = decision_timeframe_ms_for_binding(binding)
    if timeframe_ms is None:
        return _not_offered(binding, reason="no_decision_timeframe")
    ...
    return ContinuityPolicy(
        decision_session=decision_session,
        next_trigger_ms=next_trigger_function(timeframe_ms, decision_session=decision_session, window=extended_window),
        ...
    )
```

Rewrite the module docstring's second bullet: an extended-hours binding gets a policy only when the executing broker declares an extended window; otherwise it streams without one (honest, not guessed).

`app/services/bot_trade_strategy.py`:

- `_RetainedSourceBarFeed.__init__(..., extended_window: ExtendedHoursWindow | None = None)` stored as `self._extended_window`; `stream_bars` and `recent_closed_bars` filter with `_includes_decision_bar(bar, use_rth=use_rth, extended_window=self._extended_window)`.
- Replace `_includes_session_phase` with:

```python
_EXTENDED_DECISION_PHASES: frozenset[BarSessionPhase] = frozenset({"PRE", "RTH", "POST"})


def _includes_decision_bar(
    bar: MarketDataBar | RetainedSourceBar, *, use_rth: bool, extended_window: ExtendedHoursWindow | None
) -> bool:
    """Apply the sealed session policy after the source observation is durable.

    Regular-hours bindings keep trusting the feed's label. An extended binding
    decides on the bars whose open lies inside the broker's declared window
    (ADR 0059 D5.2); with no window it decides on nothing — Start admission
    refuses such a run before it streams (Task 6).
    """
    if use_rth:
        return bar.session_phase == "RTH"
    if extended_window is None:
        return False
    return session_state_at_ms(now_ms=bar.start_ms, extended_window=extended_window).phase in _EXTENDED_DECISION_PHASES
```

- The session-close flush (~505): compute `decision_session = "rth" if binding.use_rth else "extended"` once and replace the condition with `market_bar.end_ms == decision_session_close_ms(ny_datetime(market_bar.end_ms).date(), decision_session=decision_session, window=extended_window)`; thread `extended_window` into `strategy_evaluations(...)` as a keyword-only parameter (default `None`) and update the comment (RTH streaming → "streaming stops at the decision session's close").
- Both `_RetainedSourceBarFeed(...)` constructions (~708 and ~992) pass `extended_window=clerk.extended_hours_window` and `continuity=continuity_policy_for(binding, source_bars, extended_window=clerk.extended_hours_window)`; both `strategy_evaluations(...)` calls pass `extended_window=clerk.extended_hours_window`. `clerk.extended_hours_window` is the protocol attribute Task 5 adds — until Task 5 lands, read it as `getattr(clerk, "extended_hours_window", None)` **only in this task's commit**, and Task 5 replaces the `getattr` with the attribute.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command plus `tests/services/test_feed_continuity_end_to_end.py tests/marketdata/test_feed_continuity.py tests/services/test_bot_trade_strategy*.py tests/services/test_run_replay_proof.py`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/marketdata/feed.py PythonDataService/app/services/decision_clock.py PythonDataService/app/services/feed_continuity_policy.py PythonDataService/app/services/bot_trade_strategy.py PythonDataService/tests/services/test_decision_clock.py PythonDataService/tests/services/test_feed_continuity_policy.py PythonDataService/tests/marketdata/test_feed.py PythonDataService/tests/services/test_bot_trade_strategy_extended_bars.py && git commit -m "feat(clock): the decision clock fires across the broker's declared extended session (ADR 0059 D5.2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Every program leg is shaped by the decision bar and the session

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/program_leg.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/facts.py` (`ExitReducingOrderCreatedFacts`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py` (`resolve_exit`, `_resolve_claimed`, `_create_reducing_order`, `_submit_reducing_order`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/exit.py` (`resolve_accepted_exit` ~326)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py` (facade `__init__` ~189; `_execute_effect` ~615–880)
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_protocol.py` (`ActiveAlpacaClerk`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py` (both `SqliteAlpacaClerkFacade(...)` sites ~316 and ~554; new accessor beside `get_active_clerk_runtime` ~644)
- Modify: `PythonDataService/app/services/bot_trade_strategy.py` (replace Task 4's `getattr(clerk, "extended_hours_window", None)` with the attribute; `_liveness_blocks_entry` ~318–335 passes `extended_window`)
- Test: `tests/broker/alpaca/clerk/test_program_leg.py` (new), `tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py` (new), `tests/broker/alpaca/clerk/sqlite/test_runtime_program_leg.py` (new)

**Interfaces:**
- Consumes: `marketable_limit_price`, `ExtendedHoursAllowances` (Task 2); `session_state_at_ms` (Task 3); `DecisionSession` (Task 4); `RetainedSourceBar` (`app.services.source_bar_ledger`); `EffectPurpose` (`app.broker.alpaca.clerk.models`).
- Produces:
  - `program_leg.ProgramLegPolicy(window, allowances)` with `regular_only()`; `program_leg.LegShape(order_type, time_in_force, limit_price, extended_hours, side)` with `apply(*, symbol, side, quantity) -> BrokerOrderLeg`; `program_leg.REGULAR_SESSION_SHAPE`; `program_leg.ProgramLegRefused(reason_code, explanation, next_step)`; `program_leg.shape_program_leg(*, side, purpose, decision_session, decision_bar, policy) -> LegShape`.
  - `SqliteAlpacaClerkFacade(..., program_leg_policy: ProgramLegPolicy | None = None)`; properties `program_leg_policy` and `extended_hours_window`; the same two attributes on `ActiveAlpacaClerk`.
  - `active_authority.active_program_leg_policy() -> ProgramLegPolicy` (regular-only when no authority is active).
  - `resolve_accepted_exit(..., reducing_shape: LegShape | None = None)`; `resolve_exit(..., reducing_shape=None)`.
  - `ExitReducingOrderCreatedFacts(symbol, side, quantity, order_type="market", time_in_force="day", limit_price=None, extended_hours=False)`.
  - Reason codes: `EXTENDED_HOURS_UNSUPPORTED`, `EXTENDED_HOURS_ALLOWANCE_UNSET`, `EXTENDED_ANCHOR_UNAVAILABLE`, `SESSION_CLOSED_AT_DECISION`.

- [ ] **Step 1: Write the failing tests**

`tests/broker/alpaca/clerk/test_program_leg.py`:

```python
"""Program legs: market DAY inside the regular session, a marketable DAY limit flagged for extended hours outside it."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import (
    REGULAR_SESSION_SHAPE,
    ProgramLegPolicy,
    ProgramLegRefused,
    shape_program_leg,
)
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20"))
_POLICY = ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES)


def _bar(hour: int, minute: int, *, close: str = "100.00") -> RetainedSourceBar:
    end = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    return RetainedSourceBar(
        seq=1, account_id="PA-TEST", provider="ibkr", symbol="SPY",
        bar_identity=f"ibkr:SPY:{end - 60_000}:{end}", bar_ref="bar-1",
        start_ms=end - 60_000, end_ms=end,
        open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close),
        volume=1, fetched_at_ms=end,
    )
    # If RetainedSourceBar requires more fields, supply the model's defaults — read the class in source_bar_ledger.py.


def test_rth_binding_is_always_a_market_day_leg() -> None:
    shape = shape_program_leg(
        side=OrderSide.BUY, purpose=EffectPurpose.ENTER, decision_session="rth", decision_bar=None, policy=_POLICY
    )
    assert shape is REGULAR_SESSION_SHAPE
    leg = shape.apply(symbol="SPY", side=OrderSide.BUY, quantity=3.0)
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET, TimeInForce.DAY, None, False
    )


def test_extended_binding_inside_the_regular_session_is_a_market_day_leg() -> None:
    shape = shape_program_leg(
        side=OrderSide.BUY, purpose=EffectPurpose.ENTER, decision_session="extended", decision_bar=_bar(10, 0), policy=_POLICY
    )
    assert shape is REGULAR_SESSION_SHAPE


@pytest.mark.parametrize(
    ("hour", "minute", "side", "purpose", "expected_limit"),
    [
        (5, 0, OrderSide.BUY, EffectPurpose.ENTER, 100.10),   # PRE, entry allowance 10 bps up
        (16, 0, OrderSide.BUY, EffectPurpose.ENTER, 100.10),  # the regular close itself is POST
        (18, 30, OrderSide.SELL, EffectPurpose.EXIT, 99.80),  # POST, exit allowance 20 bps down
    ],
)
def test_extended_binding_outside_the_regular_session_is_a_marketable_day_limit(
    hour: int, minute: int, side: OrderSide, purpose: EffectPurpose, expected_limit: float
) -> None:
    shape = shape_program_leg(side=side, purpose=purpose, decision_session="extended", decision_bar=_bar(hour, minute), policy=_POLICY)

    assert shape.order_type is OrderType.LIMIT
    assert shape.time_in_force is TimeInForce.DAY
    assert shape.extended_hours is True
    assert shape.limit_price == expected_limit
    assert shape.side is side
    leg = shape.apply(symbol="SPY", side=side, quantity=2.5)
    assert leg.extended_hours is True and leg.limit_price == expected_limit


@pytest.mark.parametrize(
    ("policy", "bar", "reason_code"),
    [
        (ProgramLegPolicy(window=None, allowances=_ALLOWANCES), _bar(18, 0), "EXTENDED_HOURS_UNSUPPORTED"),
        (_POLICY, None, "EXTENDED_ANCHOR_UNAVAILABLE"),
        (_POLICY, _bar(20, 0), "SESSION_CLOSED_AT_DECISION"),
        (_POLICY, _bar(3, 30), "SESSION_CLOSED_AT_DECISION"),
        (ProgramLegPolicy(window=_WINDOW, allowances=None), _bar(18, 0), "EXTENDED_HOURS_ALLOWANCE_UNSET"),
    ],
)
def test_refusals(policy: ProgramLegPolicy, bar: RetainedSourceBar | None, reason_code: str) -> None:
    with pytest.raises(ProgramLegRefused) as caught:
        shape_program_leg(side=OrderSide.BUY, purpose=EffectPurpose.ENTER, decision_session="extended", decision_bar=bar, policy=policy)
    assert caught.value.reason_code == reason_code
    assert caught.value.next_step


def test_allowance_unset_inside_the_regular_session_still_shapes_a_market_leg() -> None:
    policy = ProgramLegPolicy(window=_WINDOW, allowances=None)
    assert shape_program_leg(
        side=OrderSide.BUY, purpose=EffectPurpose.ENTER, decision_session="extended", decision_bar=_bar(11, 0), policy=policy
    ) is REGULAR_SESSION_SHAPE
```

`tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py` (reuse `_FakeTrade`, `_broker_order`, `_make_entry`, `repo`, `SID`, `RUN_ID`, `ACCOUNT_ID` from `test_exit.py` by importing them):

```python
"""The reducing leg carries the decision's shape durably, and a resumed submission rebuilds it identically."""

from __future__ import annotations

from app.broker.alpaca.clerk.program_leg import LegShape
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from tests.broker.alpaca.clerk.sqlite.test_exit import ACCOUNT_ID, RUN_ID, SID, _FakeTrade, _broker_order, _make_entry

_XH_SELL = LegShape(order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=99.80, extended_hours=True, side=OrderSide.SELL)


def test_reducing_facts_omit_the_shape_when_it_is_the_regular_default() -> None:
    regular = ExitReducingOrderCreatedFacts(symbol="SPY", side="SELL", quantity=10)
    assert regular.to_facts_json() == ExitReducingOrderCreatedFacts.from_facts_json(regular.to_facts_json()).to_facts_json()
    assert "extended_hours" not in regular.to_facts_json() and "limit_price" not in regular.to_facts_json()
    # A row written before slice 3 parses to the regular shape.
    legacy = ExitReducingOrderCreatedFacts.from_facts_json('{"quantity": 10, "side": "SELL", "symbol": "SPY"}')
    assert (legacy.order_type, legacy.time_in_force, legacy.limit_price, legacy.extended_hours) == ("market", "day", None, False)


async def test_reducing_order_is_submitted_with_the_decision_shape_and_resubmitted_identically(repo) -> None:
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="exit-1", lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref)
    assert accepted.effect_operation_id is not None

    first_trade = _FakeTrade(submit_result=_broker_order("placeholder", status="accepted"))
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=first_trade, reducing_shape=_XH_SELL)
    assert first.reducing_order_ref is not None
    (leg, _client_order_id), = first_trade.submit_calls
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.side) == (
        OrderType.LIMIT, TimeInForce.DAY, 99.80, True, OrderSide.SELL
    )

    # Simulate a lost acknowledgement: the next pass finds no exact evidence after the absence grace and resubmits from the durable facts, without the shape being passed again.
    # Use the same mechanism test_exit.py's resubmission tests use (grep "_absence_grace_elapsed" / the clock advance helper) to reach _submit_reducing_order a second time.
    ...
    assert second_trade.submit_calls[0][0] == leg


async def test_a_shape_for_the_other_side_falls_back_to_market(repo, caplog) -> None:
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="exit-1", lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref)
    wrong_side = LegShape(order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=100.10, extended_hours=True, side=OrderSide.BUY)
    trade = _FakeTrade(submit_result=_broker_order("placeholder", status="accepted"))

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade, reducing_shape=wrong_side)

    (leg, _), = trade.submit_calls
    assert leg.order_type is OrderType.MARKET and leg.extended_hours is False
    assert any(getattr(r, "action", None) == "reducing_leg_shape_side_mismatch" for r in caplog.records)
```

Finish the elided resubmission step by copying the pattern of the existing test that reaches a second `_submit_reducing_order` (grep `submit_calls` with two entries in `test_exit.py`); if no such test exists, drive `_submit_reducing_order` directly with the reducing `OrderResource` from `repo.order(first.reducing_order_ref)` and a `ClaimedBrokerIO` built the way `resolve_exit` builds it.

`tests/broker/alpaca/clerk/sqlite/test_runtime_program_leg.py` — through the facade (`SqliteAlpacaClerkFacade(repo=repo, read=port, trade=port, account_mode="paper", program_leg_policy=...)`, using the conftest `_FakeTradePort`/`_FakeReadPort`; follow how `tests/routers/test_alpaca_clerk_sqlite.py` registers a strategy run and calls `execute_for_instance`):

- `use_rth=False`, policy with window+allowances, `retained_source_bar` at 18:30 ET close `100.00`, purpose ENTER → the port's `submit` receives a LIMIT DAY `extended_hours=True` leg at `100.10`.
- same at 10:00 ET → MARKET DAY.
- `use_rth=False`, `retained_source_bar=None` → receipt `state == "rejected"`, explanation starts with `EXTENDED_ANCHOR_UNAVAILABLE:`; no broker contact.
- `use_rth=True` with the same bar at 18:30 → MARKET DAY (unchanged behaviour; the RTH filter never yields this bar in production, the runtime does not re-check).
- `use_rth=False` with `ProgramLegPolicy.regular_only()` → `EXTENDED_HOURS_UNSUPPORTED` rejection.
- The facade's `extended_hours_window` equals the policy's window; `ProgramLegPolicy.regular_only()` gives `None`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_program_leg.py tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py tests/broker/alpaca/clerk/sqlite/test_runtime_program_leg.py -q -p no:cacheprovider`
Expected: FAIL — imports.

- [ ] **Step 3: Implement**

`app/broker/alpaca/clerk/program_leg.py`:

```python
"""Shape one program leg from the decision bar, the session, and the operator's allowances (ADR 0059 D5.3).

Inside the regular session a program leg is a market DAY order, exactly as
before this slice. Outside it — in the broker's declared PRE or POST window —
the leg is a marketable DAY limit flagged for extended hours, anchored to the
decision bar's close. Anything else (closed, no anchor, no window, no
allowance) is a typed refusal the Clerk turns into a rejected receipt; a
program leg is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.marketdata.feed import DecisionSession
from app.services.session_authority import session_state_at_ms
from app.services.source_bar_ledger import RetainedSourceBar

_EXTENDED_PHASES: Final = frozenset({"PRE", "POST"})


@dataclass(frozen=True)
class ProgramLegPolicy:
    """What the active authority knows about shaping extended-session legs."""

    window: ExtendedHoursWindow | None
    allowances: ExtendedHoursAllowances | None

    @classmethod
    def regular_only(cls) -> ProgramLegPolicy:
        return cls(window=None, allowances=None)


@dataclass(frozen=True)
class LegShape:
    """The session-dependent part of a program leg; ``side`` names the side a limit was priced for."""

    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None
    extended_hours: bool
    side: OrderSide | None

    def apply(self, *, symbol: str, side: OrderSide, quantity: float) -> BrokerOrderLeg:
        return BrokerOrderLeg(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            time_in_force=self.time_in_force,
            extended_hours=self.extended_hours,
        )


REGULAR_SESSION_SHAPE: Final = LegShape(
    order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, limit_price=None, extended_hours=False, side=None
)


class ProgramLegRefused(Exception):
    """This decision cannot become a program leg; the Clerk records why."""

    def __init__(self, *, reason_code: str, explanation: str, next_step: str) -> None:
        super().__init__(f"{reason_code}: {explanation}")
        self.reason_code = reason_code
        self.explanation = explanation
        self.next_step = next_step


def shape_program_leg(
    *,
    side: OrderSide,
    purpose: EffectPurpose,
    decision_session: DecisionSession,
    decision_bar: RetainedSourceBar | None,
    policy: ProgramLegPolicy,
) -> LegShape:
    """The shape of the leg this decision submits (ADR 0059 D5.3)."""
    if decision_session == "rth":
        return REGULAR_SESSION_SHAPE
    if policy.window is None:
        raise ProgramLegRefused(
            reason_code="EXTENDED_HOURS_UNSUPPORTED",
            explanation="The active broker authority declares no extended session.",
            next_step="Deploy with regular hours, or activate an authority whose capabilities declare an extended window.",
        )
    if decision_bar is None:
        raise ProgramLegRefused(
            reason_code="EXTENDED_ANCHOR_UNAVAILABLE",
            explanation="No exact retained decision bar exists to anchor an extended-session leg.",
            next_step="Retain the decision bar (replay or ingest it), then let the program decide again.",
        )
    phase = session_state_at_ms(now_ms=decision_bar.end_ms, extended_window=policy.window).phase
    if phase == "RTH":
        return REGULAR_SESSION_SHAPE
    if phase not in _EXTENDED_PHASES:
        raise ProgramLegRefused(
            reason_code="SESSION_CLOSED_AT_DECISION",
            explanation=f"The decision instant is {phase}; no session accepts a program leg now.",
            next_step="No action: the program decides again on the next bar inside a session.",
        )
    if policy.allowances is None:
        raise ProgramLegRefused(
            reason_code="EXTENDED_HOURS_ALLOWANCE_UNSET",
            explanation="ALPACA_LIVE_XH_ENTRY_BPS and ALPACA_LIVE_XH_EXIT_BPS are not both set; no extended-session leg can be priced.",
            next_step="Set both allowances in the environment file and restart the data plane.",
        )
    allowance = policy.allowances.entry_bps if purpose is EffectPurpose.ENTER else policy.allowances.exit_bps
    price = marketable_limit_price(side=side, close=decision_bar.close, allowance_bps=allowance)
    return LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=float(price),
        extended_hours=True,
        side=side,
    )
```

`facts.py` — `ExitReducingOrderCreatedFacts` gains, after `quantity: float`:

```python
    # ADR 0059 D5.3: the decision's leg shape, durable so a resumed submission
    # rebuilds the identical leg. Omitted from the canonical JSON at the
    # regular-session defaults (hash-chained schema evolution).
    order_type: str = "market"
    time_in_force: str = "day"
    limit_price: float | None = None
    extended_hours: bool = False

    def to_facts_json(self) -> str:
        payload = asdict(self)
        for name, default in _REDUCING_SHAPE_DEFAULTS.items():
            if payload[name] == default:
                payload.pop(name)
        return canonicalize(payload)
```

with a module-level `_REDUCING_SHAPE_DEFAULTS = {"order_type": "market", "time_in_force": "day", "limit_price": None, "extended_hours": False}` (a plain dict literal placed above the class). Extend the class docstring with one sentence naming the four shape fields.

`exit_resolution.py`:
- `resolve_exit(repo, *, effect_operation_id, trade, reducing_shape: LegShape | None = None)` → `_resolve_claimed(..., reducing_shape=reducing_shape)` → `_create_reducing_order(..., shape=reducing_shape)`.
- `_create_reducing_order(..., shape: LegShape | None)`: after computing `side`, resolve the durable shape:

```python
    resolved = REGULAR_SESSION_SHAPE if shape is None else shape
    if resolved.side is not None and resolved.side.value != side:
        logger.warning(
            "Reducing leg shape was priced for the other side; submitting a regular-session market leg instead",
            extra={
                "action": "reducing_leg_shape_side_mismatch",
                "effect_operation_id": effect_operation_id,
                "shaped_side": resolved.side.value,
                "reducing_side": side,
            },
        )
        resolved = REGULAR_SESSION_SHAPE
    facts = ExitReducingOrderCreatedFacts(
        symbol=symbol,
        side=side.upper(),
        quantity=abs(quantity),
        order_type=resolved.order_type.value,
        time_in_force=resolved.time_in_force.value,
        limit_price=resolved.limit_price,
        extended_hours=resolved.extended_hours,
    )
```

(add `logger = logging.getLogger(__name__)` if the module has none).
- `_submit_reducing_order`: build the leg from the facts — `BrokerOrderLeg(symbol=facts.symbol, side=facts.side.lower(), quantity=facts.quantity, order_type=facts.order_type, time_in_force=facts.time_in_force, limit_price=facts.limit_price, extended_hours=facts.extended_hours)`.

`exit.py`: `resolve_accepted_exit(repo, *, accepted, trade, reducing_shape: LegShape | None = None)` forwards it to `resolve_exit`. Every other caller of `resolve_exit` / `resolve_accepted_exit` (the reconciliation sweep, recovery, manual paths — `grep -rn "resolve_exit(\|resolve_accepted_exit(" app/`) passes nothing and therefore keeps market DAY (Ruling R5).

`runtime.py`:
- `SqliteAlpacaClerkFacade.__init__(..., program_leg_policy: ProgramLegPolicy | None = None)` → `self._program_leg_policy = program_leg_policy or ProgramLegPolicy.regular_only()`; properties:

```python
    @property
    def program_leg_policy(self) -> ProgramLegPolicy:
        return self._program_leg_policy

    @property
    def extended_hours_window(self) -> ExtendedHoursWindow | None:
        return self._program_leg_policy.window
```

- In `_execute_effect`, replace the `operation_leg = BrokerOrderLeg(...)` construction with:

```python
            program_side = OrderSide.BUY if entry.position == "long" else OrderSide.SELL
            leg_side = program_side if purpose is EffectPurpose.ENTER else _REDUCING_SIDE[program_side]
            try:
                shape = shape_program_leg(
                    side=leg_side,
                    purpose=purpose,
                    decision_session="rth" if use_rth else "extended",
                    decision_bar=retained_source_bar,
                    policy=self._program_leg_policy,
                )
            except ProgramLegRefused as exc:
                return rejected(reason_code=exc.reason_code, explanation=exc.explanation, next_step=exc.next_step)
            operation_leg = shape.apply(
                symbol=entry.instrument.underlying,
                side=leg_side,
                quantity=float(quantity * entry.qty_ratio),
            )
```

with `_REDUCING_SIDE: Final = {OrderSide.BUY: OrderSide.SELL, OrderSide.SELL: OrderSide.BUY}` at module level. **Check before changing `side`:** today `operation_leg.side` is the *program* side for both purposes and the EXIT branch never submits `operation_leg` (it only feeds `accept_enter` on the ENTER branch). Confirm with `grep -n operation_leg` that the EXIT branch does not read it; if it does, keep `side=program_side` for that read and use `leg_side` only for the shape.
- `resolve_accepted_exit(self._repo, accepted=accepted_exit, trade=trade, reducing_shape=shape)` on the EXIT branch.
- The liveness recheck's `extended_phase_proven=lambda: extended_phase_proven_at_ms(..., extended_window=self._program_leg_policy.window)`.

`active_protocol.py`: add to `ActiveAlpacaClerk` the attributes `program_leg_policy: ProgramLegPolicy` and `extended_hours_window: ExtendedHoursWindow | None` (as `@property` stubs, matching how other read-only attributes are declared there).

`active_authority.py`: at both facade construction sites pass

```python
            program_leg_policy=ProgramLegPolicy(
                window=ports.read.capabilities().extended_hours_window,
                allowances=ExtendedHoursAllowances.from_environment(),
            ),
```

and add beside `get_active_clerk_runtime`:

```python
def active_program_leg_policy() -> ProgramLegPolicy:
    """The active authority's leg policy; regular-only while none is active."""
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.clerk is None:
        return ProgramLegPolicy.regular_only()
    return runtime.clerk.program_leg_policy
```

`bot_trade_strategy.py`: replace the Task-4 `getattr(clerk, "extended_hours_window", None)` reads with `clerk.extended_hours_window`; in `_liveness_blocks_entry`, add `extended_window: ExtendedHoursWindow | None` to the signature and pass it into `extended_phase_proven_at_ms(...)`; its caller passes `clerk.extended_hours_window`.

Also update the synthetic drill support (`app/services/alpaca_sqlite_synthetic_drill_support.py:~325`) only if it constructs a facade; it builds a bare leg and needs no change.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command plus `tests/broker/alpaca/clerk/sqlite tests/routers/test_alpaca_clerk_sqlite.py tests/routers/test_clerk_transactions.py tests/broker/v2panel tests/services/test_bot_trade_strategy*.py`.
Expected: PASS (the golden execution-authority receipts must be byte-stable: no regular-session facts JSON changes).

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/program_leg.py PythonDataService/app/broker/alpaca/clerk/sqlite/facts.py PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py PythonDataService/app/broker/alpaca/clerk/sqlite/exit.py PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py PythonDataService/app/broker/alpaca/clerk/active_protocol.py PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/app/services/bot_trade_strategy.py PythonDataService/tests/broker/alpaca/clerk/test_program_leg.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime_program_leg.py && git commit -m "feat(clerk): program legs are shaped by the decision bar and the session, on ENTER and on the reducing order (ADR 0059 D5.3)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Start and Resume admit extended-hours runs on the declared window

**Files:**
- Modify: `PythonDataService/app/schemas/run_admission.py` (`StartRunFacts` ~225, `ResumeRunFacts` ~278; new `ExtendedHoursAdmissionFact`)
- Modify: `PythonDataService/app/services/run_admission.py` (`evaluate_run_admission`, after the market-data gate ~307–318)
- Modify: `PythonDataService/app/services/bot_start_admission.py` (service `__init__` ~391; facts construction ~478–500; new pure `extended_hours_admission_fact`)
- Modify: `PythonDataService/app/services/bot_resume_admission.py` (constructor + facts construction ~295–318)
- Modify: `PythonDataService/app/services/bot_runner.py` (~327, ~342: pass the policy resolver)
- Modify: `PythonDataService/app/services/broker_v2_panel/panel_data_source.py` (~352: `extended_window=active_program_leg_policy().window`)
- Test: `tests/services/test_run_admission_extended_hours.py` (new); existing admission/panel tests

**Interfaces:**
- Consumes: `ProgramLegPolicy`, `active_program_leg_policy` (Task 5); `market_data_admission_fact(..., extended_window=)` (Task 3).
- Produces:
  - `ExtendedHoursAdmissionFact(state: Literal["NOT_REQUESTED", "READY", "UNSUPPORTED", "ALLOWANCE_UNSET"], observed_at_ms: int)`; `StartRunFacts.extended_hours` / `ResumeRunFacts.extended_hours` (default `NOT_REQUESTED` at `observed_at_ms=0` so existing test construction sites keep working; every production site sets it explicitly).
  - `bot_start_admission.extended_hours_admission_fact(*, use_rth: bool, policy: ProgramLegPolicy, observed_at_ms: int) -> ExtendedHoursAdmissionFact`.
  - Reason codes: `EXTENDED_HOURS_UNSUPPORTED`, `EXTENDED_HOURS_ALLOWANCE_UNSET`.

- [ ] **Step 1: Write the failing tests** — `tests/services/test_run_admission_extended_hours.py`

```python
"""Extended-hours runs are admitted only when the active authority can clock and price them."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.schemas.run_admission import ExtendedHoursAdmissionFact
from app.services.bot_start_admission import extended_hours_admission_fact

_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("10"))


@pytest.mark.parametrize(
    ("use_rth", "policy", "state"),
    [
        (True, ProgramLegPolicy.regular_only(), "NOT_REQUESTED"),
        (False, ProgramLegPolicy.regular_only(), "UNSUPPORTED"),
        (False, ProgramLegPolicy(window=_WINDOW, allowances=None), "ALLOWANCE_UNSET"),
        (False, ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES), "READY"),
    ],
)
def test_extended_hours_admission_fact(use_rth: bool, policy: ProgramLegPolicy, state: str) -> None:
    fact = extended_hours_admission_fact(use_rth=use_rth, policy=policy, observed_at_ms=1_000)
    assert fact == ExtendedHoursAdmissionFact(state=state, observed_at_ms=1_000)
```

And in the existing `evaluate_run_admission` test module (grep `evaluate_run_admission(` under `tests/services/`, reuse its "admissible facts" builder), add three cases: `extended_hours.state == "UNSUPPORTED"` → refused `EXTENDED_HOURS_UNSUPPORTED` in every mode; `"ALLOWANCE_UNSET"` with `mode="trade"` → refused `EXTENDED_HOURS_ALLOWANCE_UNSET`; `"ALLOWANCE_UNSET"` with `mode="dry_run"` → allowed (falls through to the existing gates).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_run_admission_extended_hours.py -q -p no:cacheprovider` → FAIL (imports).

- [ ] **Step 3: Implement**

`app/schemas/run_admission.py`:

```python
class ExtendedHoursAdmissionFact(BaseModel):
    """Whether the active authority can clock and price a ``use_rth=False`` run (ADR 0059 D5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["NOT_REQUESTED", "READY", "UNSUPPORTED", "ALLOWANCE_UNSET"]
    observed_at_ms: int = Field(ge=0)
```

and on both `StartRunFacts` and `ResumeRunFacts`, after `market_liveness`:

```python
    extended_hours: ExtendedHoursAdmissionFact = ExtendedHoursAdmissionFact(state="NOT_REQUESTED", observed_at_ms=0)
```

`app/services/run_admission.py`, directly after the market-data gate:

```python
    if bot.extended_hours.state == "UNSUPPORTED":
        return decide(
            allowed=False,
            reason_code="EXTENDED_HOURS_UNSUPPORTED",
            explanation="The active broker authority declares no extended session, so a run outside regular hours has no decision clock.",
            next_step="Deploy with regular hours, or activate an authority whose capabilities declare an extended window.",
        )
    if bot.extended_hours.state == "ALLOWANCE_UNSET" and bot.mode == "trade":
        return decide(
            allowed=False,
            reason_code="EXTENDED_HOURS_ALLOWANCE_UNSET",
            explanation="ALPACA_LIVE_XH_ENTRY_BPS and ALPACA_LIVE_XH_EXIT_BPS are not both set, so no extended-session program leg can be priced.",
            next_step="Set both allowances in the environment file and restart the data plane.",
        )
```

`app/services/bot_start_admission.py`:

```python
def extended_hours_admission_fact(
    *, use_rth: bool, policy: ProgramLegPolicy, observed_at_ms: int
) -> ExtendedHoursAdmissionFact:
    """Pure: what the active authority can do for a run outside regular hours."""
    if use_rth:
        state: Literal["NOT_REQUESTED", "READY", "UNSUPPORTED", "ALLOWANCE_UNSET"] = "NOT_REQUESTED"
    elif policy.window is None:
        state = "UNSUPPORTED"
    elif policy.allowances is None:
        state = "ALLOWANCE_UNSET"
    else:
        state = "READY"
    return ExtendedHoursAdmissionFact(state=state, observed_at_ms=observed_at_ms)
```

The Start service gains a constructor parameter `program_leg_policy: Callable[[], ProgramLegPolicy] = active_program_leg_policy` (stored as `self._program_leg_policy`); at the facts construction it computes `policy = self._program_leg_policy()` once and passes `extended_window=policy.window` to `market_data_admission_fact(...)` and `extended_hours=extended_hours_admission_fact(use_rth=binding.use_rth, policy=policy, observed_at_ms=observed_at_ms)` to `StartRunFacts(...)`. Mirror this in `bot_resume_admission.py`. `bot_runner.py` passes `program_leg_policy=active_program_leg_policy` at both construction sites (explicit, like `session_capability`). `panel_data_source.py` passes `extended_window=active_program_leg_policy().window` to `build_market_pulse`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_run_admission_extended_hours.py tests/services/test_run_admission*.py tests/services/test_bot_start_admission*.py tests/services/test_bot_resume_admission*.py tests/broker/v2panel tests/routers/test_broker_bots*.py -q -p no:cacheprovider` (adjust names to the modules that exist).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/schemas/run_admission.py PythonDataService/app/services/run_admission.py PythonDataService/app/services/bot_start_admission.py PythonDataService/app/services/bot_resume_admission.py PythonDataService/app/services/bot_runner.py PythonDataService/app/services/broker_v2_panel/panel_data_source.py PythonDataService/tests/services/test_run_admission_extended_hours.py <the admission test module you edited> && git commit -m "feat(admission): extended-hours runs are admitted on the declared window and refused without a clock or a price (ADR 0059 D5.2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Unfilled is handled by machinery that exists — pinned

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py` (`_resolve_claimed`, the "terminal but no execution slice" branch ~205–218)
- Test: `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py` (new)

**Interfaces:**
- Consumes: the ENTER/EXIT harness helpers from `test_enter.py` / `test_exit.py`; `EXIT_NOT_FLAT_REASON_CODE` and `raise_uncertainty` (existing).
- Produces: `exit_resolution._UNFILLED_TERMINAL_STATES = frozenset({"canceled", "expired", "rejected"})` and the proven-unfilled branch.

- [ ] **Step 1: Write the failing tests** — `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py`

Three tests, each driving the existing machinery with the exit harness (`_make_entry`, `accept_exit`, `resolve_exit`, `_FakeTrade`, `_broker_order` imported from `tests/broker/alpaca/clerk/sqlite/test_exit.py`; `submit_accepted_enter`/`accept_enter` and the enter harness from `test_enter.py`):

1. **ENTER cancelled by the vendor leaves no exposure.** Submit an accepted ENTER whose leg is an extended-hours limit (`order_type="limit", limit_price=100.10, extended_hours=True`); the trade port acknowledges `accepted`; then a poll/reconcile observation returns the same order with `status="expired"`, `filled_quantity=0` (fold it the way `test_enter.py`'s terminal tests do — `fold_order_evidence` or the reconcile pass). Assert `repo.position(SID, "SPY") == 0`, the effect state is terminal (record the actual state — `failed` is expected for a never-filled entry; if the machine reports another terminal state, assert that and note it in the report), and no uncertainty is raised for the strategy (`repo.active_uncertainties(...)` or the module's equivalent read is empty).
2. **EXIT reducing order cancelled unfilled is an uncertainty, immediately.** `_make_entry(status="filled", filled_quantity=10)`, accept an EXIT, resolve with a fake trade whose submit acknowledges; then resolve again with `lookup_results=[_broker_order(reducing_ref, side="sell", status="canceled", filled_quantity=0)]`. Assert the effect state is `failed`, the strategy has an `EXIT_NOT_FLAT` uncertainty naming the reducing order ref in `evidence_refs`, and — the regression — the effect is **not** `unknown` (before this task the zero-fill terminal branch parked it as "awaiting websocket or recovery evidence" forever).
3. **The next EXIT decision re-issues at the new anchor.** After (2), accept a second EXIT (`decision_id="exit-2"`) and resolve it with a fresh fake trade and `reducing_shape=LegShape(... limit_price=99.50 ...)`; assert a new reducing order was submitted carrying `limit_price=99.50` and the first reducing order was not re-submitted. If `accept_exit` is refused while the `EXIT_NOT_FLAT` uncertainty stands, assert that refusal instead and record in the report which existing policy makes it so — the ADR's "next decision re-issues the EXIT" then means "after the operator's resolution", and Task 9's note must say so.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py -q -p no:cacheprovider`
Expected: test 2 FAILS (effect stays `unknown`); tests 1 and 3 may already pass — keep them, they pin D5.4.

- [ ] **Step 3: Implement the proven-unfilled branch**

In `_resolve_claimed`, replace the zero-fill branch:

```python
        if not repo.fills_for_order(reducing.order_ref):
            if (reducing.broker_state or "").lower() not in _UNFILLED_TERMINAL_STATES:
                # A filled/replaced terminal snapshot may precede its execution
                # slice on the websocket; wait for the slice.
                if effect.state != "unknown":
                    fold_uncertain(...)  # unchanged
                return _snapshot(repo, effect_operation_id)
            # canceled / expired / rejected with no recorded execution is
            # proven unfilled (ADR 0059 D5.4): fall through to EXIT_NOT_FLAT.
```

with `_UNFILLED_TERMINAL_STATES = frozenset({"canceled", "expired", "rejected"})` beside `_is_terminal`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command plus `tests/broker/alpaca/clerk/sqlite/test_exit.py tests/broker/alpaca/clerk/sqlite/test_enter.py tests/broker/alpaca/clerk/sqlite/test_reconcile.py`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py && git commit -m "fix(clerk): a reducing order the vendor cancels unfilled is an EXIT_NOT_FLAT uncertainty, not an endless unknown (ADR 0059 D5.4)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: The `limit_touch` fill model, and honest limit legs in the `sim:` world

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/fill_models.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py` (`_filled_order` ~383–421; `submit` ~244–267)
- Test: `tests/broker/alpaca/clerk/test_fill_models.py` (new), `tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py` (new)

**Interfaces:**
- Produces: `fill_models.SyntheticFill(filled_at_ms: int, price: Decimal, bar_ref: str)`; `fill_models.limit_touch_fill(leg, *, decision_bar_end_ms: int, bars: Sequence[RetainedSourceBar], cancel_at_ms: int) -> SyntheticFill | None`.
- The synthetic broker returns honest `order_type` / `time_in_force` / `limit_price` / `extended_hours` and cancels a non-marketable limit immediately (Ruling R9).

- [ ] **Step 1: Write the failing tests** — `tests/broker/alpaca/clerk/test_fill_models.py`

```python
"""limit_touch: eligibility starts after the decision bar; a bar that reaches the limit fills at the limit."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.fill_models import SyntheticFill, limit_touch_fill
from app.broker.contract.models import BrokerOrderLeg
from app.services.source_bar_ledger import RetainedSourceBar

_T0 = 1_800_000_000_000


def _bar(seq: int, *, start_ms: int, low: str, high: str) -> RetainedSourceBar:
    return RetainedSourceBar(
        seq=seq, account_id="shadow:x", provider="ibkr", symbol="SPY",
        bar_identity=f"ibkr:SPY:{start_ms}:{start_ms + 60_000}", bar_ref=f"bar-{seq}",
        start_ms=start_ms, end_ms=start_ms + 60_000,
        open=Decimal(low), high=Decimal(high), low=Decimal(low), close=Decimal(high), volume=1, fetched_at_ms=start_ms + 60_000,
    )


def _buy(limit: float) -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=limit, extended_hours=True)


def test_the_decision_bar_itself_never_fills() -> None:
    decision = _bar(1, start_ms=_T0, low="99.00", high="101.00")  # would touch a 100.10 buy
    assert limit_touch_fill(_buy(100.10), decision_bar_end_ms=decision.end_ms, bars=[decision], cancel_at_ms=_T0 + 3_600_000) is None


def test_the_first_later_bar_that_touches_fills_at_the_limit() -> None:
    bars = [
        _bar(1, start_ms=_T0, low="99.00", high="101.00"),
        _bar(2, start_ms=_T0 + 60_000, low="100.50", high="100.70"),  # above the limit: no touch
        _bar(3, start_ms=_T0 + 120_000, low="100.05", high="100.30"),  # low ≤ 100.10: touch
    ]
    assert limit_touch_fill(_buy(100.10), decision_bar_end_ms=_T0 + 60_000, bars=bars, cancel_at_ms=_T0 + 3_600_000) == SyntheticFill(
        filled_at_ms=_T0 + 180_000, price=Decimal("100.10"), bar_ref="bar-3"
    )


def test_a_sell_touches_on_the_high() -> None:
    sell = BrokerOrderLeg(symbol="SPY", side="sell", quantity=1, order_type="limit", limit_price=99.80, extended_hours=True)
    bars = [_bar(2, start_ms=_T0 + 60_000, low="99.00", high="99.79"), _bar(3, start_ms=_T0 + 120_000, low="99.50", high="99.80")]
    fill = limit_touch_fill(sell, decision_bar_end_ms=_T0 + 60_000, bars=bars, cancel_at_ms=_T0 + 3_600_000)
    assert fill is not None and fill.bar_ref == "bar-3" and fill.price == Decimal("99.80")


def test_a_bar_closing_after_the_cancel_instant_cannot_fill() -> None:
    late = _bar(2, start_ms=_T0 + 60_000, low="90.00", high="110.00")
    assert limit_touch_fill(_buy(100.10), decision_bar_end_ms=_T0 + 60_000, bars=[late], cancel_at_ms=_T0 + 90_000) is None


def test_market_legs_are_refused() -> None:
    with pytest.raises(ValueError, match="limit"):
        limit_touch_fill(BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), decision_bar_end_ms=_T0, bars=[], cancel_at_ms=_T0)
```

`tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py` — build a `SyntheticBroker` with a `SourceBarLedger` (follow `tests/services/test_alpaca_sqlite_synthetic_drills.py`'s construction), retain one bar with `close=100.00`, bind it to a client order id, then:

- a `limit` buy at `100.10`, `extended_hours=True` → returned order has `status="filled"`, `filled_avg_price == 100.0`, `order_type == "limit"`, `limit_price == 100.10`, `time_in_force == "day"`, `extended_hours is True`;
- a `limit` buy at `99.50` (below the close: not marketable) → `status="canceled"`, `filled_quantity == 0`, `canceled_at_ms == bar.end_ms`, `events == []`;
- a market buy → unchanged (`order_type == "market"`, filled at close).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_fill_models.py tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py -q -p no:cacheprovider` → FAIL.

- [ ] **Step 3: Implement**

`app/broker/alpaca/clerk/fill_models.py`:

```python
"""Synthetic fill models for authorities that never submit (ADR 0059 D5.5).

Formula (limit_touch):
    Let B be the retained bars with ``start_ms >= decision_bar_end_ms`` and
    ``end_ms <= cancel_at_ms``, in ascending order. A buy fills on the first
    b in B with ``b.low <= limit``; a sell on the first with ``b.high >=
    limit``. The fill price is the limit; the fill instant is that bar's
    close. No such bar: no fill (the vendor would have cancelled).
Reference:
    ADR 0059 Decision 5.5 — eligibility starts with the first bar after the
    decision bar (its own range predates the order); ADR 0002's third
    invariant (fills are explicit about the model that produced them).
Canonical implementation: this file.
Validated against:
    tests/broker/alpaca/clerk/test_fill_models.py
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from app.services.source_bar_ledger import RetainedSourceBar


@dataclass(frozen=True)
class SyntheticFill:
    filled_at_ms: int
    price: Decimal
    bar_ref: str


def limit_touch_fill(
    leg: BrokerOrderLeg,
    *,
    decision_bar_end_ms: int,
    bars: Sequence[RetainedSourceBar],
    cancel_at_ms: int,
) -> SyntheticFill | None:
    if leg.order_type is not OrderType.LIMIT or leg.limit_price is None:
        raise ValueError("limit_touch prices limit legs only")
    limit = Decimal(str(leg.limit_price))
    for bar in sorted(bars, key=lambda candidate: candidate.start_ms):
        if bar.start_ms < decision_bar_end_ms:
            continue
        if bar.end_ms > cancel_at_ms:
            return None
        touched = bar.low <= limit if leg.side is OrderSide.BUY else bar.high >= limit
        if touched:
            return SyntheticFill(filled_at_ms=bar.end_ms, price=limit, bar_ref=bar.bar_ref)
    return None
```

`synthetic_broker.py` — `_filled_order` becomes honest about the leg and the immediate-fill world:

```python
    def _filled_order(self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar) -> BrokerOrder:
        at_ms = bar.end_ms
        marketable = leg.order_type is OrderType.MARKET or (
            leg.limit_price is not None
            and (bar.close <= Decimal(str(leg.limit_price)) if leg.side is OrderSide.BUY else bar.close >= Decimal(str(leg.limit_price)))
        )
        common = dict(
            broker=self.broker_id,
            order_id=f"sim-order:{client_order_id}",
            client_order_id=client_order_id,
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side,
            order_type=str(leg.order_type),
            time_in_force=str(leg.time_in_force),
            quantity=leg.quantity,
            limit_price=leg.limit_price,
            stop_price=None,
            extended_hours=leg.extended_hours,
            submitted_at_ms=at_ms,
            created_at_ms=at_ms,
            updated_at_ms=at_ms,
            expired_at_ms=None,
            observed_at_ms=now_ms_utc(),
        )
        if not marketable:
            # The sim world cannot rest an order: a non-marketable limit is
            # cancelled on the spot, with no execution (ruling R9).
            return BrokerOrder(**common, filled_quantity=0.0, filled_avg_price=None, status="canceled", filled_at_ms=None, canceled_at_ms=at_ms, events=[])
        return BrokerOrder(
            **common,
            filled_quantity=leg.quantity,
            filled_avg_price=float(bar.close),
            status="filled",
            filled_at_ms=at_ms,
            canceled_at_ms=None,
            events=[{"event_type": "fill", "occurred_at_ms": at_ms, "price": float(bar.close), "quantity": leg.quantity, "execution_id": f"sim-execution:{client_order_id}"}],
        )
```

(keep the module's existing style for the event dict; `Decimal`, `OrderSide`, `OrderType` imports as needed). Update the class docstring's "immediate-fill" sentence to state the non-marketable rule.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command plus `tests/services/test_alpaca_sqlite_synthetic_drills.py tests/broker/alpaca/clerk/test_account_keyed_authority.py tests/broker/alpaca/clerk/sqlite/test_atomic_seam_fault_injection.py`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/fill_models.py PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py PythonDataService/tests/broker/alpaca/clerk/test_fill_models.py PythonDataService/tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py && git commit -m "feat(clerk): the limit_touch fill model, and honest limit legs in the sim world (ADR 0059 D5.5)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Reference note, registries, vocabulary

**Files:**
- Create: `docs/references/alpaca-extended-hours.md`
- Modify: `docs/math-sources-of-truth.md` (add a section `### Broker session and order anchoring (ADR 0059 D5)` directly after `### Broker fees (pass-through)`)
- Modify: `docs/architecture/engine-authority-map.md` (one row after the "Alpaca regulatory fee prediction…" row)
- Modify: `CONTEXT.md` (§ "Live account, shadow, and risk envelope": one term)
- Test: `PythonDataService/tests/contracts` (documentation contract: links resolve from the repository root)

- [ ] **Step 1: Write `docs/references/alpaca-extended-hours.md`**

```markdown
# Alpaca extended hours — the window, the anchor, the clock, and unfilled orders

**Status:** canonical for ADR 0059 slice 3 (2026-09-08). Lineage: live.

## What was built

- `BrokerOrderLeg.extended_hours` (contract validator: LIMIT + DAY|GTC) and `BrokerOrder.extended_hours`; `to_alpaca_order_request` forwards the flag on every order body, `from_alpaca_order` ingests it.
- `BrokerCapabilities.extended_hours_window` — the declared session as ET minutes past midnight; `ALPACA_EXTENDED_HOURS_WINDOW` = 04:00–20:00 ET.
- `app/services/session_authority.py` resolves PRE / RTH / POST from the calendar's regular session and the declared window (source `broker_declared_window`, `extended_phase_proven=True`).
- `app/services/decision_clock.py::extended_trigger_instants` and `decision_session="extended"`; `continuity_policy_for` offers it to `use_rth=False` bindings.
- `app/broker/alpaca/marketable_limit.py::marketable_limit_price` and `app/broker/alpaca/clerk/program_leg.py::shape_program_leg`; the reducing order's shape is durable in `EXIT_REDUCING_ORDER_CREATED` facts.
- `app/broker/alpaca/clerk/fill_models.py::limit_touch_fill` (for the slice-4 shadow port); the `sim:` world reports limit legs honestly and cancels a non-marketable limit immediately.

## Vendor facts (pinned 2026-09-08)

| Fact | Value | Source |
|---|---|---|
| Pre-market / after-hours | 04:00–09:30 ET / 16:00–20:00 ET, Mon–Fri | Alpaca "Orders at Alpaca" § Extended Hours Trading, `https://docs.alpaca.markets/docs/orders-at-alpaca` |
| Overnight | 20:00–04:00 ET, Sun–Fri (Blue Ocean ATS) — **not a decision phase in slice 3** | same |
| Order shape | `type=limit`, `time_in_force ∈ {day, gtc}`, `extended_hours=true`; anything else is rejected | same |
| Day order lifetime | eligible only on its day; unfilled after the close is cancelled; extended eligibility lets it execute in supported extended hours | same, Time in Force |
| Market order after 16:00 | queued for release the next trading day | same |
| Early-close days | not documented by Alpaca — the declared window applies unchanged; a venue cancel folds through D5.4 | plan ruling R2 |
| `/v2/calendar` `session_open` / `session_close` | legacy `0700` / `1900`, never explained (forum thread 2400, 2020–2023) — rejected as a source | `https://forum.alpaca.markets/t/calendar-what-are-session-open-and-session-close/2400` |

## The anchor

`buy: ceil_tick(close × (1 + entry_bps/10⁴))`, `sell: floor_tick(close × (1 − exit_bps/10⁴))`; tick 0.01 at or above $1, 0.0001 below. Rounding is in the marketable direction. The allowances are `ALPACA_LIVE_XH_ENTRY_BPS` / `ALPACA_LIVE_XH_EXIT_BPS`, required for a `use_rth=False` trade-mode run; unset → Start refuses `EXTENDED_HOURS_ALLOWANCE_UNSET`.

## The clock

`extended` buckets run from `floor_et(04:00)` to 20:00 ET; the regular close is an ordinary bucket boundary; the day's last bucket is force-flushed at 20:00 (the runner's flush instant is `decision_session_close_ms`). A decision at exactly 16:00 is POST (sessions are half-open); a decision at 20:00 is CLOSED and refused (`SESSION_CLOSED_AT_DECISION`).

## Unfilled orders (D5.4)

- ENTER cancelled by the vendor at the end of extended hours: folds terminal with zero fills; no exposure.
- EXIT reducing order cancelled unfilled: `EXIT_NOT_FLAT` uncertainty naming the reducing order, raised on the first pass that observes the cancel (`canceled` / `expired` / `rejected` with no recorded execution is proven unfilled). The next EXIT decision re-issues at the new bar's anchor — see `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py` for what the machine does today.
- Recovery-created reducing orders carry no decision context and stay market DAY; outside regular hours the vendor queues them for the next open.

## Validation

`tests/broker/alpaca/test_marketable_limit.py`, `tests/services/test_session_authority_declared_window.py`, `tests/services/test_decision_clock.py`, `tests/broker/alpaca/clerk/test_program_leg.py`, `tests/broker/alpaca/clerk/sqlite/test_exit_reducing_shape.py`, `tests/broker/alpaca/clerk/sqlite/test_unfilled_extended_orders.py`, `tests/broker/alpaca/clerk/test_fill_models.py`.

## Follow-ups

- Paper exercise: deploy one sealed instance with `use_rth=False` on the paper account with both allowances set; record the first extended-session submission, its Alpaca acknowledgement (`extended_hours: true`), and the 20:00 cancel of an unfilled order. Until then the vendor's early-close after-hours end is unverified.
- Overnight (20:00–04:00): a separate venue, separate data; needs an overnight bar source before it can be a decision phase.
- Bar labels: the IBKR data feed labels extended bars `CLOSED` unless a session capability probe is fresh; the decision session is computed from the instant, so labels are informational. Re-labelling from the declared window is a feed concern, tracked separately.
```

- [ ] **Step 2: Registry rows**

`docs/math-sources-of-truth.md` — new section after `### Broker fees (pass-through)`:

```markdown
### Broker session and order anchoring (ADR 0059 D5)

| Concept | Canonical implementation | Reference | Validated against | Status |
|---|---|---|---|---|
| Marketable-limit anchor (extended-session program legs) | `PythonDataService/app/broker/alpaca/marketable_limit.py::marketable_limit_price` | ADR 0059 D5.3; [alpaca-extended-hours](docs/references/alpaca-extended-hours.md) | `tests/broker/alpaca/test_marketable_limit.py` | Canonical |
| Extended-session decision triggers | `PythonDataService/app/services/decision_clock.py::extended_trigger_instants` | ADR 0059 D5.2; bucket rule as `rth_trigger_instants` | `tests/services/test_decision_clock.py` | Canonical |
| `limit_touch` synthetic fill | `PythonDataService/app/broker/alpaca/clerk/fill_models.py::limit_touch_fill` | ADR 0059 D5.5 | `tests/broker/alpaca/clerk/test_fill_models.py` | Canonical (consumer: slice-4 shadow port) |
```

(match the column layout of the neighbouring sections exactly — read them first; links are repository-root relative.)

`docs/architecture/engine-authority-map.md` — one row after the fee row, same columns: "Extended-hours program legs and the extended decision clock" | canonical session authority + declared broker window | `session_authority.py`, `decision_clock.py`, `program_leg.py`, `marketable_limit.py`, `fill_models.py` | what it decides (session at the decision instant, the leg's shape and price, the trigger set) | "Canonical (ADR 0059 slice 3). Overnight not a decision phase; bar labels informational."

`CONTEXT.md` — add after **Marketable limit anchor**:

```markdown
- **Decision session** — the phases a sealed instance decides in: the
  regular session, or the broker's declared extended session around it
  (pre-market, regular, after-hours). The window is broker capability data;
  the regular session is the calendar's. _Avoid_: all-hours, 24/5, use_rth
```

- [ ] **Step 3: Run the documentation contract**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q -p no:cacheprovider`
Expected: PASS (links resolve from the repository root).

- [ ] **Step 4: Commit**

```bash
cd /Users/inkant/learn-ai && git add docs/references/alpaca-extended-hours.md docs/math-sources-of-truth.md docs/architecture/engine-authority-map.md CONTEXT.md && git commit -m "docs(references): Alpaca extended hours — the declared window, the anchor, the clock, and unfilled orders (ADR 0059 D5)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Contracts and final gates on the branch tree

**Files:**
- Regenerate: `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts`

- [ ] **Step 1: Regenerate contracts**

Run: `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/python scripts/export_openapi_contract.py && cd /Users/inkant/learn-ai/Frontend && npm run codegen:openapi`
Expected: the snapshot gains `extended_hours` on `BrokerOrder`, the widened `session_authority_source`, and `ExtendedHoursAdmissionFact`; nothing else changes (inspect `git diff --stat contracts/ Frontend/src/app/api/`).

- [ ] **Step 2: Project-scope lint**

Run: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff check /Users/inkant/learn-ai/PythonDataService/app/ /Users/inkant/learn-ai/PythonDataService/tests/`
Expected: no findings.

- [ ] **Step 3: Targeted tests for every touched surface and its consumers**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker tests/services/test_session_authority_declared_window.py tests/services/test_decision_clock.py tests/services/test_feed_continuity_policy.py tests/services/test_feed_continuity_end_to_end.py tests/marketdata tests/services/test_bot_trade_strategy_extended_bars.py tests/services/test_run_admission_extended_hours.py tests/services/test_bot_start_admission.py tests/services/test_bot_resume_admission.py tests/services/test_market_liveness.py tests/routers tests/contracts -q -p no:cacheprovider`
(substitute the actual module names for any that differ; `tests/broker` is the whole broker tree on purpose — the leg contract and the facts changed.)
Expected: green. Anything red is yours to fix unless it is also red on `origin/master` at the same path — establish that baseline before calling it pre-existing, and name it in the report.

- [ ] **Step 4: Contract checks**

Run: `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check && cd /Users/inkant/learn-ai/Frontend && npm run codegen:check`
Expected: both exit 0.

- [ ] **Step 5: Commit the contracts and report**

```bash
cd /Users/inkant/learn-ai && git add contracts/openapi/python-data-service.openapi.json Frontend/src/app/api/broker.types.ts && git commit -m "chore(contracts): regenerate the OpenAPI snapshot and Frontend types for the extended-hours fields

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

State the exact commands, their exit codes, test counts, and any baseline-confirmed pre-existing failures.

---

## Rulings recorded while planning (carry into the SDD ledger)

- **R1 — `extended` = PRE ∪ RTH ∪ POST inside the declared window; OVERNIGHT is a capability fact, not a decision phase.** The data feed delivers no overnight bars and the paper exercise cannot cover it. Cost if wrong: overnight decisions wait for a later slice.
- **R2 — The window is the broker's declared capability (04:00–20:00 ET from Alpaca's documentation), not the calendar API's `session_open` / `session_close`** (unexplained legacy `0700` / `1900`). Early-close days apply the window unchanged; Alpaca documents no deviation; an order the venue cancels earlier folds through D5.4. Cost if wrong: on early-close days, POST decisions after the venue's real after-hours close produce vendor-cancelled orders and uncertainty receipts.
- **R3 — Bar labels are unchanged; the decision session is computed from the instant.** The IBKR feed labels extended bars `CLOSED` without a probe; relabelling is a feed concern. The RTH-only filter stays label-based (tests build labelled bars with synthetic timestamps). Cost if wrong: retained-bar labels read `CLOSED` for bars an extended run decided on until the feed relabels.
- **R4 — Phase at the decision instant, half-open sessions.** A decision at exactly the regular close is POST for an extended binding; at the declared close it is CLOSED and refused (`SESSION_CLOSED_AT_DECISION`). Cost if wrong *(sharpened by the fix wave)*: the day's last bucket is **always** refused, not occasionally — the force-flush fires at the declared close and that instant is CLOSED. For an ENTER that is harmless; for an EXIT the position carries overnight with a rejected receipt, and the program's next chance to reduce is the following session's first extended bucket at 04:16.
- **R5 — Recovery-created reducing orders stay market DAY** (no decision context; the vendor queues them for the next open). Decision-shaped reducing legs are durable in `EXIT_REDUCING_ORDER_CREATED` facts with omit-when-default serialisation. Cost if wrong *(sharpened by the fix wave — "as it does today" understated it)*: **no** emergency or operator reduce executes during extended hours. The exit watchdog, safe-flatten, reconciliation's flattening and the panel's "flatten and stop" all submit a market DAY order Alpaca queues to the next 09:30. Before this slice a bot could not be positioned outside regular hours; after it, one can be long from 05:00 with no path — automatic or operator — that actually reduces before the next open, while the panel's success copy reads as though the order is going out now. Documented plainly in the reference note; shaping the operator flatten from the current instant is a follow-up.
- **R6 — Manual tickets never carry `extended_hours`;** the instruction hash omits the flag when `False` so existing tickets keep validating. Cost if wrong: none for existing tickets; manual extended-hours orders remain out of scope (ADR: manual live orders are not done by this ADR).
- **R7 — `DecisionSession` is `rth | extended`; the reserved `all` is retired.** It was never schedulable; `extended` is its calendar-proven successor. Cost if wrong: a rename.
- **R8 — Allowances are read on paper too.** *(Amended by the final-review fix wave, thermo MAJOR 3.)* Unset → a `use_rth=False` Start or Resume is refused (`EXTENDED_HOURS_ALLOWANCE_UNSET`) in **every** mode, log-only and dry-run included; there is no mode carve-out. The original ruling let dry-run and log-only proceed, on the reasoning that neither prices a real leg — false for Dry Run, which runs on the synthetic authority built with the same `ProgramLegPolicy` and routes every intent through `shape_program_leg`, so it was admitted and then rejected every extended decision it made. The gate and the Clerk now share one source for both refusals (`program_leg.py`'s named `LegRefusal` values). No default in code (D4). Cost if wrong: the operator sets two values in `.env` for a log-only extended run too.
- **R9 — The `sim:` world cannot rest orders:** a non-marketable limit is cancelled immediately with zero fills; `limit_touch` ships as the pure model the slice-4 shadow port wires. Cost if wrong: sim runs with a zero allowance see immediate cancels on a bar whose close moved against them — visible, honest.
- **R10 — `extended_hours` is forwarded on every Alpaca order body** (explicit `false` for regular legs) and ingested from every order payload. Cost if wrong: two adapter tests to edit.
- **R11 — A reducing shape priced for the other side falls back to market DAY with a structured warning.** Long-only programs never hit it. Cost if wrong: one market reduce where a limit was intended.
- **R12 — `canceled` / `expired` / `rejected` with no recorded execution is proven unfilled** and resolves to `EXIT_NOT_FLAT` immediately, instead of the endless "awaiting websocket or recovery evidence" hold. Cost if wrong: an uncertainty raised one sweep earlier than a late execution slice would have arrived — the slice still folds when it does.

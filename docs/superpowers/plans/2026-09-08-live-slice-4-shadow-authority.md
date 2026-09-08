# Live Slice 4 — Shadow Account Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live boot composes the Shadow Account Authority — the real live read port bound to `NoSubmitAlpacaTradePort`, custody in an isolated `shadow:<live_account_id>` SQLite authority behind its own append-only activation fence — so a sealed instance can run whole shadow sessions on the live account without ever submitting; each session's sweep verdicts are journaled, its synthesized trades reconcile against the instance's paper twin, and the completed gate is sealed into a shadow receipt that slice 6's arming ceremony will require (ADR 0059 Decision 2, Decision 5.5, Consequences slice 4).

**Architecture:** The sim world's durable order ledger is extracted into `synthesized_orders.py` and shared by two thin worlds: `SyntheticBroker` (immediate fills, unchanged behaviour) and the new `shadow_broker.py` (`ShadowOrderBook` + `NoSubmitAlpacaTradePort` + `ShadowAccountReadPort`). The shadow read port is a composite: account, clock, activities, assets, portfolio history and capabilities come from the real live `AlpacaBroker`; positions and orders are projected from the synthesized ledger, so the existing reconciliation sweep reconciles the world the shadow Clerk custodies. Regular-session legs fill at the decision bar's close; extended-session limit legs rest and settle on read under `limit_touch` against the instance's own retained-bar evidence, cancelling at the declared window's close. `select_active_clerk_runtime` composes the shadow authority (kind `shadow`) where it refused `LIVE_ACCOUNT_REFUSED` before; the sqlite composition tail is shared. A sweep listener journals per-ET-day cleanliness; a pure twin reconciliation compares fills by sequence and shape; a sealed receipt store records the passed gate; the live verdict reports `shadow_state`. The panel deploy path gains an honest `shadow` execution mode.

**Tech Stack:** Python 3.12, Pydantic v2, `decimal`, `zoneinfo`, SQLite (`mode=ro` readers), JSONL WALs, pytest (`asyncio_mode=auto`), the SQLite Clerk test harness under `tests/broker/alpaca/clerk/`; Angular 22 + Vitest for the one Frontend task.

**Spec:** `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` — Decision 2 (the shadow gate, four paragraphs), Decision 5.5 (`limit_touch`), Decision 10 (dev reset refuses shadow), Consequences slice 4 ("The Shadow Account Authority, `NoSubmitAlpacaTradePort`, the shadow activation fence, the shadow receipt."). ADR 0002's five invariants transfer (cold-start zero orders in the namespace; typed execution rows; explicit fill provenance; bounded blast radius; graduation mints a new run ledger). Vocabulary: `CONTEXT.md` § "Live account, shadow, and risk envelope" (**Shadow Account Authority**, **Shadow gate**, **Shadow receipt**; this slice adds **Paper twin**).

## Global Constraints

- **ADR 0059 D2:** the shadow authority reads one real-money account and never submits — `NoSubmitAlpacaTradePort` never constructs a `TradingClient` write call; `submit` synthesizes a `BrokerOrder` under an explicit fill model; `cancel` never contacts Alpaca; cold start requires the live account to report **zero** orders in the Clerk's `learn-ai/<sid>/v1` namespace; the shadow authority can poison only its own directory; a shadow binding is sealed to `shadow:<id>` so graduation is a new run.
- **ADR 0059 D5.5:** outside the regular session the fill model is `limit_touch` — eligibility starts with the first bar **after** the decision bar; a bar that reaches the limit fills at the limit; the vendor cancels a DAY extended order at the declared window's close.
- **ADR 0059 D10:** `dev_reset` against a `shadow` authority is a hard refusal; fault injection stays paper-gated.
- **ADR 0042 isolation:** a real Alpaca port binds to neither `sim:` nor `shadow:` custody; the shadow custody journal never receives a real fill; the live account's execution stream is **not** shadow custody evidence.
- **Sealed artifacts — do not edit:** `app/lean_sidecar/trading_calendar.py`, `app/utils/timestamps.py`, `app/engine/consolidators/trade_bar_consolidator.py` (every program's `artifact_paths` in `app/engine/strategy/registry.py`). Import them; never change them.
- **Temporal rigor:** every wire/storage temporal value is `int64 ms UTC`; a trading date is stored as its calendar session open (`session_open_ms_utc(date)`), never an ISO string; ET dates are resolved through `app/utils/session_anchors.py` (`et_date_at_ms`, `et_midnight_ms`, `et_day_end_ms`); the declared window's bounds come only from `app/services/session_authority.declared_session_bounds`; no `time(...)`/minute literals in session logic.
- **Hash-chained facts:** no durable facts dataclass in `app/broker/alpaca/clerk/sqlite/facts.py` changes in this slice. The synthesized order JSONL gains optional fields that default to `None` so existing `sim:` rows re-parse.
- **Math Provenance Contract** (`.claude/skills/learn-ai-validation`): `project_positions` keeps its block (moved with the code); `reconcile_twin_day` carries `Formula` / `Reference` / `Canonical implementation` / `Validated against`; `docs/math-sources-of-truth.md` row 141 is re-pointed and one row is added; `docs/architecture/engine-authority-map.md` gets one row for the slice.
- **Repo rules:** `from __future__ import annotations`; type hints on every signature; Pydantic v2 only; no `print` (the CLI writes through `sys.stdout.write` / structured logging like `scripts/manage_alpaca_sqlite_clerk.py`); no silent `except`; structured logging with `extra={"action": ...}`; `ruff check app/ tests/` clean at project scope; tests via `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <paths> -q -p no:cacheprovider`; ruff via `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff`. Use absolute paths in every command.
- **Contracts:** wire schema changes (`AlpacaLiveVerdict.clerk_authority`, `AlpacaPaperDeployRequest.execution_mode`, `AlpacaPaperExecutionMode.mode`, `AlpacaPaperDeployStrategy.admissible_modes`, `AlpacaPaperDeployView.account_mode`) require `.venv/bin/python scripts/export_openapi_contract.py` (from `PythonDataService/`) and `npm run codegen:openapi` (from `Frontend/`), committed with the code (Task 12). Docs edits are guarded by `tests/contracts` (links resolve from the repository root: `docs/references/...`).
- **Commits:** stage explicit paths (never `git add -A`). Every commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Podman is not required.** The host venv is the runner. Never `podman exec`.

## Facts pinned from the code (2026-09-08, master 248342b8)

| Fact | Where |
|---|---|
| `select_active_clerk_runtime` refuses `account.account_mode != "paper"` with `LIVE_ACCOUNT_REFUSED` before binding ports | `app/broker/alpaca/clerk/active_authority.py:232` |
| `SqliteAlpacaClerkFacade.authority_kind: Literal["sqlite", "synthetic"]`; a synthetic authority must be `paper`; the decision bar is required (`SIMULATED_SOURCE_BAR_UNPROVEN`) and bound through `_DecisionBarBoundTradePort` only when `authority_kind == "synthetic"` | `app/broker/alpaca/clerk/sqlite/runtime.py:192-222, 675, 889` |
| `ActiveAlpacaClerk.authority_kind: Literal["sqlite", "synthetic"]` | `app/broker/alpaca/clerk/active_protocol.py:46` |
| `run_trade_bot` requires `authority_kind == "sqlite"` and already passes `retained_source_bar` | `app/services/bot_trade_strategy.py:773-800` |
| `get_alpaca_clerk()` returns the primary runtime only when `authority_kind == "sqlite"` | `active_authority.py:702` |
| `BindingAuthoritySelector.for_binding` routes `dry_run` to `SyntheticBindingAuthority`, everything else to `RealPaperBindingAuthority` whose `source_bars()` opens `paper:<sid>` | `app/services/bot_binding_authority.py` |
| `run_replay_proof.ledger_account_id_for(binding)` duplicates that namespace choice | `app/services/run_replay_proof.py:841` |
| `SyntheticBroker` owns the JSONL order WAL (`simulated_orders.jsonl`), the cross-process transaction lock, bar binding/verification and `_project_positions` (provenance block; registry row 141; `docs/references/synthetic-broker-position-projection.md`) | `app/broker/alpaca/clerk/synthetic_broker.py` |
| `fill_models.immediate_fill_price(leg, close)` and `limit_touch_fill(leg, *, decision_bar_end_ms, bars, cancel_at_ms) -> SyntheticFill(filled_at_ms, price, bar_ref) \| None` | `app/broker/alpaca/clerk/fill_models.py` |
| `SourceBarLedger(artifacts_root=, account_id=)` stores under `accounts/alpaca/<account_id>/`; queries: `by_identity`, `find_by_closed_end`, `bars(provider, symbol)`, `latest_for_symbol`; no range query | `app/services/source_bar_ledger.py:223-530` |
| `RetainedSourceBar` fields: `seq, account_id, provider, symbol, bar_identity, bar_ref, start_ms, end_ms, open, high, low, close (Decimal), volume, fetched_at_ms, session_phase, ...` | `source_bar_ledger.py:48` |
| `SyntheticActivationRecord/Store`: append-only JSONL at `accounts/synthetic/synthetic_activation.jsonl`, sha256-sealed rows, monotonic generations under an advisory lock | `app/broker/alpaca/clerk/synthetic_activation.py` |
| The sweep reads `list_orders(status="open", limit=500)` + `list_positions()`; verdicts `clean \| unexplained_order \| position_drift \| stale`; published through `on_result` → `facade.publish_sweep_reconciliation(result)` | `sqlite/reconcile.py:81, 487`, `runtime.py:985` |
| Order refs are `learn-ai/{sid}/v1:{intent}`; `parse_order_ref(order_ref) -> (namespace, intent)` raises `OrderRefParseError` on anything else | `app/engine/live/order_identity.py:135-165` |
| `MAX_OPEN_ORDER_SNAPSHOT = 500` | `sqlite/reconcile.py:71` |
| `SqliteEconomicProjectionReader(db_path=, account_id=, authority_generation=, db_identity_token=)` opens `mode=ro`; `account_fill_window(from_ms, to_ms, limit)` → `FillRecord(account_id, sid, intent_id, order_ref, event_key, symbol, side: OrderSide, quantity, fill_price, filled_at_ms, fee, ledger_sequence)` | `sqlite/economic_projection.py:187-348`, `clerk/fills.py:62` |
| `runs` table columns: `run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, stopped_at_ms` → `RunResource` | `sqlite/repository_read_api.py:70-88`, `sqlite/models.py:153` |
| `writes.confined_account_file(artifacts_root, account_id, filename)`; `DB_FILENAME = "clerk.db"` | `sqlite/writes.py:66`, `sqlite/repository.py:98` |
| `AlpacaLiveVerdict`: `clerk_authority: Literal["sqlite","synthetic","unavailable","not_installed"]`, `shadow_state: Literal["not_applicable","none","in_progress","complete"]`; the composer is pure | `app/schemas/alpaca_live_verdict.py`, `app/services/alpaca_live_verdict.py` |
| Panel rows: `_validate_simulated_authority_metadata` admits only `(sim:, synthetic)`; `sqlite_panel_adapter` stamps kind by `startswith("sim:")`; `panel_projection_service.project_*` marks `simulated=True` only on the `dry_run` branch | `app/schemas/broker_v2_panel.py:38`, `services/broker_v2_panel/sqlite_panel_adapter.py:605`, `panel_projection_service.py:494-560` |
| Deploy: `AlpacaPaperDeployRequest.execution_mode: Literal["paper","dry_run"]`; `AlpacaPaperExecutionMode.mode: Literal["paper","dry_run","live"]`; `AlpacaPaperDeployStrategy.admissible_modes: tuple[Literal["dry_run","paper"], ...]`; `AlpacaPaperDeployView.account_mode: Literal["paper"]`; `get_alpaca_paper_deploy_view` refuses `account_mode != "paper"`; `_admissible_modes`; `account_ready` requires paper; the view hardcodes `account_mode="paper"` and the label | `app/schemas/broker_bots.py:154, 246, 321, 347`, `services/broker_v2_panel/panel_deploy.py:65, 113, 160`, `paper_deploy_service.py:151, 237, 530-560` |
| Posture: `AccountOperatorPostureContext.account_mode` is set from the account snapshot; `account_mode != "paper"` is a blocking posture | `app/services/sqlite_clerk_compat.py:229`, `sqlite/account_operator_posture.py:323` |
| `developer_clean_slate_reset(account_mode=...)` refuses non-paper; `fault_injection` requires paper posture | `sqlite/dev_reset.py:108`, `app/broker/alpaca/fault_injection.py:84-145` |
| `main.py` builds `TradeUpdatesConsumer.for_alpaca(read=, evidence_sink=runtime.evidence_sink)` when `runtime.clerk is not None`, then `start_hold_sync()` | `app/main.py:260-272` |
| `TradeUpdatesConsumer.for_alpaca(*, read, evidence_sink: TradeUpdateEvidenceSink, ...)` — the sink is required | `app/broker/alpaca/trade_updates.py:674` |
| Frontend deploy workflow: `executionMode: Extract<DeployExecutionMode['mode'], 'dry_run' \| 'paper'>`; `setExecutionMode` guards `dry_run \| paper`; `paperUnavailableReason` / `dryRunUnavailableReason`; the section component maps mode → reason | `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts:66, 288-300, 362-372, 551-566`, `deploy-execution-section.component.ts:77-78` |
| `SealedBotProgram` carries `configured_signal_hash`, `sealed_account_id`, `mode`, `action_plan`, `quantity`, `carryover_policy`, `bot_configuration_hash` | `app/schemas/signal_program_seal.py:304-322` |
| `live_state_binding_repository(root).read(sid) -> BrokerBotBinding \| None` (with `sealed_program`) | `app/services/bot_binding_repository.py:334, 818` |

## File structure

- Create `PythonDataService/app/broker/alpaca/clerk/synthesized_orders.py` — the durable synthesized order ledger + `project_positions` (extracted from `synthetic_broker.py`).
- Modify `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py` — thin over the ledger; behaviour unchanged.
- Modify `PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py` — namespace-parameterised base; create `.../shadow_activation.py`.
- Modify `PythonDataService/app/services/source_bar_ledger.py` — `bars_after(provider, symbol, start_ms)`.
- Create `PythonDataService/app/broker/alpaca/clerk/shadow_broker.py` — `ShadowOrderBook`, `NoSubmitAlpacaTradePort`, `ShadowAccountReadPort`, `compose_shadow_ports`, `verify_shadow_namespace_empty`.
- Modify `PythonDataService/app/broker/alpaca/clerk/account_authority.py` — `SHADOW_EVIDENCE_ACCOUNT_PREFIX`, `shadow_evidence_account_id_for_strategy`, `evidence_account_id_for`.
- Create `PythonDataService/app/broker/alpaca/clerk/shadow_sessions.py` — `ShadowSessionLedger`, `ShadowSessionRecorder`, `completed_session_days`.
- Modify `PythonDataService/app/broker/alpaca/clerk/active_authority.py`, `sqlite/runtime.py`, `active_protocol.py`, `app/main.py`, `app/services/bot_trade_strategy.py`, `app/services/bot_binding_authority.py`, `app/services/run_replay_proof.py` — the shadow authority is selectable, runnable, and evidence-scoped.
- Modify the gate and panel sites listed in Task 6.
- Create `PythonDataService/app/services/alpaca_shadow_reconciliation.py` — twin reconciliation + gate evaluation; modify `sqlite/economic_projection.py` (`from_database_path`, `runs_for_strategy`).
- Create `PythonDataService/app/broker/alpaca/clerk/shadow_receipt.py`; modify `app/schemas/alpaca_live_verdict.py`, `app/services/alpaca_live_verdict.py`, `app/routers/brokers.py`.
- Create `PythonDataService/scripts/manage_alpaca_shadow.py`.
- Modify Frontend deploy workflow (Task 10).
- Create `docs/references/alpaca-shadow-authority.md`; modify `docs/references/synthetic-broker-position-projection.md`, `docs/math-sources-of-truth.md`, `docs/architecture/engine-authority-map.md`, `CONTEXT.md`.

---
### Task 1: Extract the synthesized order ledger from the sim broker

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/synthesized_orders.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py` (whole file rewritten below; public names preserved)
- Modify: `docs/references/synthetic-broker-position-projection.md` (the "Authority and proof" pointer), `docs/math-sources-of-truth.md` (row "Dry Run synthetic position projection", currently line 141)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_synthesized_orders.py`; existing `tests/services/test_source_bar_ledger.py`, `tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py`, `tests/broker/alpaca/clerk/test_account_keyed_authority.py`, `tests/services/test_bot_binding_authority_source_bars.py` must pass unchanged.

**Interfaces:**
- Produces: `SynthesizedAnchor`, `SynthesizedOrderRecord(seq, order, leg=None, anchor=None)`, `SynthesizedBarBindingError`, `BarVerifier = Callable[[RetainedSourceBar], RetainedSourceBar]`, `single_ledger_verifier(account_id, source_bars) -> BarVerifier`, `SynthesizedOrderLedger(account_id=, path=, trusted_root=, verify_bar=, label=)` with classmethod `beside_source_bars(account_id=, source_bars=, label=)` (the sim layout), `path`, `bind_evaluated_bar`, `consume_bound_bar`, `verified_retained_bar`, `transaction()`, `latest_orders()`, `latest_records()`, `latest_orders_from_records`, `latest_records_from`, `find_order`, `find_record`, `append_locked`; `project_positions(orders)`. `SYNTHESIZED_ORDER_LEDGER_FILENAME == "simulated_orders.jsonl"` (unchanged on disk). The verifier is injected because the two worlds verify against different ledgers: the sim world has one evidence ledger per authority; the shadow world (Task 3) custodies one account but settles against per-instance evidence ledgers.
- `SyntheticBroker` keeps `SYNTHETIC_BROKER_ID`, `SYNTHETIC_CAPABILITIES`, `SimulatedPriceUnavailableError`, `SyntheticBarBindingError` (now an alias of `SynthesizedBarBindingError`), `bind_evaluated_bar`, `submit(leg, *, client_order_id, retained_bar=None)`, `cancel`, `get_order_by_client_order_id`, and the read-port methods, with byte-identical behaviour.

- [ ] **Step 1: Write the failing test**

`PythonDataService/tests/broker/alpaca/clerk/test_synthesized_orders.py`:

```python
"""The shared synthesized-order ledger both no-submit worlds write (ADR 0059 D2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthesized_orders import (
    SYNTHESIZED_ORDER_LEDGER_FILENAME,
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
    project_positions,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SourceBarLedger


def _bar(symbol: str = "SPY", *, start_ms: int = 1_000, close: str = "100.00") -> MarketDataBar:
    return MarketDataBar(
        feed_id="fixture",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=start_ms + 60_000,
        session_phase="RTH",
    )


def _order(client_order_id: str, *, side: str = "buy", filled: float = 1.0, price: float | None = 100.0) -> BrokerOrder:
    return BrokerOrder(
        broker="synthetic",
        order_id=f"sim-order:{client_order_id}",
        client_order_id=client_order_id,
        symbol="SPY",
        asset_class="us_equity",
        side=side,
        order_type="market",
        time_in_force="day",
        quantity=filled,
        filled_quantity=filled,
        limit_price=None,
        stop_price=None,
        filled_avg_price=price,
        status="filled",
        submitted_at_ms=1,
        created_at_ms=1,
        updated_at_ms=1,
        filled_at_ms=1,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=1,
    )


def _ledger(tmp_path: Path) -> tuple[SynthesizedOrderLedger, SourceBarLedger]:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    return SynthesizedOrderLedger.beside_source_bars(account_id="sim:ema-1", source_bars=bars, label="test"), bars


def test_ledger_file_name_is_the_sim_worlds_existing_file(tmp_path: Path) -> None:
    ledger, bars = _ledger(tmp_path)
    assert ledger.path == bars.path.with_name(SYNTHESIZED_ORDER_LEDGER_FILENAME)
    assert SYNTHESIZED_ORDER_LEDGER_FILENAME == "simulated_orders.jsonl"


def test_ledger_refuses_a_source_ledger_from_another_account(tmp_path: Path) -> None:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:other")
    with pytest.raises(SynthesizedBarBindingError, match="share one account authority"):
        SynthesizedOrderLedger.beside_source_bars(account_id="sim:ema-1", source_bars=bars, label="test")


def test_bar_from_another_authority_is_refused_by_the_verifier(tmp_path: Path) -> None:
    ledger, _bars = _ledger(tmp_path)
    other = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:other").append(_bar(), run_id="run-1")
    with pytest.raises(SynthesizedBarBindingError, match="different account authority"):
        ledger.bind_evaluated_bar("learn-ai/ema-1/v1:a", other)


def test_records_round_trip_with_leg_and_anchor(tmp_path: Path) -> None:
    ledger, bars = _ledger(tmp_path)
    retained = bars.append(_bar(), run_id="run-1")
    leg = BrokerOrderLeg(symbol="SPY", side=OrderSide.BUY, quantity=1.0, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY)
    anchor = SynthesizedAnchor(
        fill_model="decision_bar_close",
        evidence_account_id=retained.account_id,
        provider=retained.provider,
        bar_identity=retained.bar_identity,
        bar_ref=retained.bar_ref,
        decision_bar_start_ms=retained.start_ms,
        decision_bar_end_ms=retained.end_ms,
        fill_bar_ref=retained.bar_ref,
    )
    with ledger.transaction() as records:
        ledger.append_locked(records, order=_order("learn-ai/ema-1/v1:a"), leg=leg, anchor=anchor)

    record = ledger.latest_records()["learn-ai/ema-1/v1:a"]
    assert record.leg == leg
    assert record.anchor == anchor
    assert ledger.latest_orders()[0].client_order_id == "learn-ai/ema-1/v1:a"


def test_pre_anchor_rows_still_parse(tmp_path: Path) -> None:
    ledger, _bars = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    row = SynthesizedOrderRecord(seq=1, order=_order("learn-ai/ema-1/v1:old"))
    ledger.path.write_text(row.model_dump_json(exclude={"leg", "anchor"}) + "\n", encoding="utf-8")

    assert ledger.latest_orders()[0].client_order_id == "learn-ai/ema-1/v1:old"
    assert ledger.latest_records()["learn-ai/ema-1/v1:old"].anchor is None


def test_bound_bar_is_consumed_once_and_verified(tmp_path: Path) -> None:
    ledger, bars = _ledger(tmp_path)
    retained = bars.append(_bar(), run_id="run-1")
    ledger.bind_evaluated_bar("learn-ai/ema-1/v1:a", retained)

    assert ledger.consume_bound_bar("learn-ai/ema-1/v1:a") == retained
    assert ledger.consume_bound_bar("learn-ai/ema-1/v1:a") is None
    with pytest.raises(SynthesizedBarBindingError, match="does not match the submitted order symbol"):
        ledger.verified_retained_bar(retained, symbol="QQQ")


def test_project_positions_is_the_average_cost_fold() -> None:
    orders = [
        _order("a", side="buy", filled=2.0, price=100.0),
        _order("b", side="sell", filled=1.0, price=110.0),
        _order("c", side="buy", filled=1.0, price=120.0),
    ]
    quantity, notional = project_positions(orders)["SPY"]
    assert quantity == 2.0
    assert notional == pytest.approx(220.0, abs=0.0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_synthesized_orders.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: app.broker.alpaca.clerk.synthesized_orders`.

- [ ] **Step 3: Create `synthesized_orders.py`**

```python
"""Durable synthesized-order ledger for the authorities that never submit.

Two worlds write synthesized fills — the ``sim:`` Dry Run world and the
``shadow:`` world (ADR 0059 D2) — and both need the same core: an append-only,
cross-process-locked order WAL keyed by ``client_order_id``; a per-order binding
of the exact retained decision bar the Clerk evaluated; a verifier that the
bound bar is the durable observation and belongs to this authority; and the
average-cost position projection over the ledger's fills. What differs between
the worlds — how a leg becomes a fill, and where account truth comes from —
lives in ``synthetic_broker.py`` and ``shadow_broker.py``.

Every record may carry the ``leg`` it answered and a ``SynthesizedAnchor``: the
fill model and the decision bar the fill was synthesized from (ADR 0002's third
invariant — synthetic fills declare their provenance). Both are optional so
rows written before this module existed still parse.

Math Provenance Contract
------------------------
Formula: for each symbol, the projected position is an average-cost fold of
durable synthesized fills. Same-direction fills add signed entry notional;
reductions retain the prior average cost for the remaining quantity; a flip
opens only the residual at the flip fill price. A position is emitted iff
``position_quantity_is_nonzero(quantity)``.
Reference: average-cost broker position convention, recorded in
``docs/references/synthetic-broker-position-projection.md``.
Canonical implementation: ``project_positions`` in this module.
Validated against: ``tests/services/test_source_bar_ledger.py`` exact
buy/reduce/add/flip parity fixture (``atol=0``, ``rtol=0``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.services.jsonl_wal import JsonlWal
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.advisory_lock import advisory_file_lock

# The sim world's existing file name; the shadow world writes the same shape
# in its own custody directory, so one reader serves both.
SYNTHESIZED_ORDER_LEDGER_FILENAME = "simulated_orders.jsonl"
FillModel = Literal["decision_bar_close", "limit_touch"]
_ORDER_LOCKS: dict[str, threading.Lock] = {}
_ORDER_LOCKS_GUARD = threading.Lock()


class SynthesizedBarBindingError(RuntimeError):
    """A proposed synthesized fill is not bound to its exact retained source bar."""


class SynthesizedAnchor(BaseModel):
    """Where one synthesized order's fill came from (ADR 0002 invariant 3).

    ``cancel_at_ms`` is set only for a resting model — the instant the vendor
    would have cancelled the order. ``fill_bar_ref`` names the retained bar
    that produced the fill once one has; ``None`` while the order rests or
    after it cancelled unfilled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_model: FillModel
    evidence_account_id: str
    provider: str
    bar_identity: str
    bar_ref: str
    decision_bar_start_ms: int = Field(ge=0)
    decision_bar_end_ms: int = Field(ge=0)
    cancel_at_ms: int | None = Field(default=None, ge=0)
    fill_bar_ref: str | None = None


class SynthesizedOrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    order: BrokerOrder
    leg: BrokerOrderLeg | None = None
    anchor: SynthesizedAnchor | None = None


def _corrupt_order_ledger(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Synthetic order ledger corrupt at {path}: {detail}")


def _order_lock(path: Path) -> threading.Lock:
    """Return the process-local half of one order-ledger transaction lock."""
    key = str(path)
    with _ORDER_LOCKS_GUARD:
        lock = _ORDER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ORDER_LOCKS[key] = lock
        return lock


BarVerifier = Callable[[RetainedSourceBar], RetainedSourceBar]


def single_ledger_verifier(account_id: str, source_bars: SourceBarLedger) -> BarVerifier:
    """The sim world's verifier: one authority, one evidence ledger."""

    def verify(retained_bar: RetainedSourceBar) -> RetainedSourceBar:
        if retained_bar.account_id != account_id:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding belongs to a different account authority."
            )
        persisted = source_bars.by_identity(retained_bar.bar_identity)
        if persisted != retained_bar:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding is not the exact retained source-bar observation."
            )
        return persisted

    return verify


class SynthesizedOrderLedger:
    """Append-only order WAL plus exact decision-bar binding for one authority."""

    def __init__(
        self,
        *,
        account_id: str,
        path: Path,
        trusted_root: Path,
        verify_bar: BarVerifier,
        label: str,
    ) -> None:
        self.account_id = account_id
        self._verify_bar = verify_bar
        self._bound_bars: dict[str, RetainedSourceBar] = {}
        self._binding_lock = threading.Lock()
        self._orders: JsonlWal[SynthesizedOrderRecord] = JsonlWal(
            path,
            record_model=SynthesizedOrderRecord,
            corrupt_error=_corrupt_order_ledger,
            seq_of=lambda row: row.seq,
            label=label,
            trusted_root=trusted_root,
        )

    @classmethod
    def beside_source_bars(
        cls, *, account_id: str, source_bars: SourceBarLedger, label: str
    ) -> SynthesizedOrderLedger:
        """The sim world's layout: the WAL beside the authority's one evidence ledger."""
        if source_bars.account_id != account_id:
            raise SynthesizedBarBindingError(
                "Synthesized order ledger and retained source-bar ledger must share one account authority."
            )
        return cls(
            account_id=account_id,
            path=source_bars.path.with_name(SYNTHESIZED_ORDER_LEDGER_FILENAME),
            trusted_root=source_bars.path.parent,
            verify_bar=single_ledger_verifier(account_id, source_bars),
            label=label,
        )

    @property
    def path(self) -> Path:
        return self._orders.path

    # ── decision-bar binding ────────────────────────────────────────────

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        """Bind one minted Clerk order identity to one exact retained decision bar.

        The binding is consumed by that exact ``client_order_id`` on submit.
        Rebinding the same id deterministically replaces an unconsumed binding,
        while a later retained bar for the same symbol cannot replace it.
        """
        if not client_order_id:
            raise SynthesizedBarBindingError("Synthetic bar binding requires a client order id.")
        canonical = self.verified_retained_bar(retained_bar, symbol=None)
        with self._binding_lock:
            self._bound_bars[client_order_id] = canonical

    def consume_bound_bar(self, client_order_id: str) -> RetainedSourceBar | None:
        with self._binding_lock:
            return self._bound_bars.pop(client_order_id, None)

    def verified_retained_bar(
        self,
        retained_bar: RetainedSourceBar,
        *,
        symbol: str | None,
    ) -> RetainedSourceBar:
        if symbol is not None and retained_bar.symbol != symbol:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding does not match the submitted order symbol."
            )
        return self._verify_bar(retained_bar)

    # ── the order WAL ───────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[list[SynthesizedOrderRecord]]:
        """Serialize a full order-ledger read/check/append across threads and processes."""
        with _order_lock(self._orders.path), advisory_file_lock(self._orders.path):
            yield self._orders.read_all()

    def latest_orders(self) -> list[BrokerOrder]:
        return self.latest_orders_from_records(self._orders.read_all())

    def latest_records(self) -> dict[str, SynthesizedOrderRecord]:
        return self.latest_records_from(self._orders.read_all())

    @staticmethod
    def latest_records_from(
        records: list[SynthesizedOrderRecord],
    ) -> dict[str, SynthesizedOrderRecord]:
        latest: dict[str, SynthesizedOrderRecord] = {}
        for row in records:
            latest[row.order.client_order_id or row.order.order_id] = row
        return latest

    @classmethod
    def latest_orders_from_records(cls, records: list[SynthesizedOrderRecord]) -> list[BrokerOrder]:
        return [row.order for row in cls.latest_records_from(records).values()]

    def find_order(
        self,
        records: list[SynthesizedOrderRecord],
        client_order_id: str,
    ) -> BrokerOrder | None:
        record = self.find_record(records, client_order_id)
        return None if record is None else record.order

    def find_record(
        self,
        records: list[SynthesizedOrderRecord],
        client_order_id: str,
    ) -> SynthesizedOrderRecord | None:
        return next(
            (
                record
                for record in self.latest_records_from(records).values()
                if record.order.client_order_id == client_order_id
            ),
            None,
        )

    def append_locked(
        self,
        records: list[SynthesizedOrderRecord],
        *,
        order: BrokerOrder,
        leg: BrokerOrderLeg | None = None,
        anchor: SynthesizedAnchor | None = None,
    ) -> SynthesizedOrderRecord:
        """Append inside :meth:`transaction`; ``records`` is that transaction's read."""
        next_seq = records[-1].seq + 1 if records else 1
        # A sibling instance may have appended since this instance's previous
        # write. Refresh the WAL's sequence cache while holding the
        # cross-process transaction lock so it cannot reuse a sequence.
        self._orders._next_seq = next_seq
        record = SynthesizedOrderRecord(seq=next_seq, order=order, leg=leg, anchor=anchor)
        self._orders.append(record)
        records.append(record)
        return record


def project_positions(orders: list[BrokerOrder]) -> dict[str, tuple[float, float]]:
    """Fold fills into canonical average-cost ``(quantity, signed_notional)``.

    The result intentionally contains signed notional: a long's notional is
    positive and a short's is negative. That representation makes both the
    same-direction weighted average and a side-flip's residual opening price
    exact at the broker model's float boundary.
    """
    positions: dict[str, tuple[float, float]] = {}
    for order in orders:
        if order.filled_quantity <= 0 or order.filled_avg_price is None:
            continue
        signed_fill = order.filled_quantity if order.side.lower() == "buy" else -order.filled_quantity
        quantity, notional = positions.get(order.symbol, (0.0, 0.0))
        if not position_quantity_is_nonzero(quantity) or quantity * signed_fill > 0:
            positions[order.symbol] = (
                quantity + signed_fill,
                notional + signed_fill * order.filled_avg_price,
            )
            continue

        next_quantity = quantity + signed_fill
        if not position_quantity_is_nonzero(next_quantity):
            positions[order.symbol] = (0.0, 0.0)
            continue
        if quantity * next_quantity > 0:
            average_entry_price = abs(notional / quantity)
            positions[order.symbol] = (next_quantity, next_quantity * average_entry_price)
            continue
        positions[order.symbol] = (next_quantity, next_quantity * order.filled_avg_price)
    return positions


__all__ = [
    "SYNTHESIZED_ORDER_LEDGER_FILENAME",
    "BarVerifier",
    "FillModel",
    "SynthesizedAnchor",
    "SynthesizedBarBindingError",
    "SynthesizedOrderLedger",
    "SynthesizedOrderRecord",
    "project_positions",
    "single_ledger_verifier",
]
```

Note `append_locked` now also appends the record to the transaction's list so a caller that appends twice inside one transaction sees the right next sequence — the sim broker never did that, and the shadow book's settlement (Task 3) does.

- [ ] **Step 4: Rewrite `synthetic_broker.py` over the ledger**

Replace the whole file with:

```python
"""Retained-bar-backed broker port for one isolated synthetic Clerk authority.

The ``sim:`` world's whole difference from the shadow world is its fill
model: a leg either transacts at the decision bar's close or is cancelled on
the spot (ruling R9). Everything durable — the order WAL, the decision-bar
binding, the position projection — is ``synthesized_orders.py``, shared with
``shadow_broker.py``; the position projection's provenance lives there.
"""

from __future__ import annotations

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.clerk.fill_models import immediate_fill_price
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.synthesized_orders import (
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOrderLedger,
    project_positions,
)
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.timestamps import now_ms_utc

SYNTHETIC_BROKER_ID = "synthetic"
SYNTHETIC_CAPABILITIES = BrokerCapabilities(
    broker=SYNTHETIC_BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    extended_hours_window=ALPACA_EXTENDED_HOURS_WINDOW,
    supported_order_types=("market", "limit"),
    data_feed="retained_source_bars",
    bars_may_gap=False,
    max_stream_symbols=0,
    max_concurrent_streams=0,
    rest_rate_limit_per_min=0,
)
# The sim world's historical name for the shared binding error.
SyntheticBarBindingError = SynthesizedBarBindingError


class SimulatedPriceUnavailableError(RuntimeError):
    """No retained bar exists from which a synthetic fill may be derived."""


class SyntheticBroker:
    """Durable immediate-fill port that derives prices only from retained bars.

    A market leg, or a limit leg marketable against the decision bar's close,
    fills immediately at that close. The sim world cannot rest an order: a
    non-marketable limit is canceled on the spot with zero fills (ruling R9)
    rather than waiting for a later bar to touch it.

    The Clerk remains the custody authority.  This adapter supplies the
    broker-shaped acknowledgement and fills it needs without contacting Alpaca
    or reading a second market-data source.
    """

    broker_id = SYNTHETIC_BROKER_ID

    def __init__(self, *, account_id: str, source_bars: SourceBarLedger | None = None) -> None:
        self._account_id = require_synthetic_account_id(account_id)
        self._source_bars = source_bars
        self._ledger: SynthesizedOrderLedger | None = (
            None
            if source_bars is None
            else SynthesizedOrderLedger.beside_source_bars(
                account_id=self._account_id, source_bars=source_bars, label="simulated_order"
            )
        )

    def capabilities(self) -> BrokerCapabilities:
        return SYNTHETIC_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        observed_at_ms = now_ms_utc()
        return BrokerAccountSnapshot(
            broker=self.broker_id,
            account_id=self._account_id,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=0.0,
            equity=0.0,
            buying_power=0.0,
            portfolio_value=0.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=observed_at_ms,
            observed_at_ms=observed_at_ms,
        )

    async def list_positions(self) -> list[BrokerPosition]:
        return synthesized_positions(self.broker_id, self._latest_orders())

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        return filter_synthesized_orders(self._latest_orders(), status=status, limit=limit, after_ms=after_ms)

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        del after_ms, limit
        return []

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        del status, limit
        return []

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        del symbol
        return None

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        observed_at_ms = now_ms_utc()
        return BrokerClockEvidence(
            broker=self.broker_id,
            is_open=False,
            vendor_timestamp_ms=observed_at_ms,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=observed_at_ms,
        )

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        del history_range
        return BrokerPortfolioHistory(
            timestamps=[],
            equity=[],
            profit_loss=[],
            base_value=0.0,
            timeframe="synthetic",
        )

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        """Bind one minted Clerk order identity to one exact retained decision bar."""
        if self._ledger is None:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding requires an authority-scoped retained-bar ledger."
            )
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar)

    async def submit(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
        retained_bar: RetainedSourceBar | None = None,
    ) -> BrokerOrder:
        if self._ledger is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        with self._ledger.transaction() as records:
            existing = self._ledger.find_order(records, client_order_id)
            if existing is not None:
                self._ledger.consume_bound_bar(client_order_id)
                return existing
            bar = self._submission_bar(
                client_order_id=client_order_id,
                symbol=leg.symbol,
                retained_bar=retained_bar,
            )
            order = self._resolved_order(leg, client_order_id=client_order_id, bar=bar)
            self._ledger.append_locked(
                records,
                order=order,
                leg=leg,
                anchor=SynthesizedAnchor(
                    fill_model="decision_bar_close",
                    evidence_account_id=bar.account_id,
                    provider=bar.provider,
                    bar_identity=bar.bar_identity,
                    bar_ref=bar.bar_ref,
                    decision_bar_start_ms=bar.start_ms,
                    decision_bar_end_ms=bar.end_ms,
                    fill_bar_ref=bar.bar_ref if order.status == "filled" else None,
                ),
            )
            return order

    async def cancel(self, order_id: str) -> None:
        if self._ledger is None:
            return
        with self._ledger.transaction() as records:
            record = next(
                (row for row in self._ledger.latest_records_from(records).values() if row.order.order_id == order_id),
                None,
            )
            if record is None or record.order.status == "filled":
                return
            now = now_ms_utc()
            self._ledger.append_locked(
                records,
                order=record.order.model_copy(
                    update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}
                ),
                leg=record.leg,
                anchor=record.anchor,
            )

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self._latest_orders() if order.client_order_id == client_order_id),
            None,
        )

    def _latest_orders(self) -> list[BrokerOrder]:
        return [] if self._ledger is None else self._ledger.latest_orders()

    def _submission_bar(
        self,
        *,
        client_order_id: str,
        symbol: str,
        retained_bar: RetainedSourceBar | None,
    ) -> RetainedSourceBar:
        assert self._ledger is not None and self._source_bars is not None
        bound = self._ledger.consume_bound_bar(client_order_id)
        candidate = retained_bar if retained_bar is not None else bound
        if candidate is not None:
            return self._ledger.verified_retained_bar(candidate, symbol=symbol)
        latest = self._source_bars.latest_for_symbol(symbol)
        if latest is None:
            raise SimulatedPriceUnavailableError(
                f"No retained source bar exists for {symbol!r}; refusing a synthetic fill."
            )
        return latest

    def _resolved_order(self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar) -> BrokerOrder:
        """The order this leg becomes against one decision bar: filled, or cancelled unfilled.

        The fill decision itself belongs to ``fill_models`` — a second copy of
        "would this have transacted?" living here is exactly how the sim world
        and the shadow port drift apart. This method only shapes the resulting
        ``BrokerOrder``.
        """
        at_ms = bar.end_ms
        # The sim world cannot rest an order: a non-marketable limit is
        # cancelled on the spot, with no execution (ruling R9).
        fill = immediate_fill_price(leg, bar.close)
        filled = fill is not None
        return BrokerOrder(
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
            filled_quantity=leg.quantity if filled else 0.0,
            filled_avg_price=float(fill) if fill is not None else None,
            status="filled" if filled else "canceled",
            filled_at_ms=at_ms if filled else None,
            canceled_at_ms=None if filled else at_ms,
            events=(
                [
                    {
                        "event_type": "fill",
                        "occurred_at_ms": at_ms,
                        "price": float(fill),
                        "quantity": leg.quantity,
                        "execution_id": f"sim-execution:{client_order_id}",
                    }
                ]
                if fill is not None
                else []
            ),
        )


def synthesized_positions(broker_id: str, orders: list[BrokerOrder]) -> list[BrokerPosition]:
    """Shape the canonical projection as broker positions (shared with the shadow read port)."""
    quantities = project_positions(orders)
    observed_at_ms = now_ms_utc()
    return [
        BrokerPosition(
            broker=broker_id,
            symbol=symbol,
            asset_id=None,
            asset_class="us_equity",
            quantity=quantity,
            side="long" if quantity > 0 else "short",
            average_entry_price=(abs(cost / quantity) if quantity else 0.0),
            market_value=abs(cost),
            cost_basis=abs(cost),
            current_price=None,
            unrealized_pl=0.0,
            unrealized_plpc=None,
            observed_at_ms=observed_at_ms,
        )
        for symbol, (quantity, cost) in quantities.items()
        if position_quantity_is_nonzero(quantity)
    ]


def filter_synthesized_orders(
    orders: list[BrokerOrder],
    *,
    status: str | None,
    limit: int | None,
    after_ms: int | None,
) -> list[BrokerOrder]:
    """The read port's order filter, newest first (shared with the shadow read port)."""
    if status is not None:
        orders = [order for order in orders if order.status == status]
    if after_ms is not None:
        orders = [order for order in orders if (order.updated_at_ms or 0) >= after_ms]
    return list(reversed(orders))[:limit]


__all__ = [
    "SYNTHETIC_BROKER_ID",
    "SYNTHETIC_CAPABILITIES",
    "SimulatedPriceUnavailableError",
    "SyntheticBarBindingError",
    "SyntheticBroker",
    "filter_synthesized_orders",
    "synthesized_positions",
]
```

Keep `list_orders` semantics identical to today (the sim filtered `status == status` and `updated_at_ms >= after_ms`, then reversed and sliced) — the helper is the same code moved.

- [ ] **Step 5: Re-point the two doc pointers**

In `docs/references/synthetic-broker-position-projection.md` replace `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py::_project_positions` with `PythonDataService/app/broker/alpaca/clerk/synthesized_orders.py::project_positions` and add one sentence after it: "Both no-submit worlds (`sim:` via `synthetic_broker.py`, `shadow:` via `shadow_broker.py`) read their positions through it." In `docs/math-sources-of-truth.md`'s row "Dry Run synthetic position projection" replace `synthetic_broker.py::_project_positions` with `synthesized_orders.py::project_positions` and change the trailing status cell to `**canonical for the isolated no-submit worlds (sim:, shadow:)** — reads the sealed synthesized order ledger and cannot author real custody.`

- [ ] **Step 6: Run the tests**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_synthesized_orders.py tests/services/test_source_bar_ledger.py tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py tests/broker/alpaca/clerk/test_account_keyed_authority.py tests/services/test_bot_binding_authority_source_bars.py tests/contracts -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
Expected: all pass, ruff clean.

- [ ] **Step 7: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/synthesized_orders.py PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py PythonDataService/tests/broker/alpaca/clerk/test_synthesized_orders.py docs/references/synthetic-broker-position-projection.md docs/math-sources-of-truth.md && git commit -q -m "refactor(clerk): the synthesized order ledger is one module both no-submit worlds share

Extracts the sim world's order WAL, decision-bar binding and position
projection into synthesized_orders.py so the shadow port (ADR 0059 D2) can
reuse them instead of copying them. Records now carry the leg and a fill
anchor (ADR 0002 invariant 3); pre-anchor rows still parse. Sim behaviour is
unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 2: The shadow activation fence

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py` (namespace-parameterised base; the synthetic names and messages unchanged)
- Create: `PythonDataService/app/broker/alpaca/clerk/shadow_activation.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_shadow_activation.py`; existing `tests/broker/alpaca/clerk/test_synthetic_activation.py` must pass unchanged.

**Interfaces:**
- Produces: `IsolatedActivationInvalid`, `IsolatedActivationConflict`, `IsolatedActivationRecord` (base dataclass with class vars `require_account_id`, `invalid_error`, `label`), `IsolatedActivationStore` (class vars `record_type`, `conflict_error`; `__init__(artifacts_root, *, namespace_dir, filename)`); `SHADOW_ACTIVATION_FILENAME = "shadow_activation.jsonl"`, `ShadowActivationInvalid`, `ShadowActivationConflict`, `ShadowActivationRecord`, `ShadowActivationStore(artifacts_root)` at `accounts/shadow/shadow_activation.jsonl`.
- Consumed by Task 5 (`activate_shadow_clerk_authority`, `select_active_clerk_runtime`).

- [ ] **Step 1: Write the failing test**

`PythonDataService/tests/broker/alpaca/clerk/test_shadow_activation.py`:

```python
"""The shadow world's append-only activation fence (ADR 0059 D2)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_activation import (
    SHADOW_ACTIVATION_FILENAME,
    ShadowActivationConflict,
    ShadowActivationInvalid,
    ShadowActivationRecord,
    ShadowActivationStore,
)
from app.broker.alpaca.clerk.synthetic_activation import (
    IsolatedActivationInvalid,
    SyntheticActivationRecord,
)


def _record(generation: int = 1) -> ShadowActivationRecord:
    return ShadowActivationRecord.create(
        account_id="shadow:9LIVE0001",
        authority_generation=generation,
        db_identity_token="sqlite-identity",
        activated_at_ms=1_700_000_000_000,
    )


def test_record_requires_the_shadow_namespace() -> None:
    with pytest.raises(ValueError, match="shadow: account identity"):
        ShadowActivationRecord.create(
            account_id="sim:ema-1",
            authority_generation=1,
            db_identity_token="t",
            activated_at_ms=1,
        )
    with pytest.raises(ShadowActivationInvalid, match="invalid values"):
        ShadowActivationRecord.from_payload({**asdict(_record()), "account_id": "9LIVE0001"})


def test_record_digest_verifies_and_rejects_tampering() -> None:
    record = _record()
    assert ShadowActivationRecord.from_payload(asdict(record)) == record
    with pytest.raises(ShadowActivationInvalid, match="digest does not verify"):
        ShadowActivationRecord.from_payload({**asdict(record), "activated_at_ms": 2})
    assert isinstance(ShadowActivationInvalid("x"), IsolatedActivationInvalid)


def test_store_lives_under_accounts_shadow_and_is_monotonic(tmp_path: Path) -> None:
    store = ShadowActivationStore(tmp_path)
    assert store.path == tmp_path / "accounts" / "shadow" / SHADOW_ACTIVATION_FILENAME
    assert store.latest("shadow:9LIVE0001") is None

    store.append(_record(1))
    store.append(_record(2))
    assert store.latest("shadow:9LIVE0001") == _record(2)
    with pytest.raises(ShadowActivationConflict, match="generation must increase"):
        store.append(_record(2))


def test_store_refuses_synthetic_rows_and_synthetic_store_refuses_shadow_rows(tmp_path: Path) -> None:
    store = ShadowActivationStore(tmp_path)
    with pytest.raises(ValueError, match="shadow: account identity"):
        store.latest("sim:ema-1")
    synthetic = SyntheticActivationRecord.create(
        account_id="sim:ema-1", authority_generation=1, db_identity_token="t", activated_at_ms=1
    )
    with pytest.raises(ShadowActivationInvalid):
        store.append(synthetic)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_activation.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: app.broker.alpaca.clerk.shadow_activation`.

- [ ] **Step 3: Generalise `synthetic_activation.py`**

Replace the file with:

```python
"""Durable, explicit activation fences for the isolated no-submit authorities.

One base, two namespaces: the ``sim:`` Dry Run world (``SyntheticActivation*``)
and the ``shadow:`` world (``shadow_activation.py``). Each namespace binds which
account ids it admits, its own error types, and its own append-only file under
``accounts/<namespace>/``; the sealing, verification and monotonic-generation
rules are the same code.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.paths import resolve_contained_path
from app.utils.advisory_lock import advisory_file_lock

SYNTHETIC_ACTIVATION_FILENAME = "synthetic_activation.jsonl"


class IsolatedActivationInvalid(ValueError):
    """A durable isolated-authority activation fence cannot be trusted."""


class IsolatedActivationConflict(IsolatedActivationInvalid):
    """Another writer activated the same authority generation first."""


class SyntheticActivationInvalid(IsolatedActivationInvalid):
    """The durable synthetic activation fence cannot be trusted."""


class SyntheticActivationConflict(SyntheticActivationInvalid, IsolatedActivationConflict):
    """Another writer activated the same synthetic authority generation first."""


# ``require_*_account_id`` deliberately raises a sibling domain error; this
# alias keeps the narrow constructor-validation catch readable.
AccountError = ValueError


@dataclass(frozen=True)
class IsolatedActivationRecord:
    """One append-only activation proof for an isolated Clerk account.

    Subclasses bind the namespace: ``require_account_id`` admits its ids,
    ``invalid_error`` is the error type every rejection raises, ``label`` is
    the human-readable prefix of those rejections.
    """

    schema_version: int
    account_id: str
    authority_generation: int
    db_identity_token: str
    activated_at_ms: int
    activation_sha256: str

    require_account_id: ClassVar[Callable[[str], str]]
    invalid_error: ClassVar[type[IsolatedActivationInvalid]] = IsolatedActivationInvalid
    label: ClassVar[str] = "isolated activation"

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        activated_at_ms: int,
    ) -> Self:
        payload = {
            "schema_version": 1,
            "account_id": cls.require_account_id(account_id),
            "authority_generation": authority_generation,
            "db_identity_token": db_identity_token,
            "activated_at_ms": activated_at_ms,
        }
        return cls(**payload, activation_sha256=_digest(payload))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        required = {
            "schema_version",
            "account_id",
            "authority_generation",
            "db_identity_token",
            "activated_at_ms",
            "activation_sha256",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise cls.invalid_error(f"{cls.label} record has an invalid shape")
        int_fields = ("schema_version", "authority_generation", "activated_at_ms")
        if any(type(payload[field]) is not int for field in int_fields):
            raise cls.invalid_error(f"{cls.label} record has invalid integer facts")
        if not -(2**63) <= payload["activated_at_ms"] <= 2**63 - 1:
            raise cls.invalid_error(f"{cls.label} timestamp is outside signed int64 range")
        string_fields = ("account_id", "db_identity_token", "activation_sha256")
        if any(type(payload[field]) is not str for field in string_fields):
            raise cls.invalid_error(f"{cls.label} record has invalid string facts")
        try:
            record = cls(**payload)
            cls.require_account_id(record.account_id)
        except (TypeError, AccountError, ValueError) as exc:
            raise cls.invalid_error(f"{cls.label} record has invalid values") from exc
        if record.schema_version != 1 or record.authority_generation < 1:
            raise cls.invalid_error(f"{cls.label} record has an unsupported generation")
        if not record.db_identity_token:
            raise cls.invalid_error(f"{cls.label} record has invalid identity facts")
        if record.activation_sha256 != _digest(_unsigned_payload(record)):
            raise cls.invalid_error(f"{cls.label} record digest does not verify")
        return record


@dataclass(frozen=True)
class SyntheticActivationRecord(IsolatedActivationRecord):
    """One append-only activation proof for a synthetic Clerk account."""

    require_account_id = staticmethod(require_synthetic_account_id)
    invalid_error = SyntheticActivationInvalid
    label = "synthetic activation"


class IsolatedActivationStore:
    """Append-only account-scoped activation store with no Alpaca cutover proof."""

    record_type: ClassVar[type[IsolatedActivationRecord]] = IsolatedActivationRecord
    conflict_error: ClassVar[type[IsolatedActivationConflict]] = IsolatedActivationConflict

    def __init__(self, artifacts_root: Path, *, namespace_dir: str, filename: str) -> None:
        self._path = resolve_contained_path(artifacts_root, "accounts", namespace_dir, filename)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _label(self) -> str:
        return self.record_type.label

    def latest(self, account_id: str) -> IsolatedActivationRecord | None:
        self.record_type.require_account_id(account_id)
        latest: IsolatedActivationRecord | None = None
        for record in self._read_all():
            if record.account_id != account_id:
                continue
            if latest is not None and record.authority_generation <= latest.authority_generation:
                raise self.record_type.invalid_error(f"{self._label} generations are not increasing")
            latest = record
        return latest

    def append(self, record: IsolatedActivationRecord) -> None:
        canonical = self.record_type.from_payload(asdict(record))
        # The prior-generation check and durable append form one transaction.
        # A sibling advisory lock serializes independent activation processes;
        # fsync remains the exact durable boundary inside that transaction.
        with advisory_file_lock(self._path):
            prior = self.latest(canonical.account_id)
            if prior is not None and canonical.authority_generation <= prior.authority_generation:
                raise self.conflict_error(f"{self._label} generation must increase")
            if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
                raise self.record_type.invalid_error(f"{self._label} ledger must be a regular file")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            existed = self._path.exists()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(canonical), sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

    def _read_all(self) -> list[IsolatedActivationRecord]:
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise self.record_type.invalid_error(f"{self._label} ledger must be a regular file")
        if not self._path.exists():
            return []
        records: list[IsolatedActivationRecord] = []
        try:
            for raw in self._path.read_text(encoding="utf-8").splitlines():
                if raw:
                    payload = json.loads(raw)
                    if not isinstance(payload, Mapping):
                        raise self.record_type.invalid_error(
                            f"{self._label} record has an invalid JSON payload shape"
                        )
                    records.append(self.record_type.from_payload(payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, IsolatedActivationInvalid) as exc:
            raise self.record_type.invalid_error(f"{self._label} ledger cannot be read") from exc
        return records


class SyntheticActivationStore(IsolatedActivationStore):
    """The ``sim:`` fence at ``accounts/synthetic/synthetic_activation.jsonl``."""

    record_type = SyntheticActivationRecord
    conflict_error = SyntheticActivationConflict

    def __init__(self, artifacts_root: Path) -> None:
        super().__init__(artifacts_root, namespace_dir="synthetic", filename=SYNTHETIC_ACTIVATION_FILENAME)

    def latest(self, account_id: str) -> SyntheticActivationRecord | None:  # narrow the return type
        record = super().latest(account_id)
        assert record is None or isinstance(record, SyntheticActivationRecord)
        return record


def _unsigned_payload(record: IsolatedActivationRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "account_id": record.account_id,
        "authority_generation": record.authority_generation,
        "db_identity_token": record.db_identity_token,
        "activated_at_ms": record.activated_at_ms,
    }


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


__all__ = [
    "SYNTHETIC_ACTIVATION_FILENAME",
    "IsolatedActivationConflict",
    "IsolatedActivationInvalid",
    "IsolatedActivationRecord",
    "IsolatedActivationStore",
    "SyntheticActivationConflict",
    "SyntheticActivationInvalid",
    "SyntheticActivationRecord",
    "SyntheticActivationStore",
]
```

Every synthetic error message is byte-identical to today's (`"synthetic activation record has an invalid shape"` etc.) because `label` reproduces the old prefix. The store's `append` note: a record of the *other* namespace is rejected by `record_type.from_payload` (the namespace check inside it), which is what the last test asserts.

- [ ] **Step 4: Create `shadow_activation.py`**

```python
"""The ``shadow:`` world's activation fence (ADR 0059 D2).

Modelled on the synthetic fence: a shadow authority has no custody until a
caller deliberately activates it, and the proof is an append-only,
sha256-sealed row under ``accounts/shadow/``. No startup path appends here;
``activate_shadow_clerk_authority`` (active_authority.py) is the one writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import require_shadow_account_id
from app.broker.alpaca.clerk.synthetic_activation import (
    IsolatedActivationConflict,
    IsolatedActivationInvalid,
    IsolatedActivationRecord,
    IsolatedActivationStore,
)

SHADOW_ACTIVATION_FILENAME = "shadow_activation.jsonl"


class ShadowActivationInvalid(IsolatedActivationInvalid):
    """The durable shadow activation fence cannot be trusted."""


class ShadowActivationConflict(ShadowActivationInvalid, IsolatedActivationConflict):
    """Another writer activated the same shadow authority generation first."""


@dataclass(frozen=True)
class ShadowActivationRecord(IsolatedActivationRecord):
    """One append-only activation proof for a shadow Clerk account."""

    require_account_id = staticmethod(require_shadow_account_id)
    invalid_error = ShadowActivationInvalid
    label = "shadow activation"


class ShadowActivationStore(IsolatedActivationStore):
    """The ``shadow:`` fence at ``accounts/shadow/shadow_activation.jsonl``."""

    record_type = ShadowActivationRecord
    conflict_error = ShadowActivationConflict

    def __init__(self, artifacts_root: Path) -> None:
        super().__init__(artifacts_root, namespace_dir="shadow", filename=SHADOW_ACTIVATION_FILENAME)

    def latest(self, account_id: str) -> ShadowActivationRecord | None:
        record = super().latest(account_id)
        assert record is None or isinstance(record, ShadowActivationRecord)
        return record


__all__ = [
    "SHADOW_ACTIVATION_FILENAME",
    "ShadowActivationConflict",
    "ShadowActivationInvalid",
    "ShadowActivationRecord",
    "ShadowActivationStore",
]
```

- [ ] **Step 5: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_activation.py tests/broker/alpaca/clerk/test_synthetic_activation.py tests/broker/alpaca/clerk/test_account_keyed_authority.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
Expected: all pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py PythonDataService/app/broker/alpaca/clerk/shadow_activation.py PythonDataService/tests/broker/alpaca/clerk/test_shadow_activation.py && git commit -q -m "feat(clerk): the shadow world gets its own append-only activation fence

One isolated-activation base now serves both no-submit namespaces; the
synthetic fence's names, paths and messages are unchanged, and the shadow
fence lives at accounts/shadow/shadow_activation.jsonl (ADR 0059 D2).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 3: The shadow ports — real live reads, synthesized fills, no submission

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/shadow_broker.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/account_authority.py` (evidence namespace helpers)
- Modify: `PythonDataService/app/services/source_bar_ledger.py` (`bars_after`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py` (extract `shape_immediate_order` from `_resolved_order`; behaviour unchanged)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_shadow_broker.py`, `tests/broker/alpaca/clerk/test_account_worlds.py` (three new tests)

**Interfaces:**
- Consumes: Task 1's `SynthesizedOrderLedger(account_id=, path=, trusted_root=, verify_bar=, label=)`, `SynthesizedAnchor`, `SynthesizedOrderRecord`; `fill_models.immediate_fill_price` / `limit_touch_fill`; `session_authority.declared_session_bounds(date, window)`; `sqlite/writes.account_paths` / `confined_account_file`; `order_identity.parse_order_ref` / `OrderRefParseError`; `reconcile.MAX_OPEN_ORDER_SNAPSHOT`.
- Produces (`account_authority.py`): `SHADOW_EVIDENCE_ACCOUNT_PREFIX = "shadow-evidence:"`, `shadow_evidence_account_id_for_strategy(sid)`, `is_shadow_evidence_account_id(account_id)`, `evidence_account_id_for(*, mode, strategy_instance_id, custody_kind) -> str | None`.
- Produces (`shadow_broker.py`): `SHADOW_BROKER_ID = "shadow"`, `ShadowFillBindingError`, `ShadowNamespacePoisoned(order_ids)` (`reason_code = "SHADOW_NAMESPACE_POISONED"`), `ShadowNamespaceUnproven` (`reason_code = "SHADOW_NAMESPACE_UNPROVEN"`), `EvidenceLedgers(artifacts_root)`, `ShadowOrderBook(ledger=, evidence=, window=, clock=)` with `bind_evaluated_bar`, `submit`, `cancel`, `settle`, `orders`, `order_by_client_order_id`, `record`, `positions`, `close`; `NoSubmitAlpacaTradePort(book)` (`BrokerTradePort` + `bind_evaluated_bar`); `ShadowAccountReadPort(live=, book=)` (`BrokerReadPort`); `ShadowPorts(read, trade, book, account_id)`; `compose_shadow_ports(*, live_read, live_account_id, artifacts_root, clock=now_ms_utc)`; `async verify_shadow_namespace_empty(read)`.
- Produces (`synthetic_broker.py`): `shape_immediate_order(leg, *, client_order_id, bar, broker_id, id_prefix, observed_at_ms) -> BrokerOrder` (the sim passes `broker_id="synthetic", id_prefix="sim"`).
- Produces (`source_bar_ledger.py`): `SourceBarLedger.bars_after(*, provider, symbol, start_ms) -> list[RetainedSourceBar]`.
- Consumed by Task 5 (composition + cold-start check), Task 4 (the evidence namespace in the binding authority and replay).

- [ ] **Step 1: Write the failing tests**

Append to `PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py`:

```python
from app.broker.alpaca.clerk.account_authority import (  # noqa: E402 — appended block; merge into the import above
    SHADOW_EVIDENCE_ACCOUNT_PREFIX,
    evidence_account_id_for,
    is_shadow_evidence_account_id,
    shadow_evidence_account_id_for_strategy,
)


def test_shadow_evidence_namespace_is_instance_scoped_and_not_a_custody_namespace() -> None:
    account_id = shadow_evidence_account_id_for_strategy("bot-a")
    assert account_id == f"{SHADOW_EVIDENCE_ACCOUNT_PREFIX}bot-a"
    assert is_shadow_evidence_account_id(account_id) is True
    assert is_shadow_account_id(account_id) is False
    assert is_shadow_evidence_account_id("shadow:9LIVE0001") is False


def test_evidence_account_id_follows_mode_then_custody_world() -> None:
    assert evidence_account_id_for(mode="dry_run", strategy_instance_id="b", custody_kind="real_paper") == "sim:b"
    assert evidence_account_id_for(mode="trade", strategy_instance_id="b", custody_kind="real_paper") == "paper:b"
    assert evidence_account_id_for(mode="trade", strategy_instance_id="b", custody_kind="shadow") == "shadow-evidence:b"
    # log_only retains into the world's instance namespace exactly as the
    # primary binding authority always did; replay refuses the mode itself.
    assert evidence_account_id_for(mode="log_only", strategy_instance_id="b", custody_kind="real_paper") == "paper:b"
```

(Merge the four names into the existing `from app.broker.alpaca.clerk.account_authority import (...)` block at the top of the file instead of adding a second import.)

Create `PythonDataService/tests/broker/alpaca/clerk/test_shadow_broker.py`:

```python
"""The shadow world's ports: live reads, synthesized fills, no submission (ADR 0059 D2, D5.5)."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.broker.alpaca.clerk.shadow_broker as shadow_broker_module
from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW, ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.account_authority import shadow_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.shadow_broker import (
    SHADOW_BROKER_ID,
    NoSubmitAlpacaTradePort,
    ShadowFillBindingError,
    ShadowNamespacePoisoned,
    ShadowNamespaceUnproven,
    ShadowPorts,
    compose_shadow_ports,
    verify_shadow_namespace_empty,
)
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    OrderSide,
    OrderType,
    TimeInForce,
)
from app.marketdata.feed import MarketDataBar
from app.services.session_authority import declared_session_bounds, et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

DAY = date(2026, 9, 8)  # a Tuesday; a full NYSE session
SID = "ema-shadow-1"
EVIDENCE = shadow_evidence_account_id_for_strategy(SID)
MINUTE_MS = 60_000


class _Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def _snapshot() -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id="9LIVE0001",
        account_mode="live",
        account_status="ACTIVE",
        currency="USD",
        cash=25_000.0,
        equity=25_000.0,
        buying_power=25_000.0,
        portfolio_value=25_000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1,
        observed_at_ms=2,
    )


def _vendor_order(client_order_id: str | None) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"vendor-{client_order_id}",
        client_order_id=client_order_id,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=1.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=100.0,
        status="filled",
        submitted_at_ms=1,
        created_at_ms=1,
        updated_at_ms=1,
        filled_at_ms=1,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=1,
    )


class _LiveRead:
    """A live read port double: real account truth, and it never serves positions."""

    broker_id = "alpaca"

    def __init__(self, orders: list[BrokerOrder] | None = None) -> None:
        self.orders = orders or []
        self.order_calls: list[str | None] = []

    def capabilities(self) -> Any:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        return _snapshot()

    async def list_positions(self) -> list:
        raise AssertionError("the shadow world never reads the live account's positions")

    async def list_orders(self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None) -> list[BrokerOrder]:
        del after_ms
        self.order_calls.append(status)
        return self.orders[:limit]

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list:
        del after_ms, limit
        return []

    async def list_assets(self, *, status: str | None = None, limit: int | None = 100) -> list:
        del status, limit
        return []

    async def get_asset(self, symbol: str) -> None:
        del symbol
        return None

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        return BrokerClockEvidence(broker="alpaca", is_open=True, vendor_timestamp_ms=7, next_open_ms=None, next_close_ms=None, observed_at_ms=7)

    async def get_portfolio_history(self, history_range: Any) -> BrokerPortfolioHistory:
        del history_range
        return BrokerPortfolioHistory(timestamps=[], equity=[], profit_loss=[], base_value=None, timeframe="1D")


def _retain(
    bars: SourceBarLedger,
    *,
    minute: int,
    close: str,
    low: str | None = None,
    high: str | None = None,
    phase: str = "RTH",
) -> RetainedSourceBar:
    start_ms = et_minute_of_day_ms(DAY, minute)
    return bars.append(
        MarketDataBar(
            feed_id="fixture",
            symbol="SPY",
            start_ms=start_ms,
            end_ms=start_ms + MINUTE_MS,
            open=Decimal(close),
            high=Decimal(high or close),
            low=Decimal(low or close),
            close=Decimal(close),
            volume=1,
            fetched_at_ms=start_ms + MINUTE_MS,
            session_phase=phase,
        ),
        run_id="run-1",
    )


def _market_leg() -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side=OrderSide.BUY, quantity=1.0, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY)


def _extended_leg(limit: float) -> BrokerOrderLeg:
    return BrokerOrderLeg(
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=1.0,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=limit,
        extended_hours=True,
    )


@pytest.fixture
def world(tmp_path: Path) -> tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]:
    clock = _Clock(et_minute_of_day_ms(DAY, 600))
    live = _LiveRead()
    ports = compose_shadow_ports(live_read=live, live_account_id="9LIVE0001", artifacts_root=tmp_path, clock=clock)
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=EVIDENCE)
    return ports, bars, live, clock


async def test_regular_session_leg_fills_at_the_decision_bar_close(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, _clock = world
    decision = _retain(bars, minute=600, close="100.25")
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:a", decision)

    order = await ports.trade.submit(_market_leg(), client_order_id="learn-ai/ema-shadow-1/v1:a")

    assert order.broker == SHADOW_BROKER_ID
    assert order.order_id == "shadow-order:learn-ai/ema-shadow-1/v1:a"
    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 100.25, decision.end_ms)
    assert order.events[0].execution_id == "shadow-execution:learn-ai/ema-shadow-1/v1:a"
    record = ports.book.record("learn-ai/ema-shadow-1/v1:a")
    assert record is not None and record.anchor is not None
    assert (record.anchor.fill_model, record.anchor.fill_bar_ref) == ("decision_bar_close", decision.bar_ref)
    assert [p.symbol for p in await ports.read.list_positions()] == ["SPY"]


async def test_submit_without_a_bound_decision_bar_is_refused(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, _clock = world
    _retain(bars, minute=600, close="100.25")
    with pytest.raises(ShadowFillBindingError, match="bound to this order"):
        await ports.trade.submit(_market_leg(), client_order_id="learn-ai/ema-shadow-1/v1:unbound")


async def test_extended_leg_rests_then_fills_at_the_limit_when_a_later_bar_touches(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, clock = world
    decision = _retain(bars, minute=1020, close="100.00", low="99.50", phase="POST")  # 17:00 ET
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:x", decision)

    resting = await ports.trade.submit(_extended_leg(100.50), client_order_id="learn-ai/ema-shadow-1/v1:x")
    assert (resting.status, resting.filled_quantity, resting.extended_hours) == ("new", 0.0, True)
    record = ports.book.record("learn-ai/ema-shadow-1/v1:x")
    assert record is not None and record.anchor is not None
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None and record.anchor.cancel_at_ms == bounds.close_ms

    _retain(bars, minute=1021, close="100.80", low="100.70", phase="POST")  # does not touch
    assert (await ports.read.list_orders())[0].status == "new"

    touching = _retain(bars, minute=1022, close="100.60", low="100.40", phase="POST")
    clock.now_ms = touching.end_ms + 1
    order = await ports.trade.get_order_by_client_order_id("learn-ai/ema-shadow-1/v1:x")
    assert order is not None
    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 100.5, touching.end_ms)
    settled = ports.book.record("learn-ai/ema-shadow-1/v1:x")
    assert settled is not None and settled.anchor is not None
    assert (settled.anchor.fill_model, settled.anchor.fill_bar_ref) == ("limit_touch", touching.bar_ref)


async def test_extended_leg_cancels_one_bucket_after_the_declared_close_when_untouched(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, clock = world
    decision = _retain(bars, minute=1190, close="100.00", low="100.00", phase="POST")  # 19:50 ET
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:y", decision)
    await ports.trade.submit(_extended_leg(99.00), client_order_id="learn-ai/ema-shadow-1/v1:y")
    for minute in range(1191, 1200):
        _retain(bars, minute=minute, close="100.00", low="99.90", phase="POST")
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None

    clock.now_ms = bounds.close_ms + MINUTE_MS - 1
    assert (await ports.read.list_orders())[0].status == "new"

    clock.now_ms = bounds.close_ms + MINUTE_MS
    order = (await ports.read.list_orders())[0]
    assert (order.status, order.canceled_at_ms, order.filled_quantity) == ("canceled", bounds.close_ms, 0.0)
    assert await ports.read.list_positions() == []


async def test_cancel_marks_a_resting_order_canceled_and_is_a_no_op_once_filled(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, clock = world
    decision = _retain(bars, minute=1020, close="100.00", phase="POST")
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:c", decision)
    resting = await ports.trade.submit(_extended_leg(99.00), client_order_id="learn-ai/ema-shadow-1/v1:c")
    clock.now_ms = decision.end_ms + 5

    await ports.trade.cancel(resting.order_id)
    canceled = await ports.trade.get_order_by_client_order_id("learn-ai/ema-shadow-1/v1:c")
    assert canceled is not None and (canceled.status, canceled.canceled_at_ms) == ("canceled", decision.end_ms + 5)

    filled_decision = _retain(bars, minute=600, close="100.25")
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:f", filled_decision)
    filled = await ports.trade.submit(_market_leg(), client_order_id="learn-ai/ema-shadow-1/v1:f")
    await ports.trade.cancel(filled.order_id)
    still_filled = await ports.trade.get_order_by_client_order_id("learn-ai/ema-shadow-1/v1:f")
    assert still_filled is not None and still_filled.status == "filled"


async def test_read_port_serves_live_account_truth_and_synthesized_custody(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, _bars, _live, _clock = world
    assert (await ports.read.get_account()).account_mode == "live"
    assert (await ports.read.get_clock_evidence()).vendor_timestamp_ms == 7
    assert ports.read.capabilities() is ALPACA_LIVE_CAPABILITIES
    assert await ports.read.list_positions() == []
    assert await ports.read.list_orders() == []
    assert ports.account_id == "shadow:9LIVE0001"
    assert ports.read.broker_id == SHADOW_BROKER_ID == ports.trade.broker_id


async def test_namespace_check_passes_clean_accounts_and_refuses_poison_or_full_pages() -> None:
    await verify_shadow_namespace_empty(_LiveRead([_vendor_order(None), _vendor_order("operator-ticket-7")]))

    with pytest.raises(ShadowNamespacePoisoned) as poisoned:
        await verify_shadow_namespace_empty(_LiveRead([_vendor_order("learn-ai/ema-1/v1:abc")]))
    assert poisoned.value.reason_code == "SHADOW_NAMESPACE_POISONED"
    assert poisoned.value.order_ids == ("vendor-learn-ai/ema-1/v1:abc",)

    full_page = _LiveRead([_vendor_order(None)] * 500)
    with pytest.raises(ShadowNamespaceUnproven) as unproven:
        await verify_shadow_namespace_empty(full_page)
    assert unproven.value.reason_code == "SHADOW_NAMESPACE_UNPROVEN"
    assert full_page.order_calls == ["all"]


def test_bars_after_returns_one_stream_in_open_order(tmp_path: Path) -> None:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=EVIDENCE)
    first = _retain(bars, minute=1020, close="1")
    second = _retain(bars, minute=1021, close="2")
    third = _retain(bars, minute=1022, close="3")

    assert bars.bars_after(provider="fixture", symbol="SPY", start_ms=first.end_ms) == [second, third]
    assert bars.bars_after(provider="fixture", symbol="QQQ", start_ms=0) == []


def test_the_trade_port_holds_no_vendor_client() -> None:
    source = inspect.getsource(shadow_broker_module)
    assert "TradingClient" not in source and "AlpacaBroker" not in source
    assert list(inspect.signature(NoSubmitAlpacaTradePort.__init__).parameters) == ["self", "book"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_broker.py tests/broker/alpaca/clerk/test_account_worlds.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError` on the new names.

- [ ] **Step 3: Add the evidence namespace helpers to `account_authority.py`**

Below `paper_evidence_account_id_for_strategy` add:

```python
SHADOW_EVIDENCE_ACCOUNT_PREFIX = "shadow-evidence:"
"""Instance-scoped evidence namespace for shadow retained source bars.

Custody is the account-scoped ``shadow:<live_account_id>`` authority; every
instance that runs on it keeps its own retained-bar ledger here, exactly as a
real-paper instance keeps ``paper:<instance>``. The prefix is deliberately not
``shadow:`` — an evidence namespace is never a custody identity.
"""


def shadow_evidence_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the isolated shadow source-bar namespace for one instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{SHADOW_EVIDENCE_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"


def is_shadow_evidence_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` is a shadow instance's evidence namespace."""
    return account_id.startswith(SHADOW_EVIDENCE_ACCOUNT_PREFIX)


def evidence_account_id_for(
    *,
    mode: str,
    strategy_instance_id: str,
    custody_kind: AccountAuthorityKind,
) -> str:
    """The evidence namespace whose ledger retains a binding's bars.

    Dry Run's custody and evidence share ``sim:<instance>``. Every other
    binding's evidence is instance-scoped under the world the primary
    authority custodies in — ``paper:`` on the real-paper authority,
    ``shadow-evidence:`` on the shadow authority — so two instances on one
    symbol never share a ledger and a replay proof reads exactly what its run
    retained. Whether a mode is replayable is the replay proof's judgement,
    not this function's.
    """
    if mode == "dry_run":
        return synthetic_account_id_for_strategy(strategy_instance_id)
    if custody_kind == "shadow":
        return shadow_evidence_account_id_for_strategy(strategy_instance_id)
    return paper_evidence_account_id_for_strategy(strategy_instance_id)
```

Add the four new names to `__all__`.

- [ ] **Step 4: Add `bars_after` to `SourceBarLedger`**

Directly after `bars(...)`:

```python
    def bars_after(self, *, provider: str, symbol: str, start_ms: int) -> list[RetainedSourceBar]:
        """Return one stream's retained observations opening at or after ``start_ms``, in open order.

        The shadow port settles a resting order from exactly these bars (ADR
        0059 D5.5: eligibility begins with the first bar after the decision
        bar), so the filter is on the bar's open, not its close.
        """
        with self._lock:
            rows = self._conn.execute(
                f"""
                {_BARS_WITH_JOURNAL}
                WHERE b.provider = ? AND b.symbol = ? AND b.start_ms >= ?
                ORDER BY b.start_ms ASC, b.seq ASC
                """,
                (provider, symbol, start_ms),
            ).fetchall()
        return [_retained_row(row) for row in rows]
```

- [ ] **Step 5: Extract `shape_immediate_order` in `synthetic_broker.py`**

Replace the body of `SyntheticBroker._resolved_order` with a call, and add the module function the shadow book reuses:

```python
    def _resolved_order(self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar) -> BrokerOrder:
        """The order this leg becomes against one decision bar: filled, or cancelled unfilled (ruling R9)."""
        return shape_immediate_order(
            leg,
            client_order_id=client_order_id,
            bar=bar,
            broker_id=self.broker_id,
            id_prefix="sim",
            observed_at_ms=now_ms_utc(),
        )
```

```python
def shape_immediate_order(
    leg: BrokerOrderLeg,
    *,
    client_order_id: str,
    bar: RetainedSourceBar,
    broker_id: str,
    id_prefix: str,
    observed_at_ms: int,
) -> BrokerOrder:
    """One decision bar, one answer: filled at its close, or cancelled unfilled.

    The fill decision itself belongs to ``fill_models`` — a second copy of
    "would this have transacted?" living here is exactly how the sim world and
    the shadow port drift apart. This function only shapes the resulting
    ``BrokerOrder``; both no-submit worlds call it for a regular-session leg.
    """
    at_ms = bar.end_ms
    fill = immediate_fill_price(leg, bar.close)
    filled = fill is not None
    return BrokerOrder(
        broker=broker_id,
        order_id=f"{id_prefix}-order:{client_order_id}",
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
        observed_at_ms=observed_at_ms,
        filled_quantity=leg.quantity if filled else 0.0,
        filled_avg_price=float(fill) if fill is not None else None,
        status="filled" if filled else "canceled",
        filled_at_ms=at_ms if filled else None,
        canceled_at_ms=None if filled else at_ms,
        events=(
            [
                {
                    "event_type": "fill",
                    "occurred_at_ms": at_ms,
                    "price": float(fill),
                    "quantity": leg.quantity,
                    "execution_id": f"{id_prefix}-execution:{client_order_id}",
                }
            ]
            if fill is not None
            else []
        ),
    )
```

Add `shape_immediate_order` to `__all__`. The sim's output is byte-identical (same ids, same fields).

- [ ] **Step 6: Create `shadow_broker.py`**

```python
"""The shadow world's ports: real live reads, synthesized fills, no submission.

ADR 0059 D2: a Shadow Account Authority is the real live read port bound to a
trade port that never submits. Three objects live here:

* ``ShadowOrderBook`` — the synthesized orders and their settlement. A
  regular-session leg fills at its decision bar's close (``decision_bar_close``,
  the same ``immediate_fill_price`` the sim world uses). An extended-session
  limit leg rests and settles on every read under ``limit_touch`` (D5.5)
  against the bars its own instance retained after the decision bar, and it
  cancels at the declared window's close for the decision's trading day — the
  instant the vendor would have cancelled a DAY extended order.
* ``NoSubmitAlpacaTradePort`` — ``BrokerTradePort`` over the book. It holds no
  Alpaca client; nothing in this module can reach the vendor's write API.
* ``ShadowAccountReadPort`` — ``BrokerReadPort``: account, clock, activities,
  assets, portfolio history and capabilities from the live read port;
  positions and orders from the book, so the reconciliation sweep reconciles
  the world this Clerk custodies (ruling R3).

Settlement is driven by reads, not by a task: the sweep's periodic order and
position reads are the clock. A resting order cancels only once one bucket
(the decision bar's own length) has elapsed past the declared close (ruling
R5), so the closing bucket has been retained before the book concludes the
order never touched.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    is_shadow_evidence_account_id,
    shadow_account_id_for_live_account,
)
from app.broker.alpaca.clerk.fill_models import limit_touch_fill
from app.broker.alpaca.clerk.sqlite.reconcile import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.writes import account_paths, confined_account_file
from app.broker.alpaca.clerk.synthesized_orders import (
    SYNTHESIZED_ORDER_LEDGER_FILENAME,
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
)
from app.broker.alpaca.clerk.synthetic_broker import (
    filter_synthesized_orders,
    shape_immediate_order,
    synthesized_positions,
)
from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.broker.contract.ports import BrokerReadPort
from app.engine.live.order_identity import OrderRefParseError, parse_order_ref
from app.services.session_authority import declared_session_bounds
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

SHADOW_BROKER_ID = "shadow"
_RESTING = "new"
_TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})


class ShadowFillBindingError(RuntimeError):
    """A shadow order cannot be synthesized: no bound decision bar, or no declared window."""


class ShadowNamespacePoisoned(RuntimeError):
    """The live account already holds orders the Clerk minted (ADR 0002 invariant 1)."""

    reason_code = "SHADOW_NAMESPACE_POISONED"

    def __init__(self, order_ids: Sequence[str]) -> None:
        self.order_ids = tuple(order_ids)
        super().__init__(
            f"the live account already holds {len(self.order_ids)} order(s) in the "
            f"Clerk's namespace: {', '.join(self.order_ids)}"
        )


class ShadowNamespaceUnproven(RuntimeError):
    """The order history read is at its page boundary; emptiness cannot be proven."""

    reason_code = "SHADOW_NAMESPACE_UNPROVEN"


class EvidenceLedgers:
    """Read handles on the per-instance evidence ledgers the shadow world settles against."""

    def __init__(self, artifacts_root: Path) -> None:
        self._root = artifacts_root
        self._ledgers: dict[str, SourceBarLedger] = {}
        self._lock = threading.Lock()

    def ledger(self, account_id: str) -> SourceBarLedger:
        if not is_shadow_evidence_account_id(account_id):
            raise SynthesizedBarBindingError(
                "Shadow fills settle only against a shadow-evidence: ledger."
            )
        with self._lock:
            ledger = self._ledgers.get(account_id)
            if ledger is None:
                ledger = SourceBarLedger(artifacts_root=self._root, account_id=account_id)
                self._ledgers[account_id] = ledger
            return ledger

    def verify(self, retained_bar: RetainedSourceBar) -> RetainedSourceBar:
        persisted = self.ledger(retained_bar.account_id).by_identity(retained_bar.bar_identity)
        if persisted != retained_bar:
            raise SynthesizedBarBindingError(
                "Shadow bar binding is not the exact retained source-bar observation."
            )
        return persisted

    def bars_after(
        self, account_id: str, *, provider: str, symbol: str, start_ms: int
    ) -> list[RetainedSourceBar]:
        return self.ledger(account_id).bars_after(provider=provider, symbol=symbol, start_ms=start_ms)

    def close(self) -> None:
        with self._lock:
            for ledger in self._ledgers.values():
                ledger.close(checkpoint=False)
            self._ledgers.clear()


def _fill_event(client_order_id: str, *, at_ms: int, price: float, quantity: float) -> BrokerOrderEvent:
    return BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=at_ms,
        price=price,
        quantity=quantity,
        execution_id=f"shadow-execution:{client_order_id}",
    )


class ShadowOrderBook:
    """Synthesized orders for one shadow authority, settled on every read."""

    def __init__(
        self,
        *,
        ledger: SynthesizedOrderLedger,
        evidence: EvidenceLedgers,
        window: ExtendedHoursWindow | None,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._ledger = ledger
        self._evidence = evidence
        self._window = window
        self._clock = clock

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar)

    def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            existing = self._ledger.find_order(records, client_order_id)
            if existing is not None:
                self._ledger.consume_bound_bar(client_order_id)
                return existing
            bound = self._ledger.consume_bound_bar(client_order_id)
            if bound is None:
                raise ShadowFillBindingError(
                    "Shadow execution requires the exact retained decision bar bound to this order; "
                    "nothing is ever priced from a later bar."
                )
            bar = self._ledger.verified_retained_bar(bound, symbol=leg.symbol)
            order, anchor = (
                self._resting_order(leg, client_order_id=client_order_id, bar=bar)
                if leg.extended_hours
                else self._immediate_order(leg, client_order_id=client_order_id, bar=bar)
            )
            self._ledger.append_locked(records, order=order, leg=leg, anchor=anchor)
            return order

    def cancel(self, order_id: str) -> None:
        """Mark a resting synthesized order cancelled; a terminal one is left alone. Never a vendor call."""
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            record = next(
                (
                    row
                    for row in self._ledger.latest_records_from(records).values()
                    if row.order.order_id == order_id
                ),
                None,
            )
            if record is None or record.order.status in _TERMINAL:
                return
            now = self._clock()
            self._ledger.append_locked(
                records,
                order=record.order.model_copy(
                    update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}
                ),
                leg=record.leg,
                anchor=record.anchor,
            )

    def settle(self) -> None:
        with self._ledger.transaction() as records:
            self._settle_locked(records)

    def orders(self) -> list[BrokerOrder]:
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            return self._ledger.latest_orders_from_records(records)

    def order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next((order for order in self.orders() if order.client_order_id == client_order_id), None)

    def record(self, client_order_id: str) -> SynthesizedOrderRecord | None:
        """The durable record — order, leg and fill anchor — for one client order id."""
        with self._ledger.transaction() as records:
            self._settle_locked(records)
            return self._ledger.find_record(records, client_order_id)

    def positions(self) -> list[BrokerPosition]:
        return synthesized_positions(SHADOW_BROKER_ID, self.orders())

    def close(self) -> None:
        self._evidence.close()

    def _immediate_order(
        self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar
    ) -> tuple[BrokerOrder, SynthesizedAnchor]:
        order = shape_immediate_order(
            leg,
            client_order_id=client_order_id,
            bar=bar,
            broker_id=SHADOW_BROKER_ID,
            id_prefix="shadow",
            observed_at_ms=self._clock(),
        )
        return order, SynthesizedAnchor(
            fill_model="decision_bar_close",
            evidence_account_id=bar.account_id,
            provider=bar.provider,
            bar_identity=bar.bar_identity,
            bar_ref=bar.bar_ref,
            decision_bar_start_ms=bar.start_ms,
            decision_bar_end_ms=bar.end_ms,
            fill_bar_ref=bar.bar_ref if order.status == "filled" else None,
        )

    def _resting_order(
        self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar
    ) -> tuple[BrokerOrder, SynthesizedAnchor]:
        if self._window is None:
            raise ShadowFillBindingError(
                "An extended-hours leg needs the broker's declared window to know when the vendor would cancel it."
            )
        bounds = declared_session_bounds(et_date_at_ms(bar.end_ms), self._window)
        if bounds is None:
            raise ShadowFillBindingError("The decision bar does not fall on a trading day.")
        at_ms = bar.end_ms
        order = BrokerOrder(
            broker=SHADOW_BROKER_ID,
            order_id=f"shadow-order:{client_order_id}",
            client_order_id=client_order_id,
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side,
            order_type=str(leg.order_type),
            time_in_force=str(leg.time_in_force),
            quantity=leg.quantity,
            filled_quantity=0.0,
            limit_price=leg.limit_price,
            stop_price=None,
            extended_hours=True,
            filled_avg_price=None,
            status=_RESTING,
            submitted_at_ms=at_ms,
            created_at_ms=at_ms,
            updated_at_ms=at_ms,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=self._clock(),
        )
        return order, SynthesizedAnchor(
            fill_model="limit_touch",
            evidence_account_id=bar.account_id,
            provider=bar.provider,
            bar_identity=bar.bar_identity,
            bar_ref=bar.bar_ref,
            decision_bar_start_ms=bar.start_ms,
            decision_bar_end_ms=bar.end_ms,
            cancel_at_ms=bounds.close_ms,
        )

    def _settle_locked(self, records: list[SynthesizedOrderRecord]) -> None:
        """Resolve every resting order the retained evidence can now decide (D5.5)."""
        now_ms = self._clock()
        for record in list(self._ledger.latest_records_from(records).values()):
            anchor, leg = record.anchor, record.leg
            if (
                record.order.status != _RESTING
                or anchor is None
                or leg is None
                or anchor.fill_model != "limit_touch"
                or anchor.cancel_at_ms is None
            ):
                continue
            client_order_id = record.order.client_order_id or record.order.order_id
            bars = self._evidence.bars_after(
                anchor.evidence_account_id,
                provider=anchor.provider,
                symbol=record.order.symbol,
                start_ms=anchor.decision_bar_end_ms,
            )
            fill = limit_touch_fill(
                leg,
                decision_bar_end_ms=anchor.decision_bar_end_ms,
                bars=bars,
                cancel_at_ms=anchor.cancel_at_ms,
            )
            if fill is not None:
                self._ledger.append_locked(
                    records,
                    order=record.order.model_copy(
                        update={
                            "status": "filled",
                            "filled_quantity": leg.quantity,
                            "filled_avg_price": float(fill.price),
                            "filled_at_ms": fill.filled_at_ms,
                            "updated_at_ms": fill.filled_at_ms,
                            "observed_at_ms": now_ms,
                            "events": [
                                _fill_event(
                                    client_order_id,
                                    at_ms=fill.filled_at_ms,
                                    price=float(fill.price),
                                    quantity=leg.quantity,
                                )
                            ],
                        }
                    ),
                    leg=leg,
                    anchor=anchor.model_copy(update={"fill_bar_ref": fill.bar_ref}),
                )
                continue
            bucket_ms = anchor.decision_bar_end_ms - anchor.decision_bar_start_ms
            if now_ms >= anchor.cancel_at_ms + bucket_ms:
                self._ledger.append_locked(
                    records,
                    order=record.order.model_copy(
                        update={
                            "status": "canceled",
                            "canceled_at_ms": anchor.cancel_at_ms,
                            "updated_at_ms": anchor.cancel_at_ms,
                            "observed_at_ms": now_ms,
                        }
                    ),
                    leg=leg,
                    anchor=anchor,
                )


class NoSubmitAlpacaTradePort:
    """``BrokerTradePort`` that synthesizes every order and never reaches Alpaca (ADR 0059 D2)."""

    broker_id = SHADOW_BROKER_ID

    def __init__(self, book: ShadowOrderBook) -> None:
        self._book = book

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        self._book.bind_evaluated_bar(client_order_id, retained_bar)

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return self._book.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        self._book.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._book.order_by_client_order_id(client_order_id)


class ShadowAccountReadPort:
    """``BrokerReadPort``: live account truth, synthesized custody (ruling R3)."""

    broker_id = SHADOW_BROKER_ID

    def __init__(self, *, live: BrokerReadPort, book: ShadowOrderBook) -> None:
        self._live = live
        self._book = book

    def capabilities(self) -> BrokerCapabilities:
        return self._live.capabilities()

    async def get_account(self) -> BrokerAccountSnapshot:
        return await self._live.get_account()

    async def list_positions(self) -> list[BrokerPosition]:
        return self._book.positions()

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        return filter_synthesized_orders(self._book.orders(), status=status, limit=limit, after_ms=after_ms)

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        return await self._live.list_activities(after_ms=after_ms, limit=limit)

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        return await self._live.list_assets(status=status, limit=limit)

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        return await self._live.get_asset(symbol)

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        return await self._live.get_clock_evidence()

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        return await self._live.get_portfolio_history(history_range)


@dataclass(frozen=True)
class ShadowPorts:
    read: ShadowAccountReadPort
    trade: NoSubmitAlpacaTradePort
    book: ShadowOrderBook
    account_id: str


def compose_shadow_ports(
    *,
    live_read: BrokerReadPort,
    live_account_id: str,
    artifacts_root: Path,
    clock: Clock = now_ms_utc,
) -> ShadowPorts:
    """Bind one live read port into the shadow world for ``live_account_id``.

    The order WAL lives in the shadow custody directory
    (``accounts/alpaca/shadow:<live_account_id>/``, the same isolation the
    ``sim:`` namespace gets); settlement reads the per-instance
    ``shadow-evidence:`` ledgers.
    """
    account_id = shadow_account_id_for_live_account(live_account_id)
    _accounts_root, account_dir = account_paths(artifacts_root, account_id)
    account_dir.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceLedgers(artifacts_root)
    ledger = SynthesizedOrderLedger(
        account_id=account_id,
        path=confined_account_file(artifacts_root, account_id, SYNTHESIZED_ORDER_LEDGER_FILENAME),
        trusted_root=account_dir,
        verify_bar=evidence.verify,
        label="shadow_order",
    )
    book = ShadowOrderBook(
        ledger=ledger,
        evidence=evidence,
        window=live_read.capabilities().extended_hours_window,
        clock=clock,
    )
    return ShadowPorts(
        read=ShadowAccountReadPort(live=live_read, book=book),
        trade=NoSubmitAlpacaTradePort(book),
        book=book,
        account_id=account_id,
    )


def _clerk_minted(client_order_id: str | None) -> bool:
    if not client_order_id:
        return False
    try:
        parse_order_ref(client_order_id)
    except OrderRefParseError:
        return False
    return True


async def verify_shadow_namespace_empty(read: BrokerReadPort) -> None:
    """ADR 0002 invariant 1, transferred: the live account holds no Clerk-minted order, ever.

    Bounded by what the read port can see — the newest page of the whole
    history plus every open order. A full page proves nothing and refuses
    (``SHADOW_NAMESPACE_UNPROVEN``), the same posture the sweep takes at its
    own 500-row boundary; any Clerk-minted ``client_order_id`` is poisoned
    state (``SHADOW_NAMESPACE_POISONED``).
    """
    history = await read.list_orders(status="all", limit=MAX_OPEN_ORDER_SNAPSHOT)
    if len(history) >= MAX_OPEN_ORDER_SNAPSHOT:
        raise ShadowNamespaceUnproven(
            f"the live account's order history reached the {MAX_OPEN_ORDER_SNAPSHOT}-row page "
            "boundary; an empty Clerk namespace cannot be proven from one page"
        )
    open_orders = await read.list_orders(status="open", limit=MAX_OPEN_ORDER_SNAPSHOT)
    owned = sorted(
        {order.order_id for order in (*history, *open_orders) if _clerk_minted(order.client_order_id)}
    )
    if owned:
        raise ShadowNamespacePoisoned(owned)


__all__ = [
    "SHADOW_BROKER_ID",
    "EvidenceLedgers",
    "NoSubmitAlpacaTradePort",
    "ShadowAccountReadPort",
    "ShadowFillBindingError",
    "ShadowNamespacePoisoned",
    "ShadowNamespaceUnproven",
    "ShadowOrderBook",
    "ShadowPorts",
    "compose_shadow_ports",
    "verify_shadow_namespace_empty",
]
```

Implementation notes for the implementer: (a) `parse_order_ref` also accepts the manual namespace (`build_manual_order_namespace`), which is correct — any Clerk-minted ref on the live account is poison; (b) grep `app/broker/alpaca/clerk/` and `app/services/` for `broker_id ==` before finishing and report any consumer that keys on a **read port's** `broker_id` (the facade's own `broker_id` stays `"alpaca"`); (c) the book's file I/O is synchronous inside `async` port methods exactly as the sim broker's is.

- [ ] **Step 7: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_broker.py tests/broker/alpaca/clerk/test_account_worlds.py tests/broker/alpaca/clerk/test_synthesized_orders.py tests/services/test_source_bar_ledger.py tests/broker/alpaca/clerk/test_synthetic_broker_limit_legs.py tests/broker/alpaca/clerk/test_fill_models.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
Expected: all pass, ruff clean.

- [ ] **Step 8: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/shadow_broker.py PythonDataService/app/broker/alpaca/clerk/account_authority.py PythonDataService/app/broker/alpaca/clerk/synthetic_broker.py PythonDataService/app/services/source_bar_ledger.py PythonDataService/tests/broker/alpaca/clerk/test_shadow_broker.py PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py && git commit -q -m "feat(clerk): the shadow ports — live reads, synthesized fills, no submission (ADR 0059 D2)

NoSubmitAlpacaTradePort holds no vendor client: a regular-session leg fills
at its decision bar's close, an extended-session limit leg rests and settles
on read under limit_touch (D5.5) against the instance's own retained bars,
cancelling one bucket after the declared close. The read port serves live
account truth and synthesized custody so the sweep reconciles the world the
shadow Clerk holds. Cold start refuses a live account holding Clerk-minted
orders (ADR 0002 invariant 1).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 4: The shadow session journal — per-ET-day sweep cleanliness

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/shadow_sessions.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_shadow_sessions.py`

**Interfaces:**
- Consumes: `sqlite/reconcile.AccountReconciliationResult` (`verdict`), `sqlite/writes.confined_account_file`, `account_authority.require_shadow_account_id`, `trading_calendar.is_trading_day / session_open_ms_utc / session_close_ms_utc` (sealed; import only), `session_authority.declared_session_bounds`, `session_anchors.et_date_at_ms`.
- Produces: `SHADOW_SESSIONS_FILENAME = "shadow_sessions.jsonl"`, `ShadowSessionRow(seq, kind, session_open_ms, observed_at_ms, verdict)`, `ShadowDayState(opened_at_ms, closed_clean, non_clean_verdicts)`, `ShadowSessionLedger(artifacts_root=, account_id=)` with `path`, `rows()`, `append(*, kind, session_open_ms, observed_at_ms, verdict)`, `day_state(session_open_ms)`, `completed_session_opens()`, `has_rows()`; `ShadowSessionRecorder(ledger=, window=, clock=)` with `record(result) -> result` (a `ReconciliationListener`).
- Consumed by Task 5 (wired as the sweep's listener) and Task 7 (the gate reads `day_state` / `completed_session_opens`).

- [ ] **Step 1: Write the failing test**

`PythonDataService/tests/broker/alpaca/clerk/test_shadow_sessions.py`:

```python
"""One shadow authority's per-ET-day cleanliness journal (ADR 0059 D2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.shadow_sessions import (
    SHADOW_SESSIONS_FILENAME,
    ShadowSessionLedger,
    ShadowSessionRecorder,
)
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.services.session_authority import declared_session_bounds, et_minute_of_day_ms

DAY = date(2026, 9, 8)
SATURDAY = date(2026, 9, 12)
ACCOUNT = "shadow:9LIVE0001"


class _Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def _recorder(tmp_path: Path, clock: _Clock) -> tuple[ShadowSessionLedger, ShadowSessionRecorder]:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    return ledger, ShadowSessionRecorder(ledger=ledger, window=ALPACA_EXTENDED_HOURS_WINDOW, clock=clock)


def test_ledger_is_shadow_scoped_and_lives_in_the_custody_directory(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    assert ledger.path == tmp_path / "accounts" / "alpaca" / ACCOUNT / SHADOW_SESSIONS_FILENAME
    assert ledger.has_rows() is False
    with pytest.raises(ValueError, match="shadow: account identity"):
        ShadowSessionLedger(artifacts_root=tmp_path, account_id="PA0SANITIZED00001")


def test_a_clean_day_opens_then_closes_clean_only_after_the_declared_close(tmp_path: Path) -> None:
    clock = _Clock(et_minute_of_day_ms(DAY, 300))  # 05:00 ET
    ledger, recorder = _recorder(tmp_path, clock)
    clean = AccountReconciliationResult(verdict="clean")
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None
    open_ms = session_open_ms_utc(DAY)

    assert recorder.record(clean) is clean
    state = ledger.day_state(open_ms)
    assert (state.opened_at_ms, state.closed_clean, state.non_clean_verdicts) == (clock.now_ms, False, ())

    clock.now_ms = bounds.close_ms - 1
    recorder.record(clean)
    assert ledger.day_state(open_ms).closed_clean is False

    clock.now_ms = bounds.close_ms
    recorder.record(clean)
    recorder.record(clean)  # idempotent: one closed-clean row
    assert ledger.day_state(open_ms).closed_clean is True
    assert [row.kind for row in ledger.rows()] == ["day_opened", "session_closed_clean"]
    assert ledger.completed_session_opens() == (open_ms,)


def test_a_non_clean_pass_taints_the_day_and_is_deduplicated_per_verdict(tmp_path: Path) -> None:
    clock = _Clock(et_minute_of_day_ms(DAY, 720))
    ledger, recorder = _recorder(tmp_path, clock)
    open_ms = session_open_ms_utc(DAY)
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None

    recorder.record(AccountReconciliationResult(verdict="stale"))
    recorder.record(AccountReconciliationResult(verdict="stale"))
    recorder.record(AccountReconciliationResult(verdict="position_drift", drifted_symbols=("SPY",)))
    clock.now_ms = bounds.close_ms
    recorder.record(AccountReconciliationResult(verdict="clean"))

    state = ledger.day_state(open_ms)
    assert state.non_clean_verdicts == ("stale", "position_drift")
    assert state.closed_clean is True
    assert ledger.completed_session_opens() == ()
    assert [row.kind for row in ledger.rows()] == ["day_opened", "non_clean", "non_clean", "session_closed_clean"]


def test_passes_outside_a_trading_day_write_nothing(tmp_path: Path) -> None:
    clock = _Clock(et_minute_of_day_ms(SATURDAY, 720))
    ledger, recorder = _recorder(tmp_path, clock)
    recorder.record(AccountReconciliationResult(verdict="clean"))
    assert ledger.rows() == []


def test_a_regular_hours_window_closes_at_the_calendar_close(tmp_path: Path) -> None:
    from app.lean_sidecar.trading_calendar import session_close_ms_utc

    clock = _Clock(et_minute_of_day_ms(DAY, 600))
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    recorder = ShadowSessionRecorder(ledger=ledger, window=None, clock=clock)
    recorder.record(AccountReconciliationResult(verdict="clean"))
    clock.now_ms = session_close_ms_utc(DAY)
    recorder.record(AccountReconciliationResult(verdict="clean"))
    assert ledger.day_state(session_open_ms_utc(DAY)).closed_clean is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_sessions.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: app.broker.alpaca.clerk.shadow_sessions`.

- [ ] **Step 3: Create `shadow_sessions.py`**

```python
"""One shadow authority's per-ET-day sweep cleanliness journal (ADR 0059 D2).

A shadow session counts only when the sweep reconciled cleanly for the whole
day. The sweep publishes a verdict every pass; this module keeps the tiny,
durable, append-only record the gate needs instead of journaling every pass:

* ``day_opened`` — the first pass observed on ET trading date D, with the
  instant it happened (the gate checks that instant preceded the instance's
  session open; a process that started at noon cannot vouch for the morning).
* ``non_clean`` — a pass on D whose verdict was not ``clean``, deduplicated
  while consecutive verdicts repeat.
* ``session_closed_clean`` — the first clean pass on D at or after the
  declared window's close (the calendar's close when there is no window).

A day is complete iff it was opened, closed clean, and never non-clean.
Dates are stored as their calendar session open in ``int64 ms UTC``
(temporal-rigor: a trading date is one ET-anchored instant, never a string).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.account_authority import require_shadow_account_id
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.services.jsonl_wal import JsonlWal
from app.services.session_authority import declared_session_bounds
from app.utils.advisory_lock import advisory_file_lock
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

SHADOW_SESSIONS_FILENAME = "shadow_sessions.jsonl"
RowKind = Literal["day_opened", "non_clean", "session_closed_clean"]


class ShadowSessionRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    kind: RowKind
    session_open_ms: int = Field(ge=0)
    observed_at_ms: int = Field(ge=0)
    verdict: str


@dataclass(frozen=True)
class ShadowDayState:
    opened_at_ms: int | None
    closed_clean: bool
    non_clean_verdicts: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.opened_at_ms is not None and self.closed_clean and not self.non_clean_verdicts


def _corrupt(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Shadow session journal corrupt at {path}: {detail}")


class ShadowSessionLedger:
    """Append-only journal beside one shadow authority's custody database."""

    def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
        self.account_id = require_shadow_account_id(account_id)
        path = confined_account_file(artifacts_root, self.account_id, SHADOW_SESSIONS_FILENAME)
        self._rows: JsonlWal[ShadowSessionRow] = JsonlWal(
            path,
            record_model=ShadowSessionRow,
            corrupt_error=_corrupt,
            seq_of=lambda row: row.seq,
            label="shadow_session",
            trusted_root=path.parent,
        )

    @property
    def path(self) -> Path:
        return self._rows.path

    def rows(self) -> list[ShadowSessionRow]:
        return self._rows.read_all() if self.path.exists() else []

    def has_rows(self) -> bool:
        return bool(self.rows())

    def append(self, *, kind: RowKind, session_open_ms: int, observed_at_ms: int, verdict: str) -> ShadowSessionRow:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(self.path):
            rows = self.rows()
            self._rows._next_seq = rows[-1].seq + 1 if rows else 1
            row = ShadowSessionRow(
                seq=self._rows._next_seq,
                kind=kind,
                session_open_ms=session_open_ms,
                observed_at_ms=observed_at_ms,
                verdict=verdict,
            )
            self._rows.append(row)
            return row

    def day_state(self, session_open_ms: int) -> ShadowDayState:
        rows = [row for row in self.rows() if row.session_open_ms == session_open_ms]
        opened = next((row.observed_at_ms for row in rows if row.kind == "day_opened"), None)
        return ShadowDayState(
            opened_at_ms=opened,
            closed_clean=any(row.kind == "session_closed_clean" for row in rows),
            non_clean_verdicts=tuple(row.verdict for row in rows if row.kind == "non_clean"),
        )

    def completed_session_opens(self) -> tuple[int, ...]:
        opens = sorted({row.session_open_ms for row in self.rows()})
        return tuple(open_ms for open_ms in opens if self.day_state(open_ms).complete)


class ShadowSessionRecorder:
    """The sweep listener that journals each trading day's cleanliness."""

    def __init__(
        self,
        *,
        ledger: ShadowSessionLedger,
        window: ExtendedHoursWindow | None,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._ledger = ledger
        self._window = window
        self._clock = clock

    def record(self, result: AccountReconciliationResult) -> AccountReconciliationResult:
        now_ms = self._clock()
        day = et_date_at_ms(now_ms)
        if not is_trading_day(day):
            return result
        open_ms = session_open_ms_utc(day)
        state = self._ledger.day_state(open_ms)
        if state.opened_at_ms is None:
            self._ledger.append(kind="day_opened", session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict)
        if result.verdict != "clean":
            last = self._last_row_for(open_ms)
            if last is None or last.kind != "non_clean" or last.verdict != result.verdict:
                self._ledger.append(kind="non_clean", session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict)
            return result
        if now_ms >= self._close_ms(day) and not state.closed_clean:
            self._ledger.append(kind="session_closed_clean", session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict)
        return result

    def _close_ms(self, day: object) -> int:
        from datetime import date

        assert isinstance(day, date)
        if self._window is None:
            return session_close_ms_utc(day)
        bounds = declared_session_bounds(day, self._window)
        assert bounds is not None  # a trading day by construction
        return bounds.close_ms

    def _last_row_for(self, session_open_ms: int) -> ShadowSessionRow | None:
        return next(
            (row for row in reversed(self._ledger.rows()) if row.session_open_ms == session_open_ms),
            None,
        )


__all__ = [
    "SHADOW_SESSIONS_FILENAME",
    "ShadowDayState",
    "ShadowSessionLedger",
    "ShadowSessionRecorder",
    "ShadowSessionRow",
]
```

Type `_close_ms(self, day: date)` directly with `from datetime import date` at module top (the `object` + local import above is only to keep the listing self-contained; write it properly).

- [ ] **Step 4: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_sessions.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
Expected: all pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/shadow_sessions.py PythonDataService/tests/broker/alpaca/clerk/test_shadow_sessions.py && git commit -q -m "feat(clerk): a shadow authority journals each trading day's sweep cleanliness

Three row kinds (day opened, non-clean pass, session closed clean) are all the
shadow gate needs to know whether a day reconciled cleanly end to end (ADR
0059 D2). Dates are stored as their calendar session open in int64 ms UTC.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 5: A live boot composes the shadow authority; the Clerk runs on it

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py` (shadow selection; shared repository composition; `activate_shadow_clerk_authority`; `get_alpaca_clerk` admits shadow)
- Modify: `PythonDataService/app/broker/alpaca/clerk/account_authority.py` (`bind_shadow_ports`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py` (`authority_kind` widens; the decision bar binds for every bar-bound authority)
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_protocol.py:46` (`Literal["sqlite", "synthetic", "shadow"]`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/trade_evidence.py` (`NullTradeUpdateEvidenceSink`)
- Modify: `PythonDataService/app/services/bot_trade_strategy.py:773-800` (`run_trade_bot` admits shadow)
- Modify: `PythonDataService/app/services/bot_binding_authority.py` (`PrimaryAccountBindingAuthority`; evidence namespace by custody world), `PythonDataService/app/services/bot_runner.py:301` (selector construction), `PythonDataService/app/services/run_replay_proof.py:841` (`ledger_account_id_for`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_active_authority.py` (new shadow tests), `tests/broker/alpaca/clerk/sqlite/test_runtime_shadow.py` (new), `tests/broker/alpaca/clerk/test_trade_evidence.py` (null sink), `tests/services/test_bot_binding_authority_source_bars.py` (rename + shadow namespace), `tests/services/test_run_replay_proof_service.py` (namespace for a shadow-sealed binding)

**Interfaces:**
- Consumes: Task 2 `ShadowActivationStore/Record/Invalid`; Task 3 `compose_shadow_ports`, `verify_shadow_namespace_empty`, `ShadowNamespacePoisoned/Unproven`, `ShadowOrderBook`, `evidence_account_id_for`; Task 4 `ShadowSessionLedger/Recorder`.
- Produces: `AuthorityKind = Literal["sqlite", "synthetic", "shadow", "unavailable"]`; `ActiveClerkRuntime._shadow_book` (closed by `close()`); `activate_shadow_clerk_authority(*, live_account_id, artifacts_root, activation_store=None) -> ShadowActivationRecord`; reason codes `SHADOW_NAMESPACE_POISONED`, `SHADOW_NAMESPACE_UNPROVEN`, `SHADOW_ACTIVATION_RECORD_INVALID`, `SHADOW_ACTIVATION_REQUIRED`, `SHADOW_CLERK_STARTUP_FAILED`; `account_authority.bind_shadow_ports(*, account_id, read, trade)`; `trade_evidence.NullTradeUpdateEvidenceSink`; `bot_binding_authority.PrimaryAccountBindingAuthority(binding, projector, external_start_guard, artifacts_root, custody_kind: Callable[[], AccountAuthorityKind])` and `primary_custody_kind()`.

- [ ] **Step 1: Write the failing tests**

Append to `PythonDataService/tests/broker/alpaca/clerk/test_active_authority.py` (reuse its `_Broker` double; add a live variant):

```python
from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES  # merge into the existing import block
from app.broker.alpaca.clerk.active_authority import activate_shadow_clerk_authority  # merge likewise
from app.broker.alpaca.clerk.trade_evidence import NullTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrder


class _LiveBroker(_Broker):
    """The live account: mode live, an empty order history, a trade port that must never be reached."""

    def __init__(self, history: list[BrokerOrder] | None = None) -> None:
        self.history = history or []

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        return _account().model_copy(update={"account_id": "9LIVE0001", "account_mode": "live"})

    async def list_orders(self, **_kwargs: Any) -> list:
        return self.history


def _vendor_order(client_order_id: str) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca", order_id=f"vendor-{client_order_id}", client_order_id=client_order_id, symbol="SPY",
        asset_class="us_equity", side="buy", order_type="market", time_in_force="day", quantity=1.0,
        filled_quantity=1.0, limit_price=None, stop_price=None, filled_avg_price=100.0, status="filled",
        submitted_at_ms=1, created_at_ms=1, updated_at_ms=1, filled_at_ms=1, canceled_at_ms=None,
        expired_at_ms=None, observed_at_ms=1,
    )


async def test_live_account_without_shadow_activation_is_refused_by_name(tmp_path: Path) -> None:
    runtime = await select_active_clerk_runtime(read=_LiveBroker(), trade=_LiveBroker(), artifacts_root=tmp_path)

    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "SHADOW_ACTIVATION_REQUIRED"
    assert runtime.startup_failure.account_id == "shadow:9LIVE0001"


async def test_live_account_holding_clerk_minted_orders_is_poisoned(tmp_path: Path) -> None:
    broker = _LiveBroker([_vendor_order("learn-ai/ema-1/v1:abc")])
    runtime = await select_active_clerk_runtime(read=broker, trade=broker, artifacts_root=tmp_path)

    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "SHADOW_NAMESPACE_POISONED"
    assert "vendor-learn-ai/ema-1/v1:abc" in runtime.startup_failure.recovery


async def test_activated_live_account_composes_the_shadow_authority(tmp_path: Path) -> None:
    record = await activate_shadow_clerk_authority(live_account_id="9LIVE0001", artifacts_root=tmp_path)
    assert record.account_id == "shadow:9LIVE0001"
    assert (await activate_shadow_clerk_authority(live_account_id="9LIVE0001", artifacts_root=tmp_path)) == record

    broker = _LiveBroker()
    runtime = await select_active_clerk_runtime(read=broker, trade=broker, artifacts_root=tmp_path)
    try:
        assert runtime.authority_kind == "shadow"
        assert runtime.selected_account_id == "shadow:9LIVE0001"
        assert runtime.selected_account_authority_kind == "shadow"
        assert runtime.clerk is not None and runtime.clerk.authority_kind == "shadow"
        assert runtime.clerk.account_mode == "live"
        assert runtime.sqlite_repository is not None
        assert isinstance(runtime.evidence_sink, NullTradeUpdateEvidenceSink)
        assert (await runtime.clerk.read_port.list_positions()) == []  # synthesized custody, not the live account's
    finally:
        await runtime.close()
```

(`runtime.clerk.read_port` — if the facade exposes its guarded read port under another name, use that; the assertion is that positions come from the book.)

Create `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime_shadow.py` (mirrors `test_runtime_program_leg.py`'s harness, so copy `_bar`, `_binding` from there with `ACCOUNT_ID = "shadow:9LIVE0001"` and the bar's `account_id = "shadow-evidence:spy-bot"`):

```python
"""The Clerk runs on the shadow authority: every synthesized fill is bound to the decision bar (ADR 0059 D2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.alpaca.clerk.test_shadow_broker import _LiveRead, _market_leg, _retain  # noqa: F401 — shared doubles

ACCOUNT_ID = "shadow:9LIVE0001"
SID = "spy-bot"
RUN_ID = "run-1"


def test_shadow_facade_requires_a_shadow_id_and_a_live_account(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        with pytest.raises(AccountAuthorityIdentityError, match="reads a live account"):
            SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(), authority_kind="shadow", account_mode="paper")
    finally:
        repo.close()
    paper = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    try:
        with pytest.raises(AccountAuthorityIdentityError, match="shadow: account identity"):
            SqliteAlpacaClerkFacade(repo=paper, read=_FakeReadPort(), trade=_FakeTradePort(), authority_kind="shadow", account_mode="live")
    finally:
        paper.close()


async def test_enter_on_the_shadow_authority_is_synthesized_from_the_bound_decision_bar(tmp_path: Path) -> None:
    from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import _binding

    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=600, close="100.25")
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=ports.read, trade=ports.trade, authority_kind="shadow", account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    try:
        unproven = await facade.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN_ID, decision_id="decision-0", purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan, quantity=binding.quantity, use_rth=True, retained_source_bar=None,
        )
        assert unproven.state == "rejected" and "SIMULATED_SOURCE_BAR_UNPROVEN" in unproven.explanation

        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN_ID, decision_id="decision-1", purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan, quantity=binding.quantity, use_rth=True, retained_source_bar=decision,
        )
        assert receipt.state == "succeeded", receipt.explanation
        [order] = await ports.read.list_orders()
        assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 100.25, decision.end_ms)
        assert order.client_order_id is not None and order.client_order_id.startswith("learn-ai/spy-bot/v1:")
        assert [position.symbol for position in await ports.read.list_positions()] == ["SPY"]
    finally:
        repo.close()
        ports.book.close()
```

If `execute_for_instance` needs the ENTER's decision evidence or liveness inputs beyond what `test_runtime_program_leg._enter` passes, copy exactly what that helper passes; the two assertions above are the contract.

Append to `tests/broker/alpaca/clerk/test_trade_evidence.py`:

```python
async def test_null_sink_records_nothing_and_answers_order_event() -> None:
    from app.broker.alpaca.clerk.trade_evidence import NullTradeUpdateEvidenceSink

    sink = NullTradeUpdateEvidenceSink()
    read = object()
    assert sink.guard_reconnect_read(read) is read  # type: ignore[arg-type]
    disposition = await sink.record_lifecycle_event(
        client_order_id="x", event=BrokerOrderEvent(event_type="fill", occurred_at_ms=1, price=1.0, quantity=1.0),
        event_key="k", order=None, recovery_source=None, recovery_window_limit=None,
    )
    assert disposition == "order_event"
    assert await sink.reconcile_gap() is None
```

Update `tests/services/test_bot_binding_authority_source_bars.py`: import `PrimaryAccountBindingAuthority` (renamed), pass `custody_kind=lambda: "real_paper"` in the existing construction, and add:

```python
def test_primary_authority_on_the_shadow_world_opens_the_shadow_evidence_ledger(tmp_path: Path) -> None:
    authority = PrimaryAccountBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
        custody_kind=lambda: "shadow",
    )
    assert authority.source_bars().account_id == "shadow-evidence:bot-a"
```

Add to `tests/services/test_run_replay_proof_service.py` one test that `ledger_account_id_for(binding)` returns `shadow-evidence:<sid>` for a trade binding whose `sealed_account_id` is `shadow:9LIVE0001`, `paper:<sid>` for one sealed to `PA-TEST`, and still 404s for `log_only`.

- [ ] **Step 2: Run them to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_active_authority.py tests/broker/alpaca/clerk/sqlite/test_runtime_shadow.py tests/broker/alpaca/clerk/test_trade_evidence.py tests/services/test_bot_binding_authority_source_bars.py tests/services/test_run_replay_proof_service.py -q -p no:cacheprovider`
Expected: FAIL on the new names.

- [ ] **Step 3: `account_authority.bind_shadow_ports`**

```python
def bind_shadow_ports(
    *,
    account_id: str,
    read: BrokerReadPort,
    trade: BrokerTradePort,
) -> AccountBoundBrokerPorts:
    """Create a shadow composition only for the reserved namespace (ADR 0059 D2)."""
    return AccountBoundBrokerPorts(
        account_id=require_shadow_account_id(account_id),
        authority_kind="shadow",
        read=read,
        trade=trade,
    )
```

- [ ] **Step 4: `trade_evidence.NullTradeUpdateEvidenceSink`**

```python
class NullTradeUpdateEvidenceSink:
    """The shadow authority's sink: the live execution stream is not shadow custody evidence.

    A shadow authority submits nothing, so no trade update can ever concern
    its custody; a real order on the live account is that account's business,
    not the shadow journal's (ADR 0042 isolation). The consumer still runs —
    its health is what proves the live websocket authenticates — so this
    sink answers every frame without folding it.
    """

    def guard_reconnect_read(self, read: BrokerReadPort) -> BrokerReadPort:
        return read

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None,
        recovery_source: str | None,
        recovery_window_limit: int | None,
    ) -> TradeUpdateDisposition:
        del order, recovery_source, recovery_window_limit
        logger.info(
            "shadow authority ignored a live trade update",
            extra={
                "action": "shadow_trade_update_ignored",
                "client_order_id": client_order_id,
                "event_type": event.event_type,
                "event_key": event_key,
            },
        )
        return "order_event"

    async def reconcile_gap(self) -> None:
        return None
```

- [ ] **Step 5: `runtime.py` — the facade admits shadow**

Add a module constant beside `_ENCODED_DECISION_PREFIX`: `_BAR_BOUND_AUTHORITIES: Final = frozenset({"synthetic", "shadow"})`. Change the class annotation and constructor:

```python
    authority_kind: Literal["sqlite", "synthetic", "shadow"]
    ...
        authority_kind: Literal["sqlite", "synthetic", "shadow"] = "sqlite",
        account_mode: Literal["paper", "live"],
        ...
    ) -> None:
        if authority_kind == "synthetic":
            require_synthetic_account_id(repo.account_id)
            if account_mode != "paper":
                raise AccountAuthorityIdentityError("a synthetic authority is a paper environment")
        elif authority_kind == "shadow":
            require_shadow_account_id(repo.account_id)
            if account_mode != "live":
                raise AccountAuthorityIdentityError("a shadow authority reads a live account")
        else:
            require_real_account_id(repo.account_id)
```

Add the property `binds_decision_bar` (`return self.authority_kind in _BAR_BOUND_AUTHORITIES`) and use it at both former `== "synthetic"` sites (`:675` and `:889`); change the `:675` explanation to `"Synthesized custody received no exact retained source bar for this decision."` (reason code unchanged). Import `require_shadow_account_id`.

- [ ] **Step 6: `active_authority.py` — shadow selection with a shared composition tail**

Change `AuthorityKind = Literal["sqlite", "synthetic", "shadow", "unavailable"]` and add `_REPOSITORY_BACKED: frozenset[str] = frozenset({"sqlite", "synthetic", "shadow"})`, `_ACCOUNT_KIND_BY_AUTHORITY: dict[str, AccountAuthorityKind] = {"sqlite": "real_paper", "synthetic": "synthetic", "shadow": "shadow"}`. In `ActiveClerkRuntime`: `sqlite_repository` uses `_REPOSITORY_BACKED`; add `_shadow_book: ShadowOrderBook | None = None`; `close()` calls `self._shadow_book.close()` after draining effects and before closing the repository; `selected_account_authority_kind` falls back to `_ACCOUNT_KIND_BY_AUTHORITY.get(self.authority_kind)`.

Extract the paper branch's tail (from `repository = await _open_repository_after_lease_expiry(...)` through `await asyncio.wait_for(facade.recover(), ...)`) into:

```python
@dataclass(frozen=True)
class _ComposedAuthority:
    repository: ClerkSqliteRepository
    facade: SqliteAlpacaClerkFacade
    sweep: ReconciliationSweep
    hold_sync: StreamHealthHoldSync


async def _compose_repository_runtime(
    *,
    ports: AccountBoundBrokerPorts,
    authority_kind: Literal["sqlite", "shadow"],
    account_mode: Literal["paper", "live"],
    artifacts_root: Path,
    verify_activation: Callable[[ControlMetaSnapshot], None],
    repository_opener: Callable[[str, Path], ClerkSqliteRepository],
    startup_recovery_timeout_s: float,
    execution_lease_wait_timeout_s: float,
    execution_lease_retry_interval_s: float,
    stream_health_gate: StreamHealthGate | None,
    roster_symbols: Callable[[], Sequence[str]] | None,
    sweep_listener: ReconciliationListener | None = None,
) -> _ComposedAuthority:
    """Open the account's repository and stand up its Clerk, sweep and hold sync.

    Shared by the real-paper and shadow authorities; on any failure every
    handle opened here is closed before the exception propagates, so the
    caller only maps it to a startup refusal.
    """
    repository: ClerkSqliteRepository | None = None
    sweep: ReconciliationSweep | None = None
    hold_sync: StreamHealthHoldSync | None = None
    try:
        repository = await _open_repository_after_lease_expiry(
            repository_opener,
            account_id=ports.account_id,
            artifacts_root=artifacts_root,
            wait_timeout_s=execution_lease_wait_timeout_s,
            retry_interval_s=execution_lease_retry_interval_s,
        )
        verify_activation(repository.control_meta_snapshot())
        intake = ReentrantAsyncLock()
        guarded_read, guarded_trade = guard_broker_ports(read=ports.read, trade=ports.trade, intake=intake)
        facade = SqliteAlpacaClerkFacade(
            repo=repository,
            read=guarded_read,
            trade=guarded_trade,
            stream_health=stream_health_gate,
            intake=intake,
            authority_kind=authority_kind,
            account_mode=account_mode,
            program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
        )
        publish = facade.publish_sweep_reconciliation
        on_result: ReconciliationListener = (
            publish if sweep_listener is None else (lambda result: sweep_listener(publish(result)))
        )
        sweep = ReconciliationSweep(
            repo=repository,
            read=guarded_read,
            trade=guarded_trade,
            intake=intake,
            on_result=on_result,
            after_pass=(
                SymbolValidityProbe(
                    store=SymbolValidityStore(artifacts_root),
                    read=guarded_read,
                    roster_symbols=roster_symbols,
                ).run_due
                if roster_symbols is not None
                else None
            ),
        )
        sweep.start_lease_heartbeat()
        hold_sync = StreamHealthHoldSync(repo=repository, gate=stream_health_gate)
        await asyncio.wait_for(facade.recover(), timeout=startup_recovery_timeout_s)
        return _ComposedAuthority(repository=repository, facade=facade, sweep=sweep, hold_sync=hold_sync)
    except Exception:
        if hold_sync is not None:
            await hold_sync.stop()
        if sweep is not None:
            await sweep.stop()
        if repository is not None:
            repository.close()
        raise
```

Keep every existing comment from the paper branch on the lines it explains (the sweep/hold-sync comments move with the code). The paper branch becomes: build `verify_activation` as

```python
    def _verify_paper_activation(meta: ControlMetaSnapshot) -> None:
        if store.resolve(account.account_id, meta.authority_generation, meta.db_identity_token, artifacts_root) is None:
            raise ActivationRecordInvalid("activation record disappeared during SQLite startup")
```

then `composed = await _compose_repository_runtime(ports=ports, authority_kind="sqlite", account_mode=account.account_mode, ..., verify_activation=_verify_paper_activation)` inside the existing `try/except Exception` that maps to `ACTIVATION_RECORD_INVALID` / `SQLITE_CLERK_STARTUP_FAILED` (unchanged mapping, unchanged log), and the return builds the runtime from `composed.*` exactly as today. `ReconciliationListener` is imported from `reconciliation_sweep`; `ControlMetaSnapshot` from `sqlite/models.py`.

Insert the shadow branch right after the account read, replacing the `LIVE_ACCOUNT_REFUSED` block:

```python
    if account.account_mode == "live":
        return await _select_shadow_clerk_runtime(
            account=account,
            read=read,
            artifacts_root=artifacts_root,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
        )
```

```python
async def _select_shadow_clerk_runtime(
    *,
    account: BrokerAccountSnapshot,
    read: BrokerReadPort,
    artifacts_root: Path,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository],
    startup_recovery_timeout_s: float,
    execution_lease_wait_timeout_s: float,
    execution_lease_retry_interval_s: float,
    stream_health_gate: StreamHealthGate | None,
    roster_symbols: Callable[[], Sequence[str]] | None,
) -> ActiveClerkRuntime:
    """Compose the Shadow Account Authority for a live account (ADR 0059 D2).

    The live trade port is never bound: the shadow world's trade port is
    ``NoSubmitAlpacaTradePort``. Real-money custody stays unconstructible
    until slice 7 admits an armed instance.
    """
    try:
        await verify_shadow_namespace_empty(read)
    except (ShadowNamespacePoisoned, ShadowNamespaceUnproven) as exc:
        logger.warning(
            "live account refused for shadow: order namespace not proven empty",
            extra={"action": "shadow_namespace_refused", "reason_code": exc.reason_code},
        )
        return _unavailable(exc.reason_code, account_id=account.account_id, recovery=str(exc))
    except BrokerError as exc:
        return _unavailable(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=account.account_id,
            recovery=f"Restore the live account's order-history read: {exc}",
        )
    shadow = compose_shadow_ports(live_read=read, live_account_id=account.account_id, artifacts_root=artifacts_root)
    ports = bind_shadow_ports(account_id=shadow.account_id, read=shadow.read, trade=shadow.trade)
    store = ShadowActivationStore(artifacts_root)
    try:
        activation = store.latest(shadow.account_id)
    except ShadowActivationInvalid as exc:
        shadow.book.close()
        return _unavailable(
            "SHADOW_ACTIVATION_RECORD_INVALID",
            account_id=shadow.account_id,
            recovery=str(exc),
            activation_detected=True,
        )
    if activation is None:
        shadow.book.close()
        return _unavailable(
            "SHADOW_ACTIVATION_REQUIRED",
            account_id=shadow.account_id,
            recovery=(
                "Explicitly activate the shadow authority for this live account "
                "(scripts.manage_alpaca_shadow activate) before starting shadow custody."
            ),
        )

    def _verify_shadow_activation(meta: ControlMetaSnapshot) -> None:
        if (
            meta.authority_generation != activation.authority_generation
            or meta.db_identity_token != activation.db_identity_token
        ):
            raise ShadowActivationInvalid("shadow activation does not match repository identity")

    sessions = ShadowSessionRecorder(
        ledger=ShadowSessionLedger(artifacts_root=artifacts_root, account_id=shadow.account_id),
        window=read.capabilities().extended_hours_window,
    )
    try:
        composed = await _compose_repository_runtime(
            ports=ports,
            authority_kind="shadow",
            account_mode=account.account_mode,
            artifacts_root=artifacts_root,
            verify_activation=_verify_shadow_activation,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
            sweep_listener=sessions.record,
        )
    except Exception as exc:
        shadow.book.close()
        logger.warning(
            "Shadow Alpaca Clerk failed startup; no authority installed",
            extra={"action": "shadow_active_clerk_startup_failed", "account_id": shadow.account_id},
            exc_info=True,
        )
        return _unavailable(
            "SHADOW_ACTIVATION_RECORD_INVALID" if isinstance(exc, ShadowActivationInvalid) else "SHADOW_CLERK_STARTUP_FAILED",
            account_id=shadow.account_id,
            recovery=str(exc),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )
    return ActiveClerkRuntime(
        authority_kind="shadow",
        clerk=composed.facade,
        sweep=composed.sweep,
        hold_sync=composed.hold_sync,
        evidence_sink=NullTradeUpdateEvidenceSink(),
        _sqlite_repository=composed.repository,
        account_id=shadow.account_id,
        account_authority_kind="shadow",
        _shadow_book=shadow.book,
    )
```

`BrokerError` is `app.broker.contract.errors.BrokerError`. Generalise activation into one helper used by both isolated worlds:

```python
async def _activate_isolated_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    store: IsolatedActivationStore,
) -> IsolatedActivationRecord:
    """Initialize (or reopen) one isolated repository and durably activate it exactly once."""
    try:
        repository = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=artifacts_root)
    except AlreadyInitialized:
        # A process can crash after durable repository initialization but before
        # activation-record append. A later explicit activation must complete
        # that same repository fence rather than silently selecting it at boot.
        repository = ClerkSqliteRepository.open(account_id=account_id, artifacts_root=artifacts_root)
    try:
        meta = repository.control_meta_snapshot()
        prior = store.latest(account_id)
        if prior is not None:
            if (
                prior.authority_generation == meta.authority_generation
                and prior.db_identity_token == meta.db_identity_token
            ):
                # Reusing this exact proof is safe; appending it again would
                # violate the activation ledger's monotonic generation fence.
                return prior
            raise store.record_type.invalid_error(
                f"{store.record_type.label} does not match repository identity"
            )
        record = store.record_type.create(
            account_id=account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            activated_at_ms=now_ms_utc(),
        )
        store.append(record)
        return record
    finally:
        repository.close()


async def activate_synthetic_clerk_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    activation_store: SyntheticActivationStore | None = None,
) -> SyntheticActivationRecord:
    """Explicitly initialize and durably activate one isolated ``sim:`` account. (docstring unchanged)"""
    require_synthetic_account_id(account_id)
    record = await _activate_isolated_authority(
        account_id=account_id,
        artifacts_root=artifacts_root,
        store=activation_store or SyntheticActivationStore(artifacts_root),
    )
    assert isinstance(record, SyntheticActivationRecord)
    return record


async def activate_shadow_clerk_authority(
    *,
    live_account_id: str,
    artifacts_root: Path,
    activation_store: ShadowActivationStore | None = None,
) -> ShadowActivationRecord:
    """Explicitly initialize and durably activate the shadow authority for one live account.

    No startup path calls this; the operator does, once, through
    ``scripts.manage_alpaca_shadow activate``. The custody database it creates
    is the shadow world's own — the live account's authority is untouched.
    """
    record = await _activate_isolated_authority(
        account_id=shadow_account_id_for_live_account(live_account_id),
        artifacts_root=artifacts_root,
        store=activation_store or ShadowActivationStore(artifacts_root),
    )
    assert isinstance(record, ShadowActivationRecord)
    return record
```

The synthetic error message text (`"synthetic activation does not match repository identity"`) is preserved by `label`. `get_alpaca_clerk()`:

```python
def get_alpaca_clerk() -> ActiveAlpacaClerk | None:
    """Return the primary account authority, if installed: real paper, or the shadow of a live account.

    New callers that possess an account identity must use
    :func:`get_clerk_runtime`; this helper must never return a synthetic
    Clerk to a real-account caller by accident. Paper-only surfaces keep
    refusing on the facade's ``account_mode`` — a shadow authority answers
    ``"live"``.
    """
    if _runtime is None or _runtime.authority_kind not in {"sqlite", "shadow"}:
        return None
    return _runtime.clerk
```

Export `activate_shadow_clerk_authority` in `__all__`.

- [ ] **Step 7: The runner and the binding authority**

`bot_trade_strategy.run_trade_bot`: replace the `!= "sqlite"` guard with `if getattr(clerk, "authority_kind", None) not in {"sqlite", "shadow"}: raise RuntimeError("Trade-mode decisions require the active account Clerk (real paper, or the shadow of a live account).")`.

`bot_binding_authority.py`: rename `RealPaperBindingAuthority` → `PrimaryAccountBindingAuthority` (docstring: "The process's primary account authority — real paper, or the shadow of a live account — remains the sole real custody authority."), add the field `custody_kind: Callable[[], AccountAuthorityKind]` (no default; place it before `account_id`), and:

```python
    def source_bars(self) -> SourceBarLedger:
        return SourceBarLedger(
            artifacts_root=self.artifacts_root,
            account_id=evidence_account_id_for(
                mode=self.binding.mode,
                strategy_instance_id=self.binding.strategy_instance_id,
                custody_kind=self.custody_kind(),
            ),
        )
```

Add at module level:

```python
def primary_custody_kind() -> AccountAuthorityKind:
    """The world the primary authority custodies in; refuses to guess when none is installed."""
    runtime = get_active_clerk_runtime()
    kind = None if runtime is None else runtime.selected_account_authority_kind
    if kind is None:
        raise StartAdmissionUnavailable(
            "The account Clerk is not installed.",
            detail="Restore the account Clerk before starting a bot; its world decides where evidence is retained.",
        )
    return kind
```

`BindingAuthoritySelector.for_binding` passes `custody_kind=primary_custody_kind`; `bot_runner.py:301` needs no change (the selector owns it). Update `__all__` and the test import. `evidence_account_id_for` (Task 3) is redefined here to return a namespace for **every** non-dry-run mode — `log_only` retains into `paper:`/`shadow-evidence:` exactly as today's `RealPaperBindingAuthority` did; amend Task 3's function (drop the `None` branch, return type `str`) and its test (`log_only` → `"paper:b"`).

`run_replay_proof.ledger_account_id_for`:

```python
def ledger_account_id_for(binding: BrokerBotBinding) -> str:
    """Return the evidence namespace whose ledger retained this binding's bars."""
    if binding.mode not in {"dry_run", "trade"}:
        raise RunReplayUnavailableError(
            f"Mode {binding.mode!r} retains no source-bar evidence; nothing to replay.",
            http_status=404,
        )
    custody_kind = (
        "real_paper"
        if binding.sealed_account_id is None
        else authority_kind_for_account(binding.sealed_account_id)
    )
    return evidence_account_id_for(
        mode=binding.mode,
        strategy_instance_id=binding.strategy_instance_id,
        custody_kind=custody_kind,
    )
```

- [ ] **Step 8: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk tests/services/test_bot_binding_authority_source_bars.py tests/services/test_run_replay_proof_service.py tests/services/bot_runner tests/services/test_bot_trade_strategy_extended_bars.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
Expected: all pass (the whole `tests/broker/alpaca/clerk` tree, because the facade constructor and `get_alpaca_clerk` changed), ruff clean.

- [ ] **Step 9: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/app/broker/alpaca/clerk/account_authority.py PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py PythonDataService/app/broker/alpaca/clerk/active_protocol.py PythonDataService/app/broker/alpaca/clerk/trade_evidence.py PythonDataService/app/services/bot_trade_strategy.py PythonDataService/app/services/bot_binding_authority.py PythonDataService/app/services/run_replay_proof.py PythonDataService/tests/broker/alpaca/clerk/test_active_authority.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime_shadow.py PythonDataService/tests/broker/alpaca/clerk/test_trade_evidence.py PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py PythonDataService/tests/services/test_bot_binding_authority_source_bars.py PythonDataService/tests/services/test_run_replay_proof_service.py && git commit -q -m "feat(clerk): a live boot composes the Shadow Account Authority (ADR 0059 D2)

Where a live account was refused, select_active_clerk_runtime now verifies
the Clerk's order namespace is empty on the live account, binds the live read
port into the shadow world behind its own activation fence, and stands up the
Clerk, sweep and hold sync through the composition the real-paper authority
already uses. The facade admits the shadow kind and binds every decision bar;
the runner and the binding authority route trade bindings to it, with
instance-scoped shadow-evidence ledgers. The live trade port is never bound.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 6: The gates admit the shadow world; panel rows are typed; deploy offers an honest `shadow` mode

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py` (`primary_custody_world`), `app/services/bot_binding_authority.py` (`primary_custody_kind` reads it)
- Modify: `PythonDataService/app/schemas/account_authority.py` (`SIMULATED_AUTHORITY_KINDS`), `app/schemas/broker_v2_panel.py:38-52` (validator), `app/services/broker_v2_panel/sqlite_panel_adapter.py:590-612`, `app/services/broker_v2_panel/panel_projection_service.py:494-560`
- Modify: `PythonDataService/app/schemas/broker_bots.py:154, 246, 321, 347` (literals), `app/services/broker_v2_panel/panel_deploy.py:47-70, 180-260`, `app/services/broker_v2_panel/paper_deploy_service.py:151-156, 225-245, 530-560`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/account_operator_posture.py:100-150, 323`, `app/services/sqlite_clerk_compat.py:220-235`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py:98-112`
- Test: `tests/broker/v2panel/test_panel_deploy_shadow.py` (new), `tests/broker/alpaca/clerk/sqlite/test_account_operator_posture.py` (or wherever `build_account_operator_posture` is tested — one new case), `tests/broker/v2panel/test_panel_projection.py` (two new cases), `tests/broker/alpaca/clerk/sqlite/test_dev_reset.py` (one new case), `tests/services/test_run_admission.py` (one new case)

**Interfaces:**
- Produces: `active_authority.primary_custody_world() -> Literal["real_paper", "shadow"] | None`; `schemas.account_authority.SIMULATED_AUTHORITY_KINDS = frozenset({"synthetic", "shadow"})`; `AlpacaPaperDeployRequest.execution_mode: Literal["paper", "dry_run", "shadow"]`; `AlpacaPaperExecutionMode.mode: Literal["paper", "dry_run", "shadow", "live"]`; `AlpacaPaperDeployStrategy.admissible_modes: tuple[Literal["dry_run", "paper", "shadow"], ...]`; `AlpacaPaperDeployView.account_mode: Literal["paper", "live"]`; `AccountOperatorPostureContext.custody_world: Literal["real_paper", "shadow"] = "real_paper"`; `build_alpaca_paper_deploy_view(account, clerk, validation_entries, *, symbol, custody_world)`.
- Every refusal that stays: manual orders, historical execution recovery, cutover, fault injection and dev reset keep refusing on `account_mode != "paper"` — a shadow facade answers `"live"`. Dev reset additionally refuses the `shadow:` namespace by name (ADR 0059 D10). The ADR 0054 corpus gate in `run_admission.py:231` is untouched: a shadow instance must be corpus-covered, exactly as a live one must (D11).

- [ ] **Step 1: Write the failing tests**

`tests/broker/v2panel/test_panel_deploy_shadow.py` — build it from the existing deploy-view tests (`grep -rln "get_alpaca_paper_deploy_view\|build_alpaca_paper_deploy_view" tests/`; reuse their fixtures for the account snapshot, clerk status and validation entries):

```python
"""On a shadow authority the deploy path offers `shadow`, never `paper` (ADR 0059 D2)."""

# Arrange (from the existing deploy-view fixtures): a live account snapshot
# (account_mode="live", account_id="9LIVE0001"), a ClerkStatus double, and
# one accepted validation entry.

def test_shadow_world_authors_shadow_and_dry_run_only(...) -> None:
    view = build_alpaca_paper_deploy_view(live_account, clerk_status, entries, symbol="SPY", custody_world="shadow")
    assert view.account_mode == "live"
    assert view.account_label == "Alpaca shadow · 9LIVE0001"
    offered = {mode.mode: mode.availability for mode in view.execution_modes}
    assert offered == {"dry_run": "available", "shadow": "available", "live": "planned"}
    assert all("paper" not in strategy.admissible_modes for strategy in view.strategies)
    assert any("shadow" in strategy.admissible_modes for strategy in view.strategies)
    assert view.eligibility.eligible is True  # account_ready admits the shadow world


def test_real_paper_world_is_unchanged(...) -> None:
    view = build_alpaca_paper_deploy_view(paper_account, clerk_status, entries, symbol="SPY", custody_world="real_paper")
    assert view.account_mode == "paper"
    assert {mode.mode for mode in view.execution_modes} == {"dry_run", "paper", "live"}


async def test_deploy_request_for_a_mode_the_view_does_not_offer_is_refused(...) -> None:
    # `_require_alpaca_deploy_request(view_on_shadow, request(execution_mode="paper"))` raises
    # PanelRunnerError with http_status 409 and "not available on this account" in its message;
    # `request(execution_mode="shadow")` passes the same gate the paper request passes today.


async def test_live_account_with_no_shadow_world_is_still_refused(monkeypatch, ...) -> None:
    # `primary_custody_world` patched to return None; `get_alpaca_paper_deploy_view("alpaca", "9LIVE0001")`
    # with a live snapshot raises PanelUnavailableError("Alpaca live-account deployment is refused.").
```

Write those four with the fixtures the existing tests use; the assertions above are the contract. Then the single cases:

- posture: `build_account_operator_posture(AccountOperatorPostureContext(..., account_mode="live", custody_world="shadow"))` is **not** the `alpaca_account_wrong_execution_mode` posture; the same context with `custody_world="real_paper"` still is.
- projection: `RecentFillView(order_ref="r", symbol="SPY", side="buy", quantity=1.0, price=1.0, filled_at_ms=1, simulated=True, authority_account_id="shadow:9LIVE0001", authority_kind="shadow")` validates; the same with `authority_kind="synthetic"` raises `ValueError`; the adapter (`sqlite_panel_adapter`'s fill-row adapter) stamps a fill read from `shadow:9LIVE0001` as `simulated=True, authority_kind="shadow"`.
- dev reset: `developer_clean_slate_reset(account_id="shadow:9LIVE0001", account_mode="paper", ...)` raises `DeveloperCleanSlateResetRefused` matching `"shadow authority"` before any fence is taken.
- run admission: the existing corpus-gate test duplicated with a clerk snapshot whose `account_mode="live"` → `PROGRAM_CORPUS_UNCOVERED`.

- [ ] **Step 2: Run them to verify they fail**

Run the five test files above with the standard command. Expected: FAIL on the new keyword arguments and literals.

- [ ] **Step 3: One reader of the primary world**

`active_authority.py`, beside `active_program_leg_policy`:

```python
def primary_custody_world() -> Literal["real_paper", "shadow"] | None:
    """The world the primary authority custodies in, or ``None`` while none is installed.

    Gates that relabel or relax on the shadow world read this; a caller that
    would have to *guess* where evidence goes must refuse on ``None`` instead
    (``bot_binding_authority.primary_custody_kind``).
    """
    runtime = get_active_clerk_runtime()
    kind = None if runtime is None else runtime.selected_account_authority_kind
    return kind if kind in ("real_paper", "shadow") else None
```

`bot_binding_authority.primary_custody_kind` becomes `world = primary_custody_world(); if world is None: raise StartAdmissionUnavailable(...same copy...); return world`.

- [ ] **Step 4: Typed panel rows**

`app/schemas/account_authority.py`: add `SIMULATED_AUTHORITY_KINDS: frozenset[AuthorityKind] = frozenset({"synthetic", "shadow"})` with the comment "the two worlds whose fills are synthesized; every panel row they author is `simulated`". `broker_v2_panel._validate_simulated_authority_metadata`:

```python
_SIMULATED_NAMESPACES: tuple[tuple[str, AuthorityKind], ...] = (("sim:", "synthetic"), ("shadow:", "shadow"))


def _validate_simulated_authority_metadata(*, simulated, authority_account_id, authority_kind) -> None:
    """Require simulated panel evidence to name its isolated synthesized authority."""
    if not simulated:
        return
    if not any(
        authority_account_id is not None
        and authority_account_id.startswith(prefix)
        and authority_kind == kind
        for prefix, kind in _SIMULATED_NAMESPACES
    ):
        raise ValueError("simulated panel rows require nonempty synthetic or shadow authority metadata")
```

`sqlite_panel_adapter.py` fill adapter: `kind = authority_kind_for_account(authority_account_id)`; `simulated=kind in SIMULATED_AUTHORITY_KINDS, authority_kind=kind` (delete the two `startswith("sim:")` expressions). `panel_projection_service.py` non-dry-run branch: pass `simulated=authority_kind in SIMULATED_AUTHORITY_KINDS` to `_recent_decision_views`, and thread the same value into `_recent_fill_views` if it constructs `RecentFillView` directly (read it; the adapter overwrites `recent_fills` afterwards, so the decision rows are the load-bearing change).

- [ ] **Step 5: The deploy path**

`broker_bots.py`: widen the four literals listed under Interfaces. `paper_deploy_service.py`:

```python
BrokerExecutionMode = Literal["paper", "shadow"]


def _broker_mode_for(custody_world: Literal["real_paper", "shadow"]) -> BrokerExecutionMode:
    return "shadow" if custody_world == "shadow" else "paper"


def _admissible_modes(
    *, selectable: bool, has_runtime: bool, custody_world: Literal["real_paper", "shadow"]
) -> tuple[Literal["dry_run", "paper", "shadow"], ...]:
    """Derive the wire-facing mode set from the catalog's own launch facts (#1702, #1703)."""
    if selectable:
        return ("dry_run", _broker_mode_for(custody_world))
    if has_runtime:
        return ("dry_run",)
    return ()
```

`account_ready = (account.account_mode == "paper" or custody_world == "shadow") and account.account_status.upper() == "ACTIVE" and not account.trading_blocked and not account.account_blocked`. `build_alpaca_paper_deploy_view(..., *, symbol, custody_world)` threads `custody_world` into `_strategy_views` (→ `_admissible_modes`) and the readiness checks, and authors:

```python
    broker_mode = _broker_mode_for(custody_world)
    execution_modes = (
        AlpacaPaperExecutionMode(mode="dry_run", label="Dry Run", availability="available", explanation=(...unchanged...)),
        (
            AlpacaPaperExecutionMode(
                mode="shadow",
                label="Shadow",
                availability="available",
                explanation=(
                    "Decisions run against this live account's real reads; every fill is "
                    "synthesized by the shadow Clerk and nothing is submitted (ADR 0059 D2)."
                ),
            )
            if broker_mode == "shadow"
            else AlpacaPaperExecutionMode(mode="paper", label="Paper", availability="available", explanation="Orders route only to the selected Alpaca paper account through the Clerk.")
        ),
        AlpacaPaperExecutionMode(
            mode="live",
            label="Live",
            availability="planned",
            explanation=(
                "Real-money submission requires a completed shadow receipt and the arming "
                "ceremony (ADR 0059 slices 6-7)."
                if broker_mode == "shadow"
                else "Live Alpaca execution is planned but is not connected to an admission or execution path."
            ),
        ),
    )
    return AlpacaPaperDeployView(
        broker="alpaca",
        account_id=account.account_id,
        account_mode=account.account_mode,
        account_label=f"Alpaca {'shadow' if broker_mode == 'shadow' else 'paper'} · {account.account_id}",
        ...
        execution_modes=execution_modes,
        ...
    )
```

`panel_deploy.py`:

```python
    custody_world = primary_custody_world()
    if account.account_mode != "paper" and custody_world != "shadow":
        raise PanelUnavailableError(
            "Alpaca live-account deployment is refused.",
            detail="Real-money custody is not constructible until an armed instance exists (ADR 0059 slice 7); a live account deploys only through its shadow authority.",
            next_action="Activate the shadow authority for this account, or reconnect with Alpaca paper credentials, then refresh.",
        )
    ...
    return build_alpaca_paper_deploy_view(account, clerk, validation_entries, symbol=symbol, custody_world=custody_world or "real_paper")
```

`_require_alpaca_deploy_request`: before the mode dispatch,

```python
    offered = {mode.mode for mode in view.execution_modes if mode.availability == "available"}
    if request.execution_mode not in offered:
        raise PanelRunnerError(
            "The requested execution mode is not available on this account.",
            detail=f"'{request.execution_mode}' is not offered by the {view.account_label} deploy view.",
            next_action="Choose an execution mode the deploy view lists as available.",
            http_status=409,
        )
```

then `dry_run` → `_require_dry_run_deploy_request`; `paper` and `shadow` → `_require_broker_deploy_request(view, strategy, request)` (the renamed `_require_paper_deploy_request`, whose admissibility check becomes `request.execution_mode not in strategy.admissible_modes`; its human-validation and custody checks are unchanged — a shadow run holds shadow custody and needs the same proof). The binding mode mapping (`"dry_run" if ... else "trade"`) is unchanged: a shadow run is a `trade` binding on the shadow authority. Update the docstrings that say "Paper" to "Paper / Shadow" where the check now serves both.

- [ ] **Step 6: Posture and dev reset**

`AccountOperatorPostureContext` gains `custody_world: Literal["real_paper", "shadow"] = "real_paper"` (docstring: "the world the primary authority custodies in; a shadow authority reads a live account by design, so `account_mode == 'live'` is not the wrong mode there"). The blocking posture at `:323` becomes `if ctx.account_mode != "paper" and ctx.custody_world != "shadow":`. `sqlite_clerk_compat.py` passes `custody_world=primary_custody_world() or "real_paper"`.

`dev_reset.developer_clean_slate_reset`, first statement:

```python
    if is_shadow_account_id(account_id):
        raise DeveloperCleanSlateResetRefused(
            "developer clean-slate reset never touches a shadow authority (ADR 0059 D10)"
        )
```

- [ ] **Step 7: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/v2panel tests/broker/alpaca/clerk/sqlite/test_dev_reset.py tests/services/test_run_admission.py tests/services/test_sqlite_clerk_compat.py tests/routers -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
(Substitute the real test-file names where the ones above differ.) Expected: all pass, ruff clean. `scripts/export_openapi_contract.py --check` is expected to FAIL from this task until Task 12 regenerates — note it in the commit body.

- [ ] **Step 8: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/app/services/bot_binding_authority.py PythonDataService/app/schemas/account_authority.py PythonDataService/app/schemas/broker_v2_panel.py PythonDataService/app/services/broker_v2_panel/sqlite_panel_adapter.py PythonDataService/app/services/broker_v2_panel/panel_projection_service.py PythonDataService/app/schemas/broker_bots.py PythonDataService/app/services/broker_v2_panel/panel_deploy.py PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py PythonDataService/app/broker/alpaca/clerk/sqlite/account_operator_posture.py PythonDataService/app/services/sqlite_clerk_compat.py PythonDataService/app/broker/alpaca/clerk/sqlite/dev_reset.py PythonDataService/tests/broker/v2panel PythonDataService/tests/broker/alpaca/clerk/sqlite PythonDataService/tests/services/test_run_admission.py && git commit -q -m "feat(panel): the shadow world is deployable and its rows are typed (ADR 0059 D2, D10)

A live account deploys only through its shadow authority: the deploy view
offers shadow and dry run, never paper; a request for an unoffered mode is a
409; the account posture no longer calls a shadow authority's live mode the
wrong mode. Every panel row a shadow authority authors is simulated and
typed shadow. Dev reset refuses the shadow namespace by name. Manual orders,
recovery, cutover and fault injection keep refusing anything but paper.

OpenAPI --check fails from this commit until the contract is regenerated at
the end of the branch.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 7: Twin reconciliation and the shadow gate evaluation

**Files:**
- Create: `PythonDataService/app/services/alpaca_shadow_reconciliation.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/economic_projection.py` (`from_database_path`, `runs_for_strategy`)
- Test: `PythonDataService/tests/services/test_alpaca_shadow_reconciliation.py`; `tests/broker/alpaca/clerk/sqlite/test_economic_projection.py` (or the file that tests `SqliteEconomicProjectionReader` — two new cases)

**Interfaces:**
- Consumes: `app.research.parity.qc_reconciler.DivergenceCategory` (the repo taxonomy; numerical-rigor.md keeps it in lockstep), Task 4 `ShadowSessionLedger.day_state`, `sqlite/models.RunResource`, `clerk/fills.FillRecord`, `decision_session.RunDecisionSession`, `session_authority.declared_session_bounds`, `trading_calendar.expected_sessions / session_open_ms_utc / session_close_ms_utc` (sealed; import only), `session_anchors.et_date_at_ms / et_midnight_ms / et_day_end_ms`, `SealedBotProgram`.
- Produces: `FILL_PRICE_ATOL = Decimal("0.01")`, `GATING_CATEGORIES`, `TwinFill`, `TwinDivergence`, `TwinDayReconciliation` (`passed`, `gating`, `report_sha256()`), `reconcile_twin_day(...)`, `FillSource` protocol, `EconomicFillSource(reader)`, `read_twin_fills(source, *, strategy_instance_id, session_open_ms)`, `ShadowTwinMismatch(reason)`, `twins_agree(shadow, twin) -> str | None`, `ShadowSessionVerdict`, `ShadowGateEvaluation` (`counted`, `satisfied`), `evaluate_shadow_gate(...)`; `SqliteEconomicProjectionReader.from_database_path(db_path)`, `.runs_for_strategy(sid)`.
- Consumed by Task 8 (the receipt names each counted session's `report_sha256`) and Task 9 (the CLI).

- [ ] **Step 1: Write the failing tests**

`PythonDataService/tests/services/test_alpaca_shadow_reconciliation.py`:

```python
"""Shadow-vs-paper-twin reconciliation gates on decisions and reports prices (ADR 0059 D2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.models import RunResource
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.signal_program_seal import SealedBotProgram
from app.services.alpaca_shadow_reconciliation import (
    FILL_PRICE_ATOL,
    ShadowTwinMismatch,
    TwinFill,
    evaluate_shadow_gate,
    reconcile_twin_day,
    twins_agree,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.session_authority import et_minute_of_day_ms

DAY = date(2026, 9, 8)
OPEN = session_open_ms_utc(DAY)
CLOSE = session_close_ms_utc(DAY)
SID, TWIN = "ema-shadow-1", "ema-paper-1"


def _fill(minute: int, *, side: str = "buy", qty: str = "1", price: str = "100.00", ref: str = "a") -> TwinFill:
    return TwinFill(
        symbol="SPY", side=side, quantity=Decimal(qty), fill_price=Decimal(price),
        filled_at_ms=et_minute_of_day_ms(DAY, minute), order_ref=f"learn-ai/x/v1:{ref}",
    )


def _reconcile(shadow: list[TwinFill], twin: list[TwinFill]):
    return reconcile_twin_day(
        session_open_ms=OPEN, strategy_instance_id=SID, twin_strategy_instance_id=TWIN,
        shadow_fills=tuple(shadow), twin_fills=tuple(twin),
    )


def test_identical_days_pass_with_no_divergence_and_a_stable_digest() -> None:
    first = _reconcile([_fill(600), _fill(660, side="sell")], [_fill(600, ref="p"), _fill(660, side="sell", ref="q")])
    second = _reconcile([_fill(600), _fill(660, side="sell")], [_fill(600, ref="p"), _fill(660, side="sell", ref="q")])
    assert first.passed is True and first.divergences == ()
    assert first.report_sha256() == second.report_sha256()
    assert len(first.report_sha256()) == 64


@pytest.mark.parametrize(
    ("shadow", "twin", "category"),
    [
        ([_fill(600)], [], DivergenceCategory.DECISION_MISMATCH),
        ([_fill(600)], [_fill(600, side="sell")], DivergenceCategory.DIRECTION_MISMATCH),
        ([_fill(600, qty="2")], [_fill(600, qty="1")], DivergenceCategory.QUANTITY_MISMATCH),
    ],
)
def test_shape_divergences_gate(shadow: list[TwinFill], twin: list[TwinFill], category: DivergenceCategory) -> None:
    result = _reconcile(shadow, twin)
    assert [d.category for d in result.gating] == [category]
    assert result.passed is False


def test_price_drift_is_reported_never_gated() -> None:
    result = _reconcile([_fill(600, price="100.00")], [_fill(600, price="100.07")])
    assert [d.category for d in result.divergences] == [DivergenceCategory.FILL_PRICE_DRIFT]
    assert result.gating == () and result.passed is True
    assert result.max_fill_price_drift == Decimal("0.07")
    assert result.fill_price_atol == FILL_PRICE_ATOL == Decimal("0.01")
    within = _reconcile([_fill(600, price="100.00")], [_fill(600, price="100.01")])
    assert within.divergences == () and within.max_fill_price_drift == Decimal("0.01")


def _seal(**overrides: object) -> SealedBotProgram:
    base = dict(
        configured_signal_hash="a" * 64, quantity=1, action_plan=alpaca_v1_action_plan("SPY"),
        carryover_policy="FORBID", mode="trade", sealed_account_id="PA-TEST",
    )
    return SealedBotProgram.model_construct(**{**base, **overrides})


def test_twins_agree_on_the_configured_signal_and_plan_only() -> None:
    shadow = _seal(sealed_account_id="shadow:9LIVE0001")
    assert twins_agree(shadow, _seal()) is None
    assert twins_agree(shadow, _seal(configured_signal_hash="b" * 64)) is not None
    assert twins_agree(shadow, _seal(quantity=2)) is not None
    assert twins_agree(shadow, _seal(mode="dry_run")) is not None
    assert twins_agree(_seal(), _seal()) is not None  # the shadow side must be shadow-sealed


class _Source:
    def __init__(self, fills: dict[str, list[TwinFill]], runs: dict[str, list[RunResource]]) -> None:
        self._fills, self._runs = fills, runs

    def fills_between(self, *, strategy_instance_id: str, from_ms: int, to_ms: int) -> tuple[TwinFill, ...]:
        return tuple(f for f in self._fills.get(strategy_instance_id, ()) if from_ms <= f.filled_at_ms <= to_ms)

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        return tuple(self._runs.get(strategy_instance_id, ()))


def _run(started_ms: int, stopped_ms: int | None) -> RunResource:
    return RunResource(run_id="run-1", strategy_instance_id=SID, lifecycle_run_id="l-1", state="ACTIVE" if stopped_ms is None else "STOPPED", started_at_ms=started_ms, stopped_at_ms=stopped_ms)


def _binding(sid: str, sealed: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY", use_rth=True,
        mode="trade", quantity=1, action_plan=alpaca_v1_action_plan("SPY"), sealed_account_id=sealed, run_id="run-1", created_at_ms=0,
        sealed_program=_seal(sealed_account_id=sealed),
    )


def _clean_day(ledger: ShadowSessionLedger, *, opened_minute: int = 180) -> None:
    ledger.append(kind="day_opened", session_open_ms=OPEN, observed_at_ms=et_minute_of_day_ms(DAY, opened_minute), verdict="clean")
    ledger.append(kind="session_closed_clean", session_open_ms=OPEN, observed_at_ms=et_minute_of_day_ms(DAY, 1200), verdict="clean")


def _evaluate(tmp_path: Path, *, ledger: ShadowSessionLedger, source: _Source, required: int = 1):
    return evaluate_shadow_gate(
        live_account_id="9LIVE0001",
        shadow_binding=_binding(SID, "shadow:9LIVE0001"),
        twin_binding=_binding(TWIN, "PA-TEST"),
        twin_account_id="PA-TEST",
        required_sessions=required,
        session_ledger=ledger,
        shadow_source=source,
        twin_source=source,
        window=ALPACA_EXTENDED_HOURS_WINDOW,
        now_ms=et_minute_of_day_ms(date(2026, 9, 9), 60),
    )


def test_a_clean_covered_reconciled_day_counts(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger)
    source = _Source({SID: [_fill(600)], TWIN: [_fill(600, ref="p")]}, {SID: [_run(OPEN - 1, None)]})

    evaluation = _evaluate(tmp_path, ledger=ledger, source=source)

    [verdict] = evaluation.sessions
    assert (verdict.state, verdict.shadow_run_id) == ("counted", "run-1")
    assert verdict.reconciliation is not None and verdict.reconciliation.passed
    assert evaluation.satisfied is True and evaluation.counted == (verdict,)


@pytest.mark.parametrize(
    ("opened_minute", "run", "twin_fills", "state"),
    [
        (600, _run(OPEN - 1, None), [_fill(600, ref="p")], "sweep_opened_late"),
        (180, _run(OPEN + 1, None), [_fill(600, ref="p")], "run_not_covering"),
        (180, _run(OPEN - 1, CLOSE - 1), [_fill(600, ref="p")], "run_not_covering"),
        (180, _run(OPEN - 1, None), [], "twin_diverged"),
    ],
)
def test_days_that_do_not_count_say_why(tmp_path: Path, opened_minute: int, run: RunResource, twin_fills: list[TwinFill], state: str) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    _clean_day(ledger, opened_minute=opened_minute)
    source = _Source({SID: [_fill(600)], TWIN: twin_fills}, {SID: [run]})

    evaluation = _evaluate(tmp_path, ledger=ledger, source=source)

    assert [v.state for v in evaluation.sessions] == [state]
    assert evaluation.satisfied is False


def test_a_non_clean_day_and_a_seal_mismatch_are_named(tmp_path: Path) -> None:
    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id="shadow:9LIVE0001")
    ledger.append(kind="day_opened", session_open_ms=OPEN, observed_at_ms=et_minute_of_day_ms(DAY, 180), verdict="stale")
    ledger.append(kind="non_clean", session_open_ms=OPEN, observed_at_ms=et_minute_of_day_ms(DAY, 180), verdict="stale")
    source = _Source({}, {SID: [_run(OPEN - 1, None)]})
    assert [v.state for v in _evaluate(tmp_path, ledger=ledger, source=source).sessions] == ["sweep_not_clean"]

    with pytest.raises(ShadowTwinMismatch, match="configured signal"):
        evaluate_shadow_gate(
            live_account_id="9LIVE0001",
            shadow_binding=_binding(SID, "shadow:9LIVE0001"),
            twin_binding=_binding(TWIN, "PA-TEST").model_copy(update={"sealed_program": _seal(configured_signal_hash="b" * 64)}),
            twin_account_id="PA-TEST", required_sessions=1, session_ledger=ledger,
            shadow_source=source, twin_source=source, window=ALPACA_EXTENDED_HOURS_WINDOW, now_ms=CLOSE + 1,
        )
```

Reader tests (in the economic-projection test file): `SqliteEconomicProjectionReader.from_database_path(repo.db_path)` opens a repository initialized in `tmp_path` while the repository handle is still open (no lease conflict), and `runs_for_strategy` returns the runs `register_strategy_run` + a stopped run created; a path without `control_meta` raises `EconomicProjectionUnavailable`.

- [ ] **Step 2: Run them to verify they fail**

Run the two test files. Expected: FAIL — `ModuleNotFoundError` / `AttributeError`.

- [ ] **Step 3: The read-only openers on `SqliteEconomicProjectionReader`**

```python
    @classmethod
    def from_database_path(cls, db_path: Path) -> SqliteEconomicProjectionReader:
        """Open one authority's database read-only by path — a foreign one's included.

        The paper twin's process holds that database's execution lease; this
        reader never takes it (``mode=ro``, ``query_only``), so the twin
        reconciliation can read it while the twin runs.
        """
        probe = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = probe.execute(
                "SELECT account_id, authority_generation, db_identity_token FROM control_meta WHERE id = 1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EconomicProjectionUnavailable(f"{db_path} is not a readable clerk database: {exc}") from exc
        finally:
            probe.close()
        if row is None:
            raise EconomicProjectionUnavailable(f"{db_path} has no control_meta row")
        return cls(db_path=db_path, account_id=row[0], authority_generation=row[1], db_identity_token=row[2])

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        """Every run the authority recorded for one instance, oldest first."""
        with self._read_transaction():
            self._verified_meta()
            rows = self._conn.execute(
                "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, "
                "stopped_at_ms FROM runs WHERE strategy_instance_id = ? "
                "ORDER BY started_at_ms ASC, run_id ASC",
                (strategy_instance_id,),
            ).fetchall()
        return tuple(RunResource(**dict(row)) for row in rows)
```

(`RunResource` from `sqlite/models.py`; `_verified_meta` is the existing per-read identity check.)

- [ ] **Step 4: Create `alpaca_shadow_reconciliation.py`**

```python
"""Shadow-vs-paper-twin reconciliation and the shadow gate (ADR 0059 D2).

A shadow session counts only when (a) the sweep reconciled cleanly for the
whole day, (b) the instance's run covered its whole decision session, and
(c) the instance's synthesized trades reconcile against its paper twin — the
same configured signal, plan and size sealed to the paper account, run over
the same day. Shadow proves the live plumbing; it does not prove execution
quality, so the twin comparison gates on *decisions* and only reports prices.

Math Provenance Contract (reconcile_twin_day)
--------------------------------------------
Formula: order each side's fills by ``(filled_at_ms, order_ref)`` and pair
them by index. The first failing rule classifies a pair: a fill with no
partner, or partners on different symbols, is ``DECISION_MISMATCH``;
different sides, ``DIRECTION_MISMATCH``; different quantities,
``QUANTITY_MISMATCH``; otherwise ``|shadow.price - twin.price| > atol`` is
``FILL_PRICE_DRIFT``. The gating set is ``{DECISION_MISMATCH,
DIRECTION_MISMATCH, QUANTITY_MISMATCH}``; ``passed`` iff no gating divergence.
``atol = $0.01``, the taxonomy's ``fill_price_atol`` default.
Reference: ``.claude/rules/numerical-rigor.md`` § "Trade-level reconciliation
taxonomy" (the ``DivergenceCategory`` enum); ADR 0059 D2 — "synthesized fills
are optimistic by construction", so price drift is reported, never gated.
Canonical implementation: this file.
Validated against: ``tests/services/test_alpaca_shadow_reconciliation.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from app.broker.alpaca.clerk.account_authority import is_shadow_account_id
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.models import RunResource
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.signal_program_seal import SealedBotProgram
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import declared_session_bounds
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms

FILL_PRICE_ATOL = Decimal("0.01")
GATING_CATEGORIES: frozenset[DivergenceCategory] = frozenset(
    {
        DivergenceCategory.DECISION_MISMATCH,
        DivergenceCategory.DIRECTION_MISMATCH,
        DivergenceCategory.QUANTITY_MISMATCH,
    }
)
SessionState = Literal[
    "counted",
    "sweep_not_clean",
    "sweep_opened_late",
    "run_not_covering",
    "twin_diverged",
    "not_evaluable",
]


@dataclass(frozen=True)
class TwinFill:
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    filled_at_ms: int
    order_ref: str


@dataclass(frozen=True)
class TwinDivergence:
    category: DivergenceCategory
    index: int
    detail: str


@dataclass(frozen=True)
class TwinDayReconciliation:
    session_open_ms: int
    strategy_instance_id: str
    twin_strategy_instance_id: str
    shadow_fills: tuple[TwinFill, ...]
    twin_fills: tuple[TwinFill, ...]
    divergences: tuple[TwinDivergence, ...]
    max_fill_price_drift: Decimal | None
    fill_price_atol: Decimal

    @property
    def gating(self) -> tuple[TwinDivergence, ...]:
        return tuple(d for d in self.divergences if d.category in GATING_CATEGORIES)

    @property
    def passed(self) -> bool:
        return not self.gating

    def report_sha256(self) -> str:
        """A content hash of the whole comparison — what the receipt names."""
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ordered(fills: Sequence[TwinFill]) -> tuple[TwinFill, ...]:
    return tuple(sorted(fills, key=lambda fill: (fill.filled_at_ms, fill.order_ref)))


def reconcile_twin_day(
    *,
    session_open_ms: int,
    strategy_instance_id: str,
    twin_strategy_instance_id: str,
    shadow_fills: Sequence[TwinFill],
    twin_fills: Sequence[TwinFill],
    fill_price_atol: Decimal = FILL_PRICE_ATOL,
) -> TwinDayReconciliation:
    shadow, twin = _ordered(shadow_fills), _ordered(twin_fills)
    divergences: list[TwinDivergence] = []
    drifts: list[Decimal] = []
    for index in range(max(len(shadow), len(twin))):
        left = shadow[index] if index < len(shadow) else None
        right = twin[index] if index < len(twin) else None
        if left is None or right is None:
            side = "twin" if left is None else "shadow"
            divergences.append(TwinDivergence(DivergenceCategory.DECISION_MISMATCH, index, f"only the {side} side has fill #{index}"))
            continue
        if left.symbol != right.symbol:
            divergences.append(TwinDivergence(DivergenceCategory.DECISION_MISMATCH, index, f"{left.symbol} vs {right.symbol}"))
            continue
        if left.side != right.side:
            divergences.append(TwinDivergence(DivergenceCategory.DIRECTION_MISMATCH, index, f"{left.side} vs {right.side}"))
            continue
        if left.quantity != right.quantity:
            divergences.append(TwinDivergence(DivergenceCategory.QUANTITY_MISMATCH, index, f"{left.quantity} vs {right.quantity}"))
            continue
        drift = abs(left.fill_price - right.fill_price)
        drifts.append(drift)
        if drift > fill_price_atol:
            divergences.append(TwinDivergence(DivergenceCategory.FILL_PRICE_DRIFT, index, f"|{left.fill_price} - {right.fill_price}| = {drift} > {fill_price_atol}"))
    return TwinDayReconciliation(
        session_open_ms=session_open_ms,
        strategy_instance_id=strategy_instance_id,
        twin_strategy_instance_id=twin_strategy_instance_id,
        shadow_fills=shadow,
        twin_fills=twin,
        divergences=tuple(divergences),
        max_fill_price_drift=max(drifts) if drifts else None,
        fill_price_atol=fill_price_atol,
    )


class FillSource(Protocol):
    """Where one authority's fills and runs are read from (a read-only projection in production)."""

    def fills_between(self, *, strategy_instance_id: str, from_ms: int, to_ms: int) -> tuple[TwinFill, ...]: ...

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]: ...


class EconomicFillSource:
    """``FillSource`` over ``SqliteEconomicProjectionReader`` (``mode=ro``; never the lease)."""

    def __init__(self, reader: SqliteEconomicProjectionReader) -> None:
        self._reader = reader

    @classmethod
    def from_database_path(cls, db_path: Path) -> EconomicFillSource:
        return cls(SqliteEconomicProjectionReader.from_database_path(db_path))

    def fills_between(self, *, strategy_instance_id: str, from_ms: int, to_ms: int) -> tuple[TwinFill, ...]:
        records = self._reader.account_fill_window(from_ms=from_ms, to_ms=to_ms)
        return tuple(
            TwinFill(
                symbol=record.symbol,
                side=str(record.side),
                quantity=Decimal(str(record.quantity)),
                fill_price=Decimal(str(record.fill_price)),
                filled_at_ms=record.filled_at_ms,
                order_ref=record.order_ref,
            )
            for record in records
            if record.sid == strategy_instance_id
        )

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        return self._reader.runs_for_strategy(strategy_instance_id)

    def close(self) -> None:
        self._reader.close()


def read_twin_fills(source: FillSource, *, strategy_instance_id: str, session_open_ms: int) -> tuple[TwinFill, ...]:
    """The ET calendar day's fills for one instance (a shadow extended fill can land after the close)."""
    day = et_date_at_ms(session_open_ms)
    return source.fills_between(
        strategy_instance_id=strategy_instance_id,
        from_ms=et_midnight_ms(day),
        to_ms=et_day_end_ms(day) - 1,
    )


class ShadowTwinMismatch(ValueError):
    """The named twin is not this instance's paper twin."""

    reason_code = "SHADOW_TWIN_MISMATCH"


def twins_agree(shadow: SealedBotProgram, twin: SealedBotProgram) -> str | None:
    """``None`` when ``twin`` is ``shadow``'s paper twin; otherwise the first disagreement.

    Identity is the configured signal, the action plan, the size and the
    carryover policy — never the instance id, the account, or the outer seal
    hash (both of those legitimately differ between the two worlds).
    """
    if not is_shadow_account_id(shadow.sealed_account_id):
        return "the shadow side is not sealed to a shadow: account"
    if is_shadow_account_id(twin.sealed_account_id):
        return "the twin is sealed to a shadow: account, not the paper account"
    if shadow.mode != "trade" or twin.mode != "trade":
        return "both twins must be trade-mode bindings"
    if shadow.configured_signal_hash != twin.configured_signal_hash:
        return "the twins do not share a configured signal"
    if shadow.action_plan != twin.action_plan:
        return "the twins do not share an action plan"
    if shadow.quantity != twin.quantity:
        return "the twins do not share a size"
    if shadow.carryover_policy != twin.carryover_policy:
        return "the twins do not share a carryover policy"
    return None


@dataclass(frozen=True)
class ShadowSessionVerdict:
    session_open_ms: int
    state: SessionState
    detail: str
    shadow_run_id: str | None
    reconciliation: TwinDayReconciliation | None


@dataclass(frozen=True)
class ShadowGateEvaluation:
    live_account_id: str
    strategy_instance_id: str
    twin_account_id: str
    twin_strategy_instance_id: str
    configured_signal_hash: str
    required_sessions: int
    sessions: tuple[ShadowSessionVerdict, ...]

    @property
    def counted(self) -> tuple[ShadowSessionVerdict, ...]:
        return tuple(verdict for verdict in self.sessions if verdict.state == "counted")

    @property
    def satisfied(self) -> bool:
        return len(self.counted) >= self.required_sessions


def _decision_span(session: RunDecisionSession, day: date) -> tuple[int, int]:
    if session.kind == "rth":
        return session_open_ms_utc(day), session_close_ms_utc(day)
    bounds = declared_session_bounds(day, session.window)
    assert bounds is not None  # ``day`` comes from expected_sessions
    return bounds.open_ms, bounds.close_ms


def _covering_run(runs: Sequence[RunResource], *, open_ms: int, close_ms: int) -> RunResource | None:
    return next(
        (
            run
            for run in runs
            if run.started_at_ms <= open_ms and (run.stopped_at_ms is None or run.stopped_at_ms >= close_ms)
        ),
        None,
    )


def evaluate_shadow_gate(
    *,
    live_account_id: str,
    shadow_binding: BrokerBotBinding,
    twin_binding: BrokerBotBinding,
    twin_account_id: str,
    required_sessions: int,
    session_ledger: ShadowSessionLedger,
    shadow_source: FillSource,
    twin_source: FillSource,
    window: ExtendedHoursWindow | None,
    now_ms: int,
) -> ShadowGateEvaluation:
    """Judge every trading day since the shadow instance first ran (ADR 0059 D2)."""
    if shadow_binding.sealed_program is None or twin_binding.sealed_program is None:
        raise ShadowTwinMismatch("both bindings must carry their sealed program")
    disagreement = twins_agree(shadow_binding.sealed_program, twin_binding.sealed_program)
    if disagreement is not None:
        raise ShadowTwinMismatch(disagreement)
    runs = shadow_source.runs_for_strategy(shadow_binding.strategy_instance_id)
    session = RunDecisionSession.resolve(use_rth=shadow_binding.use_rth, window=window)
    verdicts: list[ShadowSessionVerdict] = []
    if runs and session is not None:
        first_day = et_date_at_ms(min(run.started_at_ms for run in runs))
        for day in expected_sessions(first_day, et_date_at_ms(now_ms)):
            open_ms, close_ms = _decision_span(session, day)
            if close_ms > now_ms:
                continue
            verdicts.append(
                _judge_day(
                    day,
                    open_ms=open_ms,
                    close_ms=close_ms,
                    runs=runs,
                    session_ledger=session_ledger,
                    shadow_source=shadow_source,
                    twin_source=twin_source,
                    strategy_instance_id=shadow_binding.strategy_instance_id,
                    twin_strategy_instance_id=twin_binding.strategy_instance_id,
                )
            )
    elif runs:
        verdicts.append(
            ShadowSessionVerdict(
                session_open_ms=session_open_ms_utc(et_date_at_ms(runs[0].started_at_ms)),
                state="not_evaluable",
                detail="an extended-session binding has no declared window to judge against",
                shadow_run_id=None,
                reconciliation=None,
            )
        )
    return ShadowGateEvaluation(
        live_account_id=live_account_id,
        strategy_instance_id=shadow_binding.strategy_instance_id,
        twin_account_id=twin_account_id,
        twin_strategy_instance_id=twin_binding.strategy_instance_id,
        configured_signal_hash=shadow_binding.sealed_program.configured_signal_hash,
        required_sessions=required_sessions,
        sessions=tuple(verdicts),
    )


def _judge_day(
    day: date,
    *,
    open_ms: int,
    close_ms: int,
    runs: Sequence[RunResource],
    session_ledger: ShadowSessionLedger,
    shadow_source: FillSource,
    twin_source: FillSource,
    strategy_instance_id: str,
    twin_strategy_instance_id: str,
) -> ShadowSessionVerdict:
    calendar_open_ms = session_open_ms_utc(day)
    state = session_ledger.day_state(calendar_open_ms)
    verdict = ShadowSessionVerdict(session_open_ms=calendar_open_ms, state="counted", detail="", shadow_run_id=None, reconciliation=None)
    if not state.complete:
        why = (
            f"non-clean sweep verdicts: {', '.join(state.non_clean_verdicts)}"
            if state.non_clean_verdicts
            else "the sweep never opened the day" if state.opened_at_ms is None else "the sweep did not close the day clean"
        )
        return verdict.__class__(**{**asdict(verdict), "state": "sweep_not_clean", "detail": why})
    if state.opened_at_ms is not None and state.opened_at_ms > open_ms:
        return verdict.__class__(**{**asdict(verdict), "state": "sweep_opened_late", "detail": "the sweep's first pass came after the decision session opened"})
    run = _covering_run(runs, open_ms=open_ms, close_ms=close_ms)
    if run is None:
        return verdict.__class__(**{**asdict(verdict), "state": "run_not_covering", "detail": "no run of this instance spanned the whole decision session"})
    reconciliation = reconcile_twin_day(
        session_open_ms=calendar_open_ms,
        strategy_instance_id=strategy_instance_id,
        twin_strategy_instance_id=twin_strategy_instance_id,
        shadow_fills=read_twin_fills(shadow_source, strategy_instance_id=strategy_instance_id, session_open_ms=calendar_open_ms),
        twin_fills=read_twin_fills(twin_source, strategy_instance_id=twin_strategy_instance_id, session_open_ms=calendar_open_ms),
    )
    if not reconciliation.passed:
        detail = "; ".join(f"{d.category}: {d.detail}" for d in reconciliation.gating)
        return ShadowSessionVerdict(calendar_open_ms, "twin_diverged", detail, run.run_id, reconciliation)
    return ShadowSessionVerdict(calendar_open_ms, "counted", "", run.run_id, reconciliation)
```

Write `_judge_day`'s early returns as plain `ShadowSessionVerdict(...)` constructions rather than the `verdict.__class__(**{**asdict(...)})` shorthand shown for brevity. `TwinDayReconciliation.report_sha256` serialises `Decimal` and `DivergenceCategory` through `default=str`, which is deterministic for both.

- [ ] **Step 5: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_shadow_reconciliation.py tests/broker/alpaca/clerk/sqlite -q -p no:cacheprovider -k "shadow or economic" && .venv/bin/ruff check app/ tests/`
Expected: all pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/services/alpaca_shadow_reconciliation.py PythonDataService/app/broker/alpaca/clerk/sqlite/economic_projection.py PythonDataService/tests/services/test_alpaca_shadow_reconciliation.py PythonDataService/tests/broker/alpaca/clerk/sqlite && git commit -q -m "feat(shadow): twin reconciliation gates on decisions and the gate judges each trading day (ADR 0059 D2)

Shadow fills pair with the paper twin's by sequence and shape under the
repo's divergence taxonomy; price drift is reported, never gated, because a
synthesized fill is optimistic by construction. A day counts only when the
sweep was clean from before the session opened to after it closed, one run
spanned the whole decision session, and the twin reconciled. The paper
twin's database is read mode=ro without its lease.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 8: The shadow receipt store, and the live verdict says where shadow stands

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/shadow_receipt.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py` (rename `_digest` → public `canonical_sha256`; update its two call sites)
- Modify: `PythonDataService/app/schemas/alpaca_live_verdict.py` (`ClerkAuthority` gains `"shadow"`), `app/services/alpaca_live_verdict.py` (`shadow_state` input; `observe_shadow_state`), `app/routers/brokers.py:690-710`
- Test: `tests/broker/alpaca/clerk/test_shadow_receipt.py`, `tests/services/test_alpaca_live_verdict.py`, `tests/routers/test_alpaca_live_verdict_endpoint.py`

**Interfaces:**
- Produces: `SHADOW_RECEIPTS_FILENAME = "shadow_receipts.jsonl"`, `ShadowReceiptInvalid`, `ShadowReceiptSession(session_open_ms, shadow_run_id, reconciliation_sha256)`, `ShadowReceipt.create(...)` / `from_payload(...)` (sha256-sealed, `receipt_sha256`), `ShadowReceiptStore(artifacts_root)` at `accounts/shadow/shadow_receipts.jsonl` with `append`, `all_for(strategy_instance_id)`, `latest(strategy_instance_id)`, `current(strategy_instance_id, *, configured_signal_hash, required_sessions)`, `any_for_account(live_account_id)`; `alpaca_live_verdict(..., shadow_state=None)`; `observe_shadow_state(runtime, artifacts_root) -> ShadowState`.
- Slice 6 consumes `ShadowReceiptStore.current(...)`; arming without one is `LIVE_SHADOW_INCOMPLETE` there.

- [ ] **Step 1: Write the failing tests**

`tests/broker/alpaca/clerk/test_shadow_receipt.py`:

```python
"""The sealed per-instance proof that the shadow gate passed (ADR 0059 D2)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_receipt import (
    SHADOW_RECEIPTS_FILENAME,
    ShadowReceipt,
    ShadowReceiptInvalid,
    ShadowReceiptSession,
    ShadowReceiptStore,
)

SIGNAL = "a" * 64


def _receipt(*, sessions: int = 2, signal: str = SIGNAL, written_at_ms: int = 1_700_000_000_000) -> ShadowReceipt:
    return ShadowReceipt.create(
        live_account_id="9LIVE0001",
        strategy_instance_id="ema-shadow-1",
        configured_signal_hash=signal,
        twin_account_id="PA-TEST",
        twin_strategy_instance_id="ema-paper-1",
        required_sessions=2,
        sessions=tuple(
            ShadowReceiptSession(session_open_ms=1_000 + index, shadow_run_id="run-1", reconciliation_sha256="b" * 64)
            for index in range(sessions)
        ),
        written_at_ms=written_at_ms,
    )


def test_receipt_is_sealed_and_verifies() -> None:
    receipt = _receipt()
    assert len(receipt.receipt_sha256) == 64
    assert ShadowReceipt.from_payload(asdict(receipt)) == receipt
    with pytest.raises(ShadowReceiptInvalid, match="digest does not verify"):
        ShadowReceipt.from_payload({**asdict(receipt), "required_sessions": 1})
    with pytest.raises(ShadowReceiptInvalid, match="shadow: account"):
        ShadowReceipt.from_payload({**asdict(receipt), "live_account_id": "shadow:9LIVE0001"})


def test_store_appends_and_answers_current_by_signal_and_count(tmp_path: Path) -> None:
    store = ShadowReceiptStore(tmp_path)
    assert store.path == tmp_path / "accounts" / "shadow" / SHADOW_RECEIPTS_FILENAME
    assert store.latest("ema-shadow-1") is None and store.any_for_account("9LIVE0001") is False

    store.append(_receipt(sessions=1, written_at_ms=1))
    store.append(_receipt(sessions=2, written_at_ms=2))

    assert store.latest("ema-shadow-1") == _receipt(sessions=2, written_at_ms=2)
    assert store.current("ema-shadow-1", configured_signal_hash=SIGNAL, required_sessions=2) == _receipt(sessions=2, written_at_ms=2)
    assert store.current("ema-shadow-1", configured_signal_hash=SIGNAL, required_sessions=3) is None
    assert store.current("ema-shadow-1", configured_signal_hash="c" * 64, required_sessions=1) is None
    assert store.any_for_account("9LIVE0001") is True
    assert len(store.all_for("ema-shadow-1")) == 2


def test_store_refuses_a_tampered_row(tmp_path: Path) -> None:
    store = ShadowReceiptStore(tmp_path)
    store.append(_receipt())
    store.path.write_text(store.path.read_text().replace('"required_sessions":2', '"required_sessions":1'), encoding="utf-8")
    with pytest.raises(ShadowReceiptInvalid):
        store.latest("ema-shadow-1")
```

`tests/services/test_alpaca_live_verdict.py` — add:

```python
def _shadow_runtime() -> ActiveClerkRuntime:
    return ActiveClerkRuntime(authority_kind="shadow", account_id="shadow:9LIVE0001", account_authority_kind="shadow")


@pytest.mark.parametrize("shadow_state", ["none", "in_progress", "complete"])
def test_live_shadow_authority_reports_the_observed_shadow_state(shadow_state: str) -> None:
    verdict = alpaca_live_verdict(settings=_live(), runtime=_shadow_runtime(), now_ms=_NOW, shadow_state=shadow_state)  # type: ignore[arg-type]
    assert verdict.clerk_authority == "shadow"
    assert verdict.observed_account_id == "shadow:9LIVE0001"
    assert verdict.mode_agreement == "agreed"
    assert verdict.final_verdict == "live-unarmed"
    assert verdict.shadow_state == shadow_state
    assert "shadow authority" in verdict.headline


def test_shadow_state_is_not_applicable_on_paper_even_if_supplied() -> None:
    verdict = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW, shadow_state="complete")
    assert verdict.shadow_state == "not_applicable"
```

`tests/routers/test_alpaca_live_verdict_endpoint.py` — add one case: with a shadow runtime installed via `set_active_clerk_runtime` and a receipt appended to `ShadowReceiptStore(settings.clerk_dir)` (patch `clerk_dir` to `tmp_path`), `GET /api/brokers/alpaca/live-verdict` reports `clerk_authority == "shadow"` and `shadow_state == "complete"`.

- [ ] **Step 2: Run them to verify they fail**

Run the three test files. Expected: FAIL on the new module and keyword.

- [ ] **Step 3: `canonical_sha256` and the receipt store**

In `synthetic_activation.py` rename `_digest` to `canonical_sha256` (public; docstring: "sha256 over the canonical JSON of ``payload`` — the one sealing function every isolated-authority record uses") and add it to `__all__`. Create `shadow_receipt.py`:

```python
"""The shadow receipt: sealed, per-instance proof that the shadow gate passed (ADR 0059 D2).

Append-only under ``accounts/shadow/``; every row is sha256-sealed over its
payload, so a receipt is either exactly what the gate wrote or invalid. Slice
6's arming ceremony reads ``ShadowReceiptStore.current`` and refuses to arm
without one (``LIVE_SHADOW_INCOMPLETE``). Dates are the sessions' calendar
opens in ``int64 ms UTC``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.synthetic_activation import canonical_sha256
from app.broker.alpaca.paths import resolve_contained_path
from app.utils.advisory_lock import advisory_file_lock

SHADOW_RECEIPTS_FILENAME = "shadow_receipts.jsonl"
_SHA256_LENGTH = 64


class ShadowReceiptInvalid(ValueError):
    """A shadow receipt row cannot be trusted."""


@dataclass(frozen=True)
class ShadowReceiptSession:
    session_open_ms: int
    shadow_run_id: str
    reconciliation_sha256: str


@dataclass(frozen=True)
class ShadowReceipt:
    schema_version: int
    live_account_id: str
    strategy_instance_id: str
    configured_signal_hash: str
    twin_account_id: str
    twin_strategy_instance_id: str
    required_sessions: int
    sessions: tuple[ShadowReceiptSession, ...]
    written_at_ms: int
    receipt_sha256: str

    @classmethod
    def create(
        cls,
        *,
        live_account_id: str,
        strategy_instance_id: str,
        configured_signal_hash: str,
        twin_account_id: str,
        twin_strategy_instance_id: str,
        required_sessions: int,
        sessions: Sequence[ShadowReceiptSession],
        written_at_ms: int,
    ) -> ShadowReceipt:
        unsigned = {
            "schema_version": 1,
            "live_account_id": require_real_account_id(live_account_id),
            "strategy_instance_id": strategy_instance_id,
            "configured_signal_hash": configured_signal_hash,
            "twin_account_id": require_real_account_id(twin_account_id),
            "twin_strategy_instance_id": twin_strategy_instance_id,
            "required_sessions": required_sessions,
            "sessions": [asdict(session) for session in sessions],
            "written_at_ms": written_at_ms,
        }
        record = cls(
            **{**unsigned, "sessions": tuple(sessions)},
            receipt_sha256=canonical_sha256(unsigned),
        )
        _validate(record)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ShadowReceipt:
        try:
            sessions = tuple(ShadowReceiptSession(**session) for session in payload["sessions"])
            record = cls(**{**payload, "sessions": sessions})
        except (KeyError, TypeError, ValueError) as exc:
            raise ShadowReceiptInvalid("shadow receipt has an invalid shape") from exc
        _validate(record)
        if record.receipt_sha256 != canonical_sha256(_unsigned(record)):
            raise ShadowReceiptInvalid("shadow receipt digest does not verify")
        return record


def _unsigned(record: ShadowReceipt) -> dict[str, Any]:
    payload = asdict(record)
    del payload["receipt_sha256"]
    return payload


def _validate(record: ShadowReceipt) -> None:
    try:
        require_real_account_id(record.live_account_id)
        require_real_account_id(record.twin_account_id)
    except ValueError as exc:
        raise ShadowReceiptInvalid("shadow receipt names a shadow: or sim: account as a real one") from exc
    if record.schema_version != 1 or record.required_sessions < 1 or record.written_at_ms < 0:
        raise ShadowReceiptInvalid("shadow receipt has invalid integer facts")
    if len(record.configured_signal_hash) != _SHA256_LENGTH or any(
        len(session.reconciliation_sha256) != _SHA256_LENGTH or session.session_open_ms < 0 or not session.shadow_run_id
        for session in record.sessions
    ):
        raise ShadowReceiptInvalid("shadow receipt has invalid session facts")


class ShadowReceiptStore:
    """Append-only receipt ledger at ``accounts/shadow/shadow_receipts.jsonl``."""

    def __init__(self, artifacts_root: Path) -> None:
        self._path = resolve_contained_path(artifacts_root, "accounts", "shadow", SHADOW_RECEIPTS_FILENAME)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, receipt: ShadowReceipt) -> None:
        canonical = ShadowReceipt.from_payload(asdict(receipt))
        with advisory_file_lock(self._path):
            if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
                raise ShadowReceiptInvalid("shadow receipt ledger must be a regular file")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            existed = self._path.exists()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(canonical), sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

    def all_for(self, strategy_instance_id: str) -> tuple[ShadowReceipt, ...]:
        return tuple(r for r in self._read_all() if r.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> ShadowReceipt | None:
        receipts = self.all_for(strategy_instance_id)
        return receipts[-1] if receipts else None

    def current(
        self, strategy_instance_id: str, *, configured_signal_hash: str, required_sessions: int
    ) -> ShadowReceipt | None:
        """The latest receipt that still proves the gate for this seal and count, or ``None``."""
        latest = self.latest(strategy_instance_id)
        if (
            latest is None
            or latest.configured_signal_hash != configured_signal_hash
            or len(latest.sessions) < required_sessions
        ):
            return None
        return latest

    def any_for_account(self, live_account_id: str) -> bool:
        return any(r.live_account_id == live_account_id for r in self._read_all())

    def _read_all(self) -> list[ShadowReceipt]:
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise ShadowReceiptInvalid("shadow receipt ledger must be a regular file")
        if not self._path.exists():
            return []
        try:
            return [
                ShadowReceipt.from_payload(json.loads(raw))
                for raw in self._path.read_text(encoding="utf-8").splitlines()
                if raw
            ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShadowReceiptInvalid("shadow receipt ledger cannot be read") from exc


__all__ = [
    "SHADOW_RECEIPTS_FILENAME",
    "ShadowReceipt",
    "ShadowReceiptInvalid",
    "ShadowReceiptSession",
    "ShadowReceiptStore",
]
```

- [ ] **Step 4: The verdict**

`schemas/alpaca_live_verdict.py`: `ClerkAuthority = Literal["sqlite", "synthetic", "shadow", "unavailable", "not_installed"]` (docstring: slice 4 fills `shadow_state`). `services/alpaca_live_verdict.py`: `alpaca_live_verdict(*, settings, runtime, now_ms, shadow_state: ShadowState | None = None)`; the final (agreed live) branch becomes:

```python
    observed_shadow: ShadowState = shadow_state if shadow_state is not None else "none"
    shadow_active = authority == "shadow"
    return AlpacaLiveVerdict(
        ...,
        shadow_state=observed_shadow,
        final_verdict="live-unarmed",
        headline=(
            f"LIVE account {account_id} — shadow authority active, no instance armed"
            if shadow_active
            else f"LIVE account {account_id} — real money, no instance armed"
        ),
        detail=(
            "This is a real-money Alpaca account. Its shadow authority reads it and synthesizes "
            "every fill; nothing is submitted. Arming requires a completed shadow receipt and the "
            "supervised ceremony (ADR 0059)."
            if shadow_active
            else "...unchanged copy..."
        ),
        ...
    )
```

`shadow_state` is ignored on paper (stays `not_applicable`) and on the unknown branch (stays `none`/`not_applicable` as today). Add:

```python
def observe_shadow_state(runtime: ActiveClerkRuntime | None, artifacts_root: Path) -> ShadowState:
    """What the durable shadow evidence says for the installed shadow authority (read-only)."""
    if runtime is None or runtime.authority_kind != "shadow" or runtime.selected_account_id is None:
        return "none"
    live_account_id = runtime.selected_account_id.removeprefix(SHADOW_ACCOUNT_PREFIX)
    if ShadowReceiptStore(artifacts_root).any_for_account(live_account_id):
        return "complete"
    if ShadowSessionLedger(artifacts_root=artifacts_root, account_id=runtime.selected_account_id).has_rows():
        return "in_progress"
    return "none"
```

Router: `shadow_state=None if alpaca_settings is None else observe_shadow_state(runtime, alpaca_settings.clerk_dir)`.

- [ ] **Step 5: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_receipt.py tests/broker/alpaca/clerk/test_synthetic_activation.py tests/broker/alpaca/clerk/test_shadow_activation.py tests/services/test_alpaca_live_verdict.py tests/routers/test_alpaca_live_verdict_endpoint.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/`
Expected: all pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/clerk/shadow_receipt.py PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py PythonDataService/app/schemas/alpaca_live_verdict.py PythonDataService/app/services/alpaca_live_verdict.py PythonDataService/app/routers/brokers.py PythonDataService/tests/broker/alpaca/clerk/test_shadow_receipt.py PythonDataService/tests/services/test_alpaca_live_verdict.py PythonDataService/tests/routers/test_alpaca_live_verdict_endpoint.py && git commit -q -m "feat(shadow): the sealed shadow receipt, and the live verdict reports shadow progress (ADR 0059 D2, D8)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 9: The operator CLI — activate, sessions, receipt

**Files:**
- Create: `PythonDataService/scripts/manage_alpaca_shadow.py`
- Test: `PythonDataService/tests/scripts/test_manage_alpaca_shadow.py`

**Interfaces:**
- Consumes: Task 5 `activate_shadow_clerk_authority`; Task 7 `evaluate_shadow_gate`, `EconomicFillSource.from_database_path`; Task 8 `ShadowReceiptStore`, `ShadowReceipt`; Task 4 `ShadowSessionLedger`; `live_state_binding_repository(root).read(sid)`; `sqlite/writes.confined_account_file`, `sqlite/repository.DB_FILENAME`; `ALPACA_LIVE_CAPABILITIES.extended_hours_window`.
- Produces: `python -m scripts.manage_alpaca_shadow --live-account-id <id> [--artifacts-root P] [--live-state-root P] {activate | sessions ... | receipt ...}`; `main(argv, *, evaluate=evaluate_shadow_gate) -> int` (0 ok; 2 gate not satisfied / twin mismatch; 1 usage or evidence error). JSON on stdout, one object per invocation.

- [ ] **Step 1: Write the failing test**

`tests/scripts/test_manage_alpaca_shadow.py`:

```python
"""The shadow operator CLI activates, lists sessions, and writes the receipt only when the gate holds."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.manage_alpaca_shadow import main
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.services.alpaca_shadow_reconciliation import ShadowGateEvaluation, ShadowSessionVerdict, TwinDayReconciliation


def _evaluation(counted: int, required: int) -> ShadowGateEvaluation:
    sessions = tuple(
        ShadowSessionVerdict(
            session_open_ms=1_000 + i, state="counted", detail="", shadow_run_id="run-1",
            reconciliation=TwinDayReconciliation(1_000 + i, "s", "t", (), (), (), None, __import__("decimal").Decimal("0.01")),
        )
        for i in range(counted)
    )
    return ShadowGateEvaluation(
        live_account_id="9LIVE0001", strategy_instance_id="s", twin_account_id="PA-TEST",
        twin_strategy_instance_id="t", configured_signal_hash="a" * 64, required_sessions=required, sessions=sessions,
    )


def test_activate_writes_the_fence_and_is_idempotent(tmp_path: Path, capsys) -> None:
    argv = ["--live-account-id", "9LIVE0001", "--artifacts-root", str(tmp_path), "--live-state-root", str(tmp_path / "live"), "activate"]
    assert main(argv) == 0
    assert main(argv) == 0
    record = ShadowActivationStore(tmp_path).latest("shadow:9LIVE0001")
    assert record is not None and json.loads(capsys.readouterr().out.splitlines()[-1])["account_id"] == "shadow:9LIVE0001"


def test_receipt_is_written_only_when_the_gate_is_satisfied(tmp_path: Path, capsys) -> None:
    base = ["--live-account-id", "9LIVE0001", "--artifacts-root", str(tmp_path), "--live-state-root", str(tmp_path / "live")]
    twin = ["--strategy-instance-id", "s", "--twin-account-id", "PA-TEST", "--twin-strategy-instance-id", "t", "--required-sessions", "2"]

    assert main([*base, "receipt", *twin], evaluate=lambda **_kw: _evaluation(1, 2)) == 2
    assert ShadowReceiptStore(tmp_path).latest("s") is None
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["satisfied"] is False

    assert main([*base, "receipt", *twin], evaluate=lambda **_kw: _evaluation(2, 2)) == 0
    receipt = ShadowReceiptStore(tmp_path).latest("s")
    assert receipt is not None and len(receipt.sessions) == 2 and receipt.required_sessions == 2
    assert main([*base, "sessions", *twin], evaluate=lambda **_kw: _evaluation(2, 2)) == 0
```

The `evaluate` seam receives keyword arguments exactly as `evaluate_shadow_gate` does; the stub ignores them. The CLI must not open any database when `evaluate` is injected — wire the `FillSource` construction inside the default evaluator path only (a small `_default_evaluate(**kwargs)` that opens the two `EconomicFillSource`s and closes them), so the injected stub bypasses I/O.

- [ ] **Step 2: Run it to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/scripts/test_manage_alpaca_shadow.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: scripts.manage_alpaca_shadow`.

- [ ] **Step 3: Create the CLI**

Model it on `scripts/manage_alpaca_sqlite_clerk.py` (argparse, `sys.stdout.write(json.dumps(..., sort_keys=True) + "\n")`, `main(argv) -> int`). Shape:

```python
"""Operator CLI for the shadow gate (ADR 0059 D2): activate, sessions, receipt.

``activate`` initializes the shadow custody database for one live account and
appends its activation proof — the one write no startup path performs.
``sessions`` judges every trading day since the shadow instance first ran
against its paper twin. ``receipt`` does the same and, when the gate is
satisfied, seals the result into the receipt store.
"""

# _parse_args: global --live-account-id (required), --artifacts-root (default get_alpaca_settings().clerk_dir),
#   --live-state-root (default live_artifacts_root()); subcommands:
#   activate
#   sessions / receipt: --strategy-instance-id, --twin-account-id, --twin-strategy-instance-id (required),
#     --twin-artifacts-root (default: --artifacts-root), --required-sessions (default: settings.live_shadow_sessions;
#     required when settings are not live), --now-ms (default now_ms_utc())
#
# main(argv, *, evaluate=evaluate_shadow_gate):
#   activate → asyncio.run(activate_shadow_clerk_authority(...)); print asdict(record); return 0
#   sessions/receipt →
#     bindings = live_state_binding_repository(live_state_root); shadow_binding = bindings.read(sid); twin_binding = bindings.read(twin_sid)
#       (either None → print {"error": "...binding not found"}; return 1)
#     evaluation = evaluate(live_account_id=..., shadow_binding=..., twin_binding=..., twin_account_id=..., required_sessions=...,
#                           session_ledger=ShadowSessionLedger(artifacts_root=..., account_id=shadow_account_id_for_live_account(...)),
#                           shadow_source=<EconomicFillSource over confined_account_file(artifacts_root, shadow_id, DB_FILENAME)>,
#                           twin_source=<EconomicFillSource over confined_account_file(twin_artifacts_root, twin_account_id, DB_FILENAME)>,
#                           window=ALPACA_LIVE_CAPABILITIES.extended_hours_window, now_ms=...)
#       — the two sources are built lazily by a `_default_evaluate` wrapper around `evaluate_shadow_gate` so an injected
#         evaluator never touches a database; ShadowTwinMismatch → print {"error": reason_code, "detail": ...}; return 2
#     summary = {"satisfied": evaluation.satisfied, "counted": len(evaluation.counted), "required": ..., "sessions": [asdict(v) minus the reconciliation's fill lists, plus reconciliation.report_sha256()]}
#     receipt and evaluation.satisfied → ShadowReceiptStore(artifacts_root).append(ShadowReceipt.create(... sessions=[ShadowReceiptSession(v.session_open_ms, v.shadow_run_id, v.reconciliation.report_sha256()) for v in evaluation.counted], written_at_ms=now_ms)); summary["receipt_sha256"] = ...
#     print summary; return 0 if evaluation.satisfied or command == "sessions" else 2
```

Write the real functions (the block above is the contract, not the code); `Decimal` values in the printed summary go through `default=str`.

- [ ] **Step 4: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/scripts/test_manage_alpaca_shadow.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/`
Expected: pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/scripts/manage_alpaca_shadow.py PythonDataService/tests/scripts/test_manage_alpaca_shadow.py && git commit -q -m "feat(shadow): operator CLI — activate the shadow authority, list sessions, seal the receipt (ADR 0059 D2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 10: Contracts regenerated; the Frontend deploy form offers `shadow`

**Files:**
- Regenerate: `contracts/openapi/...` (whatever `scripts/export_openapi_contract.py` writes), `Frontend/src/app/api/broker.types.ts`
- Modify: `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts`, `.html`, `deploy-execution-section.component.ts`, `.html`, `alpaca-deploy-workflow.fixtures.ts`, `alpaca-deploy-workflow.component.spec.ts`

**Interfaces:**
- Consumes: the Task 6 wire literals; the Task 8 `clerk_authority` literal.
- Produces: the deploy ticket admits `'shadow'`; on a view whose available broker mode is `shadow`, the form defaults to it, labels the submit "Deploy shadow bot", and posts `execution_mode: 'shadow'`; the `paperUnavailableReason` input is renamed `brokerModeUnavailableReason` and serves `paper` and `shadow` alike.

- [ ] **Step 1: Regenerate both contracts**

Run: `.venv/bin/python scripts/export_openapi_contract.py && cd /Users/inkant/learn-ai/Frontend && npm run codegen:openapi`
Commit the regenerated files alone: `git add contracts Frontend/src/app/api/broker.types.ts && git commit -q -m "chore(contracts): regenerate OpenAPI and Frontend types for the shadow deploy mode and clerk authority (ADR 0059 slice 4)" + trailer`.

- [ ] **Step 2: Write the failing spec**

Add to `alpaca-deploy-workflow.fixtures.ts`:

```ts
export const SHADOW_DEPLOY_VIEW: AlpacaPaperDeployView = {
  ...DEPLOY_VIEW,
  account_mode: 'live',
  account_label: 'Alpaca shadow · 9LIVE0001',
  strategies: DEPLOY_VIEW.strategies.map((strategy) => ({
    ...strategy,
    admissible_modes: strategy.admissible_modes.map((mode) => (mode === 'paper' ? 'shadow' : mode)),
  })),
  execution_modes: [
    DEPLOY_VIEW.execution_modes[0],
    { mode: 'shadow', label: 'Shadow', availability: 'available', explanation: 'Synthesized fills against the live account; nothing is submitted.' },
    { mode: 'live', label: 'Live', availability: 'planned', explanation: 'Requires a shadow receipt and arming.' },
  ],
};
```

Add to the spec:

```ts
it('on a shadow view the Shadow mode is offered, selected by default, and submitted', async () => {
  const service = mockService(RECEIPT, SHADOW_DEPLOY_VIEW);
  await renderWorkflow(service);

  expect(screen.queryByRole('radio', { name: /Paper/ })).toBeNull();
  const shadowRadio = screen.getByRole<HTMLInputElement>('radio', { name: /Shadow/ });
  expect(shadowRadio.checked).toBe(true);
  expect(screen.getByRole('button', { name: 'Deploy shadow bot' })).toBeTruthy();

  await fillTicketAndSubmit();  // whatever helper the spec already uses to reach a submit
  expect(service.deploy).toHaveBeenCalledWith(expect.objectContaining({ execution_mode: 'shadow' }));
});
```

(Use the spec's existing helpers for filling the bot name and submitting; the three assertions are the contract.)

- [ ] **Step 3: Run it to verify it fails**

Run: `cd /Users/inkant/learn-ai/Frontend && npx ng test --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts'`
Expected: FAIL (type error on `'shadow'` in the ticket union / the missing radio).

- [ ] **Step 4: The component edits**

`alpaca-deploy-workflow.component.ts`:
- line 66: `executionMode: Extract<DeployExecutionMode['mode'], 'dry_run' | 'paper' | 'shadow'>;`
- add `protected readonly brokerMode = computed<'paper' | 'shadow'>(() => this.currentView()?.execution_modes.some((mode) => mode.mode === 'shadow' && mode.availability === 'available') ? 'shadow' : 'paper');`
- rename `paperUnavailableReason` → `brokerModeUnavailableReason`, body: `const strategy = this.selectedStrategy(); if (strategy === null || strategy.admissible_modes.includes(this.brokerMode())) return null; return strategy.blocked_explanation ?? null;` and update its comment ("the Paper/Shadow option").
- readiness reason (≈line 366): `this.ticket().executionMode === 'dry_run' ? this.dryRunUnavailableReason() : this.brokerModeUnavailableReason()`.
- `setExecutionMode`: guard `if (mode !== 'dry_run' && mode !== 'paper' && mode !== 'shadow') return;` and `if (mode !== 'dry_run' && this.brokerModeUnavailableReason() !== null) return;`.
- default mode: where the ticket is first built with `executionMode: 'paper'`, and where the loaded view is applied, add one effect/step: if the ticket's mode is `'paper'` and `this.brokerMode() === 'shadow'`, set it to `'shadow'` (a `linkedSignal` or the existing view-applied effect — follow the file's pattern).
- the submit label: find the `'Deploy paper bot'` / `Deploy ${...} bot` author and derive the noun from the selected mode (`dry_run` → existing wording, `paper` → "paper", `shadow` → "shadow").
- template: `[brokerModeUnavailableReason]="brokerModeUnavailableReason()"`.

`deploy-execution-section.component.ts`: rename the input `paperUnavailableReason` → `brokerModeUnavailableReason`; `strategyUnavailableReason(mode)`: `if (mode.mode === 'paper' || mode.mode === 'shadow') return this.brokerModeUnavailableReason();`. Update its `.html` if it references the input.

- [ ] **Step 5: Run the Frontend checks**

Run: `cd /Users/inkant/learn-ai/Frontend && npx ng test --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts' && npx ng test --include='src/app/components/broker/broker-deploy-page/deploy-execution-section.component.spec.ts' && npx eslint src/ --max-warnings 0 && npm run codegen:check`
(Run the second spec only if that file exists.) Expected: green.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai && git add Frontend/src/app/components/broker/broker-deploy-page && git commit -q -m "feat(frontend): the deploy form offers Shadow on a shadow authority and posts execution_mode shadow (ADR 0059 D2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 11: Reference note, registries, vocabulary

**Files:**
- Create: `docs/references/alpaca-shadow-authority.md`
- Modify: `docs/math-sources-of-truth.md` (row 76's consumer text; one new row under "Broker session and order anchoring (ADR 0059 D5)" or a new "Shadow gate (ADR 0059 D2)" subsection), `docs/architecture/engine-authority-map.md` (one row), `CONTEXT.md` (**Paper twin**)
- Test: `tests/contracts` (link resolution)

- [ ] **Step 1: The reference note**

`docs/references/alpaca-shadow-authority.md` — sections, each written in full from this plan's facts and rulings:

1. **What was built** — the four ADR D2 deliverables, one paragraph each, naming the modules (`shadow_broker.py`, `shadow_activation.py`, `shadow_sessions.py`, `shadow_receipt.py`, `alpaca_shadow_reconciliation.py`, `scripts/manage_alpaca_shadow.py`).
2. **Worlds and paths** — `shadow:<live_account_id>` custody at `accounts/alpaca/shadow:<id>/` (the `sim:` mechanism; ruling R2), the fence and receipts under `accounts/shadow/`, per-instance evidence at `accounts/alpaca/shadow-evidence:<sid>/`; the composite read port (ruling R3) and the sentence "a real position on the live account is invisible to the shadow sweep and visible on the account card, which reads the broker directly".
3. **Fill models** — `decision_bar_close` inside the regular session; `limit_touch` for extended legs; cancel at the declared window's close; settlement on read with one bucket of slack (ruling R5); `cancel` semantics (R6); provenance in `SynthesizedAnchor`.
4. **Cold start** — the namespace check, its page bound, the two refusals.
5. **Session accounting** — the three journal rows; what makes a day complete; the "opened late" rule.
6. **Twin reconciliation** — the pairing rule, the gating set, `fill_price_atol = $0.01` reported-not-gated with the ADR sentence that justifies it; the seal-identity rule (`twins_agree`).
7. **Receipt** — fields, sealing, `current()`; what slice 6 will require.
8. **Operator recipe** — the three CLI commands in order, with the paper twin running on the paper host over the same days; `.env` needs `ALPACA_LIVE_SHADOW_SESSIONS`; how the verdict banner reads (`shadow authority active`).
9. **Validation** — the test files of Tasks 1–9.
10. **Follow-ups (not this slice)** — a paginated order-history walk for accounts past 500 orders; the decision-bar join for the twin comparison (today: sequence + shape); overnight session; the live verdict per-instance shadow progress; slice 6 arming consumes `ShadowReceiptStore.current`.

Links from the repo root (`docs/references/...`, `PythonDataService/app/...`).

- [ ] **Step 2: Registries and vocabulary**

`docs/math-sources-of-truth.md`: in row 76 replace "consumer: slice-4 shadow port" with "consumer: `shadow_broker.ShadowOrderBook._settle_locked`"; add a row: `| Shadow-vs-paper-twin trade reconciliation | \`PythonDataService/app/services/alpaca_shadow_reconciliation.py::reconcile_twin_day\` pairs fills by sequence and shape under the repo divergence taxonomy; gating set {DECISION, DIRECTION, QUANTITY}; \`fill_price_atol = $0.01\` reported, never gated | none — the taxonomy enum is \`app/research/parity/qc_reconciler.py::DivergenceCategory\` (kept in lockstep per numerical-rigor.md) | ADR 0059 D2; [alpaca-shadow-authority](docs/references/alpaca-shadow-authority.md) | \`PythonDataService/tests/services/test_alpaca_shadow_reconciliation.py\` | canonical — 4-field provenance block present |`.

`docs/architecture/engine-authority-map.md`: one row modelled on row 21 — "Alpaca shadow execution (ADR 0059 D2)": `active_authority._select_shadow_clerk_runtime` composes `shadow_broker.py` + the shadow `ClerkSqliteRepository` + per-instance `shadow-evidence:` ledgers; the runner consumes the same Signal Program and Action Plan; it cannot bind the live trade port or write a real fill; validated by the Task 3/5/7 tests.

`CONTEXT.md` § "Live account, shadow, and risk envelope": add after **Shadow gate**:

```
- **Paper twin** — the sealed instance on the paper account that shares a
  shadow instance's configured signal, action plan, size and carryover
  policy and runs over the same sessions. Its identity is the configured
  signal, never the instance id or the account. _Avoid_: control run,
  baseline bot, mirror bot
```

- [ ] **Step 3: Run the docs contract and commit**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q -p no:cacheprovider`
Expected: pass. Then:

```bash
cd /Users/inkant/learn-ai && git add docs/references/alpaca-shadow-authority.md docs/math-sources-of-truth.md docs/architecture/engine-authority-map.md CONTEXT.md && git commit -q -m "docs(shadow): reference note, registry rows and the Paper twin term for ADR 0059 slice 4

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 12: Final gates on the branch tree

- [ ] **Step 1: Contracts are current** — `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check && cd /Users/inkant/learn-ai/Frontend && npm run codegen:check`. If either reports drift, regenerate and commit `chore(contracts): ...`.
- [ ] **Step 2: Lint at project scope** — `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/ruff check app/ tests/ scripts/ && cd /Users/inkant/learn-ai/Frontend && npx eslint src/ --max-warnings 0`.
- [ ] **Step 3: Targeted tests** — `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker tests/services/test_bot_binding_authority_source_bars.py tests/services/test_run_replay_proof_service.py tests/services/bot_runner tests/services/test_alpaca_live_verdict.py tests/services/test_alpaca_shadow_reconciliation.py tests/services/test_run_admission.py tests/services/test_sqlite_clerk_compat.py tests/services/test_source_bar_ledger.py tests/routers tests/scripts tests/contracts -q -p no:cacheprovider` (substitute file names that exist), plus the two Frontend specs from Task 10.
- [ ] **Step 4: Golden receipts are byte-stable** — `git status --short PythonDataService/artifacts PythonDataService/tests/fixtures` shows nothing; the three sealed artifacts are untouched (`git diff --stat master -- PythonDataService/app/lean_sidecar/trading_calendar.py PythonDataService/app/utils/timestamps.py PythonDataService/app/engine/consolidators/trade_bar_consolidator.py` is empty).
- [ ] **Step 5: Report** the gate outputs in the ledger; no commit unless Step 1 regenerated.

## Rulings recorded while planning (carry into the SDD ledger)

- **R1 — The shadow authority is the primary runtime of a live boot.** `select_active_clerk_runtime` composes it (kind `shadow`, id `shadow:<live_account_id>`) where it refused `LIVE_ACCOUNT_REFUSED`; `real_live` custody stays unconstructible until slice 7. Cost if wrong: the shadow world moves to an account-keyed non-primary registration — one function's return path.
- **R2 — Custody isolation follows the `sim:` mechanism.** The shadow repository is `ClerkSqliteRepository` for `shadow:<id>` (directory `accounts/alpaca/shadow:<id>/`, distinct by prefix from the live account's); the fence and the receipts live under `accounts/shadow/`. A second repository root would need a parallel established-accounts registry, recovery-lock and reset registries. The ADR's "under `accounts/shadow/<live_account_id>/`" is honoured by the fence/receipt directory. Cost if wrong: a directory move.
- **R3 — The shadow read port is a composite.** Account, clock, activities, assets, portfolio history, capabilities: the live read port. Positions and orders: projected from the synthesized ledger. The Clerk's sweep reconciles the world it custodies; real positions and synthesized fills cannot share one reconciliation (the ADR rejected writing shadow fills into live custody for exactly this reason). A real position on the live account is invisible to the shadow sweep and visible on the account card. Cost if wrong: a divergence a later slice must surface differently.
- **R4 — Fill model by leg shape.** A leg without `extended_hours` fills at the decision bar's close (`decision_bar_close`, via `immediate_fill_price`; a non-marketable regular limit cancels on the spot, R9 of slice 3); an extended-hours leg rests under `limit_touch` and cancels at the declared window's close of the decision's trading day. Cost if wrong: a fill-model constant.
- **R5 — Resting orders settle on read, from the bound bar's own evidence ledger, with one bucket of slack.** The order record stores the decision bar's identity and evidence namespace; every read runs `limit_touch_fill` over the bars retained after the decision bar; with no touch, the order cancels only once `now ≥ cancel_at + (decision_bar.end − decision_bar.start)`, so the closing bucket has been retained first. No background task: the sweep's reads are the clock. Cost if wrong: a settlement delay of one bucket after the declared close.
- **R6 — `cancel` marks a resting synthesized order cancelled; it never contacts Alpaca.** The ADR's "no-op" means no vendor call; the EXIT machine's cancel-and-prove needs a terminal answer. Cancel on a terminal order is a no-op. Cost if wrong: none observable at the vendor.
- **R7 — Shadow evidence is instance-scoped under `shadow-evidence:<sid>`,** chosen by one function (`evidence_account_id_for`) that the binding authority and the replay proof both use; the world comes from the primary authority's kind (binding authority) or the binding's sealed account (replay), never guessed. Cost if wrong: a prefix rename.
- **R8 — Synthesized executions are typed by namespace and authority kind,** as the sim world's are: `shadow-order:` / `shadow-execution:` ids, `broker="shadow"`, `authority_kind="shadow"` and `simulated=True` on every panel row, and the fill model plus decision bar in the durable `SynthesizedAnchor`. No new column on the hash-chained facts. Cost if wrong: an `execution_source` column later, behind the omit-when-default shim.
- **R9 — Cold-start verification reads what the port can see.** The newest page of the whole history plus every open order; any Clerk-minted `client_order_id` (`parse_order_ref` succeeds) is `SHADOW_NAMESPACE_POISONED`; a full page is `SHADOW_NAMESPACE_UNPROVEN` (the sweep's own 500-row posture). Cost if wrong: an account past 500 historical orders cannot shadow until a paginated walk exists (follow-up).
- **R10 — A shadow session counts when** the sweep's journal shows the day opened before the instance's decision session opened, closed clean after it closed, with no non-clean pass; one run of the instance spanned the whole decision session; and the twin reconciliation has no gating divergence. Cost if wrong: a day is not counted and the operator runs another.
- **R11 — Twin reconciliation gates on decisions and reports prices.** Pairing by sequence and shape under the repo taxonomy; gating set {DECISION, DIRECTION, QUANTITY}; `fill_price_atol = $0.01` reported, never gated (the ADR: synthesized fills are optimistic by construction). Twin identity is the configured signal, plan, size and carryover policy — never the instance id, the account, or the outer seal hash. Cost if wrong: a decision-time join is added later (follow-up).
- **R12 — The receipt is an append-only, sealed JSONL under `accounts/shadow/`**, `current()` keyed on the configured-signal hash and the required count; the verdict's `shadow_state` is account-level: `complete` iff a receipt exists for the live account, `in_progress` iff the session journal has rows, else `none`. Cost if wrong: per-instance progress on the verdict (follow-up).
- **R13 — Operator surface is a CLI, not an endpoint.** `scripts.manage_alpaca_shadow {activate,sessions,receipt}` reads both custody databases `mode=ro` (no lease) and appends to JSONL stores; the only new HTTP fact is `shadow_state` on the existing verdict. Cost if wrong: an endpoint wraps the same functions.
- **R14 — The gates a shadow run needs admit the shadow world by kind, never by account mode.** Deploy view/request, deploy readiness, the account posture, `get_alpaca_clerk`, `run_trade_bot`, the facade. Everything else keeps refusing on `account_mode != "paper"`, which a shadow facade answers `"live"`; dev reset refuses the `shadow:` namespace by name (D10); the corpus gate is untouched (D11). The wire `execution_mode` gains an honest `shadow` value now rather than reusing `paper` — a label that lies is the ADR 0011 failure mode. Cost if wrong: slice 7 renames one literal.
- **R15 — The shadow runtime installs a null trade-updates sink.** The live execution stream is not shadow custody evidence (ADR 0042); the consumer still runs so the stream-health gate samples the live websocket and the hold sync has both providers. Cost if wrong: an external order on the live account is never journaled by the shadow world — by design.

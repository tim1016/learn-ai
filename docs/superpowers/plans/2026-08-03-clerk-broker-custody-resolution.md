# Clerk ↔ Broker Custody Resolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an account-level surface on the Alpaca Accounts page that diagnoses, explains, and resolves Clerk↔broker divergences behind a required operator comment, syncing the Clerk to the broker.

**Architecture:** A read-only `GET …/clerk/custody-diagnosis` composes existing pure divergence functions (`derive.has_missing_intent`, `exposure.project_expected_account_exposure`, `derive.hold_state`, `derive.account_freeze_state`) into a structured `CustodyDiagnosis` (per-symbol delta + backend-authored explanation/causes + resolution plan + a `snapshot_version` guard). A `POST …/clerk/resolve` re-diagnoses against that snapshot, then orchestrates the existing recovery verbs (`reconcile_once` → `record_inventory_baseline(operator, reason)` → `clear_hold(operator, reason)`), journaling the operator comment onto the existing `OrderJournalEntry.operator`/`.reason` slots (no storage schema change). The frontend adds one `<app-alpaca-custody-resolution>` child to the 41-line `AlpacaDeskComponent`, with a required-comment + typed-token confirm dialog. Slice 3 un-dead-wires the same comment on the per-bot Operator lens.

**Tech Stack:** Python 3.11 / FastAPI / Pydantic v2 (data-plane `:8000`); Angular 22 standalone + signals + `resource()` + PrimeNG + Vitest; pytest (`asyncio_mode = auto`).

## Global Constraints

- **Timestamps are `int64 ms UTC`** on every wire/stored field (`*_at_ms`, `observed_at_ms`, `recorded_at_ms`); the UI renders them only through the shared `TimestampDisplayComponent` (`src/app/shared/timestamp`), instants viewer-local. No ISO/`DateTime`/naive-`datetime` on the wire.
- **Operator prose is backend-authored** (`explanation`, `possible_causes`, plan text, `blocked_reason`) and rendered verbatim/unpiped. **Code-like tokens** (`kind`, `scope`, `resolution_step`, reason codes) render through the `receiptLabel` pipe. **Opaque tokens** (`evidence_refs`, `receipt_id`) are preserved exactly.
- **No Clerk journal schema change.** Reuse `OrderJournalEntry.operator` + `.reason` (validators already require them on `HOLD_CLEARED` / `BROKER_EVIDENCE_BASELINE`).
- **Boundary validation:** all request models `model_config = ConfigDict(extra="forbid")`; `reason` is required, non-blank (`field_validator` that strips + rejects blank, copied from `ClearHoldRequest._reason_is_nonblank`, `brokers.py:255-262`).
- **Auth:** `GET …/custody-diagnosis` is a protected read (no per-route dep, matching `get_clerk_status` at `brokers.py:265`); `POST …/resolve` is a control mutation with `dependencies=[Depends(require_data_plane_control_secret)]` (matching `clear_clerk_hold` at `brokers.py:280`). Response fields are snake_case.
- **No new dependencies.** Reuse PrimeNG `Textarea`, Angular `resource()`, the existing `ActionReceiptView`/`PanelActionReceiptComponent`, and the `<dialog>` pattern from `account-desk-recovery-confirm-dialog`.
- **Angular:** standalone (no `standalone: true`), `ChangeDetectionStrategy.OnPush`, signals, `input()`/`output()`, `inject()`, `@if`/`@for` with `track`, `[class.x]`/`[style.x]` (never `ngClass`/`ngStyle`), AXE / WCAG AA.
- **Python:** full type hints, `async def` for I/O, `logging` (no `print`), no bare `except`. Lint clean at project scope: `ruff check PythonDataService/app/ PythonDataService/tests/`.
- **Contract gates:** regenerate the committed OpenAPI contract after adding endpoints (CI "Verify committed OpenAPI contract"); add any new code-like `receiptLabel` tokens to the vocabulary snapshot + contract test.

**Commands** — Python tests: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/ -v` (or a sibling container for large runs). Frontend tests: `podman exec my-frontend npx ng test`. Type-check: `podman exec my-frontend npx tsc --noEmit`. Lint: `npx eslint Frontend/src/ --max-warnings 0` and `ruff check PythonDataService/app/ PythonDataService/tests/`.

## File Structure

**Backend (create):**
- `PythonDataService/app/broker/alpaca/clerk/diagnosis.py` — the pure `diagnose_custody(entries, orders, positions) -> tuple[CustodyDivergence, ...]` fold, the backend-authored copy map, `custody_snapshot_version(...)`, and all new Pydantic models (`CustodyDiagnosis`, `CustodyDivergence`, `CustodyPositionDelta`, `CustodyResolutionStep`, `CustodyResolutionRequest`, `CustodyResolutionReceipt`, `CustodyResolutionStepResult`).
- `PythonDataService/tests/broker/alpaca/clerk/test_custody_diagnosis.py` — pure-fold + clerk-method + endpoint tests.
- `PythonDataService/tests/broker/alpaca/clerk/test_custody_resolve.py` — resolve orchestration + endpoint tests.

**Backend (modify):**
- `PythonDataService/app/broker/alpaca/clerk/clerk.py` — add `custody_diagnosis()` and `resolve_custody(...)` methods.
- `PythonDataService/app/broker/alpaca/clerk/__init__.py` — re-export the new models (mirroring `ClerkStatus`).
- `PythonDataService/app/routers/brokers.py` — add the two endpoints.
- `PythonDataService/app/services/broker_v2_panel/panel_data_source.py` — thread `reason` into `_clear_hold` / `_record_inventory_baseline` (Slice 3).
- `PythonDataService/app/services/broker_v2_panel/action_execution_service.py` — pass `request.reason` to the performer (Slice 3).

**Frontend (create):**
- `Frontend/src/app/components/brokers/alpaca-desk/alpaca-custody-resolution.component.{ts,html,scss}` — the account custody card (4 states).
- `Frontend/src/app/components/brokers/alpaca-desk/custody-resolution-confirm-dialog.component.{ts,html,scss}` — required-comment + typed-token confirm.
- Co-located `*.spec.ts` for both.

**Frontend (modify):**
- `Frontend/src/app/services/brokers.service.ts` — add `getCustodyDiagnosis` + `resolveCustody`.
- `Frontend/src/app/api/alpaca.types.ts` — add generated-type aliases.
- `Frontend/src/app/components/brokers/alpaca-desk/alpaca-desk.component.{ts,html}` — slot the new child after `<app-alpaca-hold-banner />`.
- (Slice 3) `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.service.ts`, `operator-readiness.component.*`, `panel-action-button.component.*`.

---

# SLICE 1 — Read-only custody diagnosis (end-to-end)

Delivers: the Accounts page shows the divergence (delta + explanation + causes + plan) in four honest states, read-only. No mutation yet.

## Task 1.1: Pure diagnosis fold + models + copy + snapshot

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/diagnosis.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_custody_diagnosis.py`

**Interfaces:**
- Consumes (existing, verbatim): `exposure.project_expected_account_exposure(entries) -> dict[str, float]`; `exposure.signed_broker_position_quantity(position) -> float`; `derive.inflight_order_symbols(entries) -> frozenset[str]`; `derive.hold_state(entries) -> HoldState`; `derive.account_freeze_state(entries) -> AccountFreezeState`; `derive.latest_reconciliation(entries) -> ReconciliationSummary | None`; `derive.unresolved_intents(entries) -> list[...]`; `reconcile.order_ref_namespace_matches` / `derive.unexplained_order_ids`.
- Produces: `diagnose_custody(entries, orders, positions, *, namespaces) -> tuple[CustodyDivergence, ...]`; `custody_snapshot_version(entries, orders, positions) -> str`; the models below.

- [ ] **Step 1: Write the failing test** (`test_custody_diagnosis.py`)

```python
from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
# Reuse the broker/position/order fakes already in this dir:
from tests.broker.alpaca.clerk.test_clerk_reconciliation import _position  # helper builder


def test_attribution_mismatch_reports_per_symbol_delta() -> None:
    # Journal expects nothing (no baseline, no terminal orders); broker holds 1 SPY.
    entries: list[OrderJournalEntry] = []
    positions = [_position(symbol="SPY", quantity=1, side="long")]

    divergences = diagnosis.diagnose_custody(
        entries, orders=[], positions=positions, namespaces=frozenset()
    )

    assert len(divergences) == 1
    d = divergences[0]
    assert d.kind == "exposure_attribution_mismatch"
    assert d.state == "resolvable_now"
    assert d.resolution_step == "record_inventory_baseline"
    assert d.position_deltas == (
        diagnosis.CustodyPositionDelta(
            symbol="SPY", clerk_attributed_qty=0.0, broker_observed_qty=1.0
        ),
    )
    assert d.possible_causes  # backend-authored, non-empty
    assert d.explanation  # backend-authored prose


def test_flat_and_reconciled_account_has_no_divergence() -> None:
    assert diagnosis.diagnose_custody([], orders=[], positions=[], namespaces=frozenset()) == ()
```

- [ ] **Step 2: Run to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_custody_diagnosis.py -v`
Expected: FAIL — `ModuleNotFoundError: app.broker.alpaca.clerk.diagnosis`.

- [ ] **Step 3: Write `diagnosis.py`** — models + copy + the fold.

```python
"""Read-only Clerk↔broker custody diagnosis (account-scoped).

Pure projection: reuse the existing divergence folds (``derive``/``exposure``)
to produce a structured, backend-authored diagnosis the Accounts page renders
verbatim. Never mutates the journal or the broker.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.broker.alpaca.clerk import derive, exposure, reconcile
from app.broker.alpaca.clerk.models import (
    BrokerOrder,
    BrokerPosition,
    OrderJournalEntry,
)

CustodyDivergenceKind = Literal[
    "exposure_attribution_mismatch",
    "exposure_hold",
    "stale_reconciliation",
    "needs_review",
]
CustodyDivergenceState = Literal[
    "resolvable_now", "blocked_on_prerequisite", "needs_review"
]
CustodyActionId = Literal["reconcile_now", "record_inventory_baseline", "clear_hold"]


class CustodyPositionDelta(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    clerk_attributed_qty: float
    broker_observed_qty: float


class CustodyDivergence(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: CustodyDivergenceKind
    state: CustodyDivergenceState
    explanation: str
    possible_causes: tuple[str, ...]
    position_deltas: tuple[CustodyPositionDelta, ...] = ()
    resolution_step: CustodyActionId | None = None
    prerequisite_detail: str | None = None
    evidence_refs: tuple[str, ...] = ()


class CustodyResolutionStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: CustodyActionId
    scope: Literal["account", "bot", "broker"]
    mutates: bool


class CustodyDiagnosis(BaseModel):
    model_config = ConfigDict(frozen=True)
    broker: str
    account_id: str
    in_sync: bool
    observed_at_ms: int
    snapshot_version: str
    resolution_posture: Literal["paper", "live"] = "paper"
    resolvable: bool = False
    blocked_reason: str | None = None
    divergences: tuple[CustodyDivergence, ...] = ()
    resolution_plan: tuple[CustodyResolutionStep, ...] = ()


# ── Backend-authored copy (rendered verbatim by the client) ─────────────────
_CAUSES: dict[CustodyDivergenceKind, tuple[str, ...]] = {
    "exposure_attribution_mismatch": (
        "A bot process was terminated mid-run before its fill was journaled.",
        "An unclean shutdown interrupted the reconciliation sweep.",
        "A manual or foreign order changed the position outside bot custody.",
        "A broker fill landed after the Clerk's last durable snapshot.",
    ),
    "exposure_hold": (
        "An order this account did not submit appeared at the broker.",
        "A prior clear-hold was issued while the foreign order persisted, so the "
        "next sweep re-raised the hold.",
    ),
    "stale_reconciliation": (
        "The broker was unreachable during the last reconciliation sweep.",
        "The data-plane restarted before a fresh sweep completed.",
    ),
    "needs_review": (
        "The Clerk submitted an order the broker reports neither as working nor "
        "filled — its true outcome cannot be proven automatically.",
    ),
}
_EXPLANATION: dict[CustodyDivergenceKind, str] = {
    "exposure_attribution_mismatch": (
        "The broker holds exposure the Clerk cannot map to a recorded intent. "
        "Adopting the broker's observed inventory as the account baseline "
        "restores exact custody."
    ),
    "exposure_hold": (
        "The Clerk raised an exposure hold and is refusing new submissions until "
        "an operator confirms the account is safe."
    ),
    "stale_reconciliation": (
        "The Clerk could not establish current order and exposure truth from a "
        "fresh broker observation. Reconcile once the broker is reachable."
    ),
    "needs_review": (
        "An unresolved submission cannot be mapped to any broker outcome. This "
        "needs manual review before any custody cutover."
    ),
}


def custody_snapshot_version(
    entries: list[OrderJournalEntry],
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
) -> str:
    """Stable hash of the salient custody state (the resolve concurrency guard)."""
    payload = {
        "expected": exposure.project_expected_account_exposure(entries),
        "observed": {
            p.symbol.upper(): exposure.signed_broker_position_quantity(p)
            for p in positions
            if p.quantity != 0
        },
        "hold": derive.hold_state(entries).active,
        "working_orders": sorted(
            o.order_id
            for o in orders
            if o.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def diagnose_custody(
    entries: list[OrderJournalEntry],
    *,
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
    namespaces: frozenset[str],
) -> tuple[CustodyDivergence, ...]:
    """Project current Clerk↔broker divergences (pure; no mutation)."""
    divergences: list[CustodyDivergence] = []

    hold = derive.hold_state(entries)
    if hold.active:
        divergences.append(
            CustodyDivergence(
                kind="exposure_hold",
                state="resolvable_now",
                explanation=_EXPLANATION["exposure_hold"],
                possible_causes=_CAUSES["exposure_hold"],
                resolution_step="clear_hold",
                evidence_refs=tuple(sorted(derive.unexplained_order_ids(entries))),
            )
        )

    deltas = _attribution_deltas(entries, positions)
    if deltas:
        unresolved = derive.unresolved_intents(entries)
        working = [
            o
            for o in orders
            if o.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ]
        state: CustodyDivergenceState = "resolvable_now"
        prerequisite: str | None = None
        if working:
            state = "blocked_on_prerequisite"
            prerequisite = (
                f"{len(working)} working order(s) are open. Cancel or settle them "
                "before adopting a baseline."
            )
        elif unresolved:
            state = "blocked_on_prerequisite"
            prerequisite = (
                f"{len(unresolved)} unresolved submission(s) must reconcile before "
                "a baseline cutover."
            )
        divergences.append(
            CustodyDivergence(
                kind="exposure_attribution_mismatch",
                state=state,
                explanation=_EXPLANATION["exposure_attribution_mismatch"],
                possible_causes=_CAUSES["exposure_attribution_mismatch"],
                position_deltas=deltas,
                resolution_step="record_inventory_baseline",
                prerequisite_detail=prerequisite,
            )
        )

    return tuple(divergences)


def _attribution_deltas(
    entries: list[OrderJournalEntry], positions: list[BrokerPosition]
) -> tuple[CustodyPositionDelta, ...]:
    """Per-symbol (attributed vs observed) drift, skipping in-flight symbols.

    Mirrors the comparison in ``derive.has_missing_intent`` but returns the
    concrete deltas instead of a boolean.
    """
    expected = exposure.project_expected_account_exposure(entries)
    inflight = derive.inflight_order_symbols(entries)
    observed: dict[str, float] = {}
    for position in positions:
        symbol = position.symbol.upper()
        if symbol in inflight:
            continue
        observed[symbol] = observed.get(
            symbol, 0.0
        ) + exposure.signed_broker_position_quantity(position)

    deltas: list[CustodyPositionDelta] = []
    for symbol in sorted(set(expected) | set(observed)):
        if symbol in inflight:
            continue
        e = expected.get(symbol, 0.0)
        o = observed.get(symbol, 0.0)
        if abs(o - e) > 1e-9:
            deltas.append(
                CustodyPositionDelta(
                    symbol=symbol, clerk_attributed_qty=e, broker_observed_qty=o
                )
            )
    return tuple(deltas)
```

Note: `BrokerOrder`/`BrokerPosition` are already imported into `models.py`; import them from there (or their canonical module) to match existing usage. If `_position` is not importable from the test module, construct `BrokerPosition(...)` directly using its fields (`symbol`, `quantity`, `side`, `average_entry_price`, `market_value`, `cost_basis`, `current_price`, `unrealized_pl`, `observed_at_ms`, `broker`).

- [ ] **Step 4: Run to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_custody_diagnosis.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Add the resolution-plan + request/receipt models** to `diagnosis.py` (needed by Tasks 1.2 and 2.1). Append:

```python
class CustodyResolutionStepResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: str
    message: str


class CustodyResolutionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)
    broker: str
    account_id: str
    resolved: bool
    receipt_id: str
    recorded_at_ms: int
    steps_executed: tuple[CustodyResolutionStepResult, ...] = ()
    in_sync: bool = False
    remaining_divergences: tuple[CustodyDivergence, ...] = ()


class CustodyResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=512)
    snapshot_version: str = Field(min_length=1, max_length=128)
    confirmation_token: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("reason")
    @classmethod
    def _reason_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


def resolution_plan(
    divergences: tuple[CustodyDivergence, ...],
) -> tuple[CustodyResolutionStep, ...]:
    """Ordered plan for the resolvable divergences: reconcile → baseline → clear-hold."""
    steps: list[CustodyResolutionStep] = []
    kinds = {d.kind for d in divergences if d.state == "resolvable_now"}
    if kinds & {"exposure_attribution_mismatch"}:
        steps.append(CustodyResolutionStep(action_id="reconcile_now", scope="account", mutates=False))
        steps.append(
            CustodyResolutionStep(action_id="record_inventory_baseline", scope="account", mutates=True)
        )
    if "exposure_hold" in kinds:
        steps.append(CustodyResolutionStep(action_id="clear_hold", scope="account", mutates=True))
    return tuple(steps)
```

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/diagnosis.py PythonDataService/tests/broker/alpaca/clerk/test_custody_diagnosis.py
git commit -m "feat(clerk): pure custody-diagnosis fold + models"
```

## Task 1.2: Clerk `custody_diagnosis()` method + endpoint

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/clerk.py` (add method), `PythonDataService/app/broker/alpaca/clerk/__init__.py` (re-export models), `PythonDataService/app/routers/brokers.py` (add route)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_custody_diagnosis.py` (append clerk + endpoint tests)

**Interfaces:**
- Consumes: `diagnosis.diagnose_custody`, `diagnosis.resolution_plan`, `diagnosis.custody_snapshot_version`, the clerk's `_read.list_orders`/`_read.list_positions`, `self._known_namespaces(journal)`, `self.read_journal_entries()`.
- Produces: `AlpacaClerk.custody_diagnosis() -> CustodyDiagnosis`; `GET /api/brokers/{broker}/clerk/custody-diagnosis`.

- [ ] **Step 1: Write the failing clerk-method test** (append to `test_custody_diagnosis.py`, Style A direct-clerk — reuse the `_clerk_root` fixture, `_FakeBroker`, `_position` helpers from `test_clerk_reconciliation.py`):

```python
from app.broker.alpaca.clerk import AlpacaClerk
from tests.broker.alpaca.clerk.test_clerk_reconciliation import _FakeBroker, _fixed_clock, _position


async def test_custody_diagnosis_reports_missing_intent_mismatch() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is False
    assert result.resolvable is True
    assert [s.action_id for s in result.resolution_plan] == [
        "reconcile_now",
        "record_inventory_baseline",
    ]
    assert result.divergences[0].kind == "exposure_attribution_mismatch"
    assert result.divergences[0].position_deltas[0].broker_observed_qty == 1.0
    assert result.snapshot_version  # non-empty guard token


async def test_custody_diagnosis_flat_account_is_in_sync() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is True
    assert result.divergences == ()
    assert result.resolution_plan == ()
```

(If `_FakeBroker` in the existing file takes only `orders=`, extend it in that test file to also accept `positions=` and return them from `list_positions`; this is a test-fake change, safe to make in the plan.)

- [ ] **Step 2: Run to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_custody_diagnosis.py -v -k custody_diagnosis`
Expected: FAIL — `AttributeError: 'AlpacaClerk' object has no attribute 'custody_diagnosis'`.

- [ ] **Step 3: Add `custody_diagnosis()` to `clerk.py`** (place near `status()`, `clerk.py:490`; read fresh broker without the intake lock like `_reconcile_with_proof`, then read the journal under the lock):

```python
    async def custody_diagnosis(self) -> diagnosis.CustodyDiagnosis:
        """Read-only Clerk↔broker custody diagnosis (no journal/broker mutation).

        Reads the journal and a fresh broker snapshot, then projects the
        structured divergences, the resolution plan, and the snapshot guard.
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            entries = journal.read_entries()
            namespaces = self._known_namespaces(journal)
        orders, positions = await asyncio.gather(
            self._read.list_orders(status="all", limit=500),
            self._read.list_positions(),
        )
        divergences = diagnosis.diagnose_custody(
            entries, orders=orders, positions=positions, namespaces=namespaces
        )
        plan = diagnosis.resolution_plan(divergences)
        blocked = next(
            (d.prerequisite_detail for d in divergences if d.state == "blocked_on_prerequisite"),
            None,
        )
        return diagnosis.CustodyDiagnosis(
            broker=self.broker_id,
            account_id=account_id,
            in_sync=not divergences,
            observed_at_ms=self._clock(),
            snapshot_version=diagnosis.custody_snapshot_version(entries, orders, positions),
            resolvable=bool(plan),
            blocked_reason=blocked,
            divergences=divergences,
            resolution_plan=plan,
        )
```

Add `from app.broker.alpaca.clerk import diagnosis` at the top of `clerk.py` (or `from . import diagnosis`).

- [ ] **Step 4: Re-export models** — in `app/broker/alpaca/clerk/__init__.py`, add `CustodyDiagnosis` (and the request/receipt models) to the exports alongside `ClerkStatus`.

- [ ] **Step 5: Run the clerk-method tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_custody_diagnosis.py -v -k custody_diagnosis`
Expected: PASS.

- [ ] **Step 6: Add the endpoint** to `brokers.py` (after `get_clerk_status`, `brokers.py:277`; protected read, no per-route dep, matching that route):

```python
@router.get("/{broker}/clerk/custody-diagnosis", response_model=CustodyDiagnosis)
async def get_custody_diagnosis(broker: str) -> CustodyDiagnosis:
    """Diagnose Clerk↔broker custody divergence (read-only).

    Transport only: resolve the account-scoped Clerk and delegate. The Clerk
    reads a fresh broker snapshot and projects the structured, backend-authored
    diagnosis the Accounts page renders verbatim.
    """
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.custody_diagnosis()
    except BrokerError as error:
        _raise_http(error)
```

Add `CustodyDiagnosis` to the `from app.broker.alpaca.clerk import (...)` block (`brokers.py:20-26`).

- [ ] **Step 7: Write the failing endpoint test** (append; Style B ASGITransport — reuse `_alpaca_clerk` fixture, `_get`, `_ACCOUNT_BODY` from `test_clerk_status_endpoint.py`):

```python
@responses.activate
async def test_custody_diagnosis_endpoint_reports_mismatch(_alpaca_clerk: None) -> None:
    responses.add(responses.GET, f"{_BASE}/v2/account", body=_ACCOUNT_BODY, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/orders", body="[]", status=200)
    responses.add(
        responses.GET, f"{_BASE}/v2/positions", body=_one_spy_position_body(), status=200
    )

    response = await _get("/api/brokers/alpaca/clerk/custody-diagnosis")

    assert response.status_code == 200
    body = response.json()
    assert body["in_sync"] is False
    assert body["divergences"][0]["kind"] == "exposure_attribution_mismatch"
    assert body["snapshot_version"]
```

(`_one_spy_position_body()` returns a one-position Alpaca positions JSON — model it on the existing `_foreign_order_body()` helper in that file.)

- [ ] **Step 8: Run endpoint test; then regenerate the OpenAPI contract**

Run: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk/test_custody_diagnosis.py -v` → PASS.
Then regenerate the committed contract (per the OpenAPI CI gate): `podman exec polygon-data-service python scripts/export_openapi_contract.py` (or the repo's documented export command), and stage the updated `contracts/` file.

- [ ] **Step 9: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/clerk.py PythonDataService/app/broker/alpaca/clerk/__init__.py PythonDataService/app/routers/brokers.py PythonDataService/tests/broker/alpaca/clerk/test_custody_diagnosis.py contracts/
git commit -m "feat(clerk): GET custody-diagnosis endpoint"
```

## Task 1.3: Frontend types + service method

**Files:**
- Modify: `Frontend/src/app/api/alpaca.types.ts`, `Frontend/src/app/services/brokers.service.ts`
- Test: `Frontend/src/app/services/brokers.service.spec.ts` (create if absent)

**Interfaces:**
- Produces: `type CustodyDiagnosis`; `BrokersService.getCustodyDiagnosis(broker?) : Promise<CustodyDiagnosis>`.

- [ ] **Step 1: Regenerate + alias the type.** After Task 1.2's contract regen, run `npm run codegen:openapi` (per the alpaca.types.ts header comment) so `broker.types.ts` gains `CustodyDiagnosis`. Then add to `alpaca.types.ts`:

```ts
// Custody resolution (Clerk↔broker reconciliation on the Accounts page).
export type CustodyDiagnosis = components['schemas']['CustodyDiagnosis'];
export type CustodyDivergence = components['schemas']['CustodyDivergence'];
export type CustodyResolutionReceipt = components['schemas']['CustodyResolutionReceipt'];
```

- [ ] **Step 2: Write the failing service test** (`brokers.service.spec.ts`, using `HttpTestingController`):

```ts
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import { BrokersService } from './brokers.service';

describe('BrokersService.getCustodyDiagnosis', () => {
  it('GETs the custody-diagnosis endpoint', async () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), BrokersService],
    });
    const svc = TestBed.inject(BrokersService);
    const http = TestBed.inject(HttpTestingController);

    const promise = svc.getCustodyDiagnosis('alpaca');
    const req = http.expectOne('/api/brokers/alpaca/clerk/custody-diagnosis');
    expect(req.request.method).toBe('GET');
    req.flush({ broker: 'alpaca', account_id: 'PA1', in_sync: true, observed_at_ms: 1, snapshot_version: 'x', resolution_posture: 'paper', resolvable: false, divergences: [], resolution_plan: [] });

    expect((await promise).in_sync).toBe(true);
  });
});
```

- [ ] **Step 2b: Run to verify it fails** — `podman exec my-frontend npx ng test --include='**/brokers.service.spec.ts'` → FAIL (`getCustodyDiagnosis` not a function).

- [ ] **Step 3: Add the method** to `brokers.service.ts` (after `getClerkStatus`):

```ts
  getCustodyDiagnosis(broker = 'alpaca'): Promise<CustodyDiagnosis> {
    return firstValueFrom(
      this.http.get<CustodyDiagnosis>(`${this.base}/${broker}/clerk/custody-diagnosis`),
    );
  }
```

Add `CustodyDiagnosis` to the `import type { ... } from '../api/alpaca.types'` block.

- [ ] **Step 4: Run to verify pass** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(brokers): getCustodyDiagnosis client"`.

## Task 1.4: The custody-resolution card (read-only, 4 states)

**Files:**
- Create: `Frontend/src/app/components/brokers/alpaca-desk/alpaca-custody-resolution.component.{ts,html,scss}` + `.spec.ts`
- Modify: `Frontend/src/app/components/brokers/alpaca-desk/alpaca-desk.component.{ts,html}`

**Interfaces:**
- Consumes: `BrokersService.getCustodyDiagnosis`, `CustodyDiagnosis`, `TimestampDisplayComponent`, `ReceiptLabelPipe`.
- Produces: `AlpacaCustodyResolutionComponent` (selector `app-alpaca-custody-resolution`).

- [ ] **Step 1: Write the failing component test** (`alpaca-custody-resolution.component.spec.ts`, Vitest + Testing Library — match `alpaca-desk.component.spec.ts` provider-mock style):

```ts
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import type { CustodyDiagnosis } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';

function diagnosis(overrides: Partial<CustodyDiagnosis> = {}): CustodyDiagnosis {
  return {
    broker: 'alpaca', account_id: 'PA1', in_sync: true, observed_at_ms: 1,
    snapshot_version: 'v1', resolution_posture: 'paper', resolvable: false,
    blocked_reason: null, divergences: [], resolution_plan: [], ...overrides,
  } as CustodyDiagnosis;
}

function svc(d: CustodyDiagnosis) {
  return { getCustodyDiagnosis: vi.fn().mockResolvedValue(d) };
}

describe('AlpacaCustodyResolutionComponent', () => {
  it('shows the in-sync strip when clerk and broker agree', async () => {
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(diagnosis()) }],
    });
    expect(await screen.findByText(/in sync/i)).toBeTruthy();
  });

  it('shows the delta and explanation when diverged', async () => {
    const diverged = diagnosis({
      in_sync: false, resolvable: true,
      divergences: [{
        kind: 'exposure_attribution_mismatch', state: 'resolvable_now',
        explanation: 'The broker holds exposure the Clerk cannot map.',
        possible_causes: ['A bot process was terminated mid-run.'],
        position_deltas: [{ symbol: 'SPY', clerk_attributed_qty: 2, broker_observed_qty: 1 }],
        resolution_step: 'record_inventory_baseline', prerequisite_detail: null, evidence_refs: [],
      }],
      resolution_plan: [{ action_id: 'record_inventory_baseline', scope: 'account', mutates: true }],
    });
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(diverged) }],
    });
    expect(await screen.findByText(/cannot map/i)).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByRole('button', { name: /resolve & sync/i })).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run to verify it fails** — `podman exec my-frontend npx ng test --include='**/alpaca-custody-resolution.component.spec.ts'` → FAIL (component doesn't exist).

- [ ] **Step 3: Implement the component TS** (`alpaca-custody-resolution.component.ts`):

```ts
import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokersService } from '../../../services/brokers.service';
import type { CustodyDiagnosis } from '../../../api/alpaca.types';

@Component({
  selector: 'app-alpaca-custody-resolution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-custody-resolution.component.html',
  styleUrl: './alpaca-custody-resolution.component.scss',
})
export class AlpacaCustodyResolutionComponent {
  private readonly brokers = inject(BrokersService);
  protected readonly reloadKey = signal(0);
  protected readonly diagnosisResource = resource({
    params: () => ({ key: this.reloadKey() }),
    loader: () => this.brokers.getCustodyDiagnosis('alpaca'),
  });
  protected readonly diagnosis = computed<CustodyDiagnosis | undefined>(() => this.diagnosisResource.value());
  protected reload(): void { this.reloadKey.update((v) => v + 1); }
}
```

- [ ] **Step 4: Implement the template** (`alpaca-custody-resolution.component.html`) — four states. Read-only in this slice (button emits nothing yet; Slice 2 wires it):

```html
@let d = diagnosis();
@if (d) {
  @if (d.in_sync) {
    <section class="custody custody--sync" aria-label="Clerk and broker in sync">
      <span class="custody__ok" aria-hidden="true">✓</span>
      <p>Clerk and broker are in sync · checked
        <app-timestamp-display [value]="d.observed_at_ms" mode="local" /></p>
    </section>
  } @else {
    <section class="custody custody--diverged" aria-labelledby="custody-title">
      <h2 id="custody-title">Clerk ↔ broker out of sync</h2>
      @for (div of d.divergences; track div.kind) {
        <article class="custody__divergence">
          <p class="custody__explanation">{{ div.explanation }}</p>
          @if (div.position_deltas.length) {
            <table class="custody__delta">
              <thead><tr><th>Symbol</th><th>Clerk attributes</th><th>Broker holds</th></tr></thead>
              <tbody>
                @for (p of div.position_deltas; track p.symbol) {
                  <tr><td>{{ p.symbol }}</td><td>{{ p.clerk_attributed_qty }}</td><td>{{ p.broker_observed_qty }}</td></tr>
                }
              </tbody>
            </table>
          }
          <details class="custody__causes">
            <summary>Possible causes</summary>
            <ul>@for (c of div.possible_causes; track c) { <li>{{ c }}</li> }</ul>
          </details>
          @if (div.prerequisite_detail) {
            <p class="custody__prereq" role="status">{{ div.prerequisite_detail }}</p>
          }
        </article>
      }
      @if (d.resolvable) {
        <button type="button" class="custody__resolve" disabled>Resolve &amp; sync to broker</button>
      }
    </section>
  }
}
```

- [ ] **Step 5: Minimal SCSS** (`alpaca-custody-resolution.component.scss`) — reuse desk custom properties; ensure the delta table scrolls on overflow (`overflow-x:auto`) and colours work in light/dark. Keep it short.

- [ ] **Step 6: Run to verify component tests pass** → PASS.

- [ ] **Step 7: Slot into the desk.** In `alpaca-desk.component.ts` add `AlpacaCustodyResolutionComponent` to `imports`; in `alpaca-desk.component.html` add `<app-alpaca-custody-resolution />` immediately after `<app-alpaca-hold-banner />`. Update `alpaca-desk.component.spec.ts`'s `brokerService()` mock to add `getCustodyDiagnosis: vi.fn().mockResolvedValue(<in-sync diagnosis>)` so the existing desk tests still pass.

- [ ] **Step 8: Run desk tests + typecheck + lint** — `podman exec my-frontend npx ng test --include='**/alpaca-desk.component.spec.ts'`; `podman exec my-frontend npx tsc --noEmit`; `npx eslint Frontend/src/ --max-warnings 0`. All green.

- [ ] **Step 9: Commit** — `git commit -m "feat(alpaca-desk): read-only custody-resolution card"`.

---

# SLICE 2 — Guided resolve (mutating)

Delivers: the "Resolve & sync" button runs the plan behind a required comment + typed token, journals the comment, shows a receipt, and re-diagnoses.

## Task 2.1: Clerk `resolve_custody()` orchestration

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/clerk.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_custody_resolve.py`

**Interfaces:**
- Consumes: `self.custody_diagnosis()`, `self.reconcile_once()`, `self.record_inventory_baseline(operator, reason)`, `self.clear_hold(operator, reason)`, `diagnosis.custody_snapshot_version`.
- Produces: `AlpacaClerk.resolve_custody(*, operator, reason, snapshot_version) -> CustodyResolutionReceipt`; raises `CustodySnapshotChangedError` (new, subclass of a domain error) on stale snapshot.

- [ ] **Step 1: Write the failing test** (`test_custody_resolve.py`, Style A):

```python
async def test_resolve_adopts_baseline_and_journals_operator_reason() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    diag = await clerk.custody_diagnosis()

    receipt = await clerk.resolve_custody(
        operator="ops", reason="07-31 run was killed mid-fill; adopting broker truth.",
        snapshot_version=diag.snapshot_version,
    )

    assert receipt.resolved is True
    assert receipt.in_sync is True
    # The operator comment is journaled on the baseline row.
    baseline = [e for e in clerk._journal.read_entries() if e.kind == ClerkEntryKind.BROKER_EVIDENCE_BASELINE]
    assert baseline[-1].operator == "ops"
    assert "adopting broker truth" in baseline[-1].reason


async def test_resolve_rejects_stale_snapshot() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    with pytest.raises(diagnosis.CustodySnapshotChangedError):
        await clerk.resolve_custody(operator="ops", reason="x", snapshot_version="stale-token")
```

- [ ] **Step 2: Run to verify it fails** → FAIL (`resolve_custody` / `CustodySnapshotChangedError` missing).

- [ ] **Step 3: Add `CustodySnapshotChangedError`** to `diagnosis.py`:

```python
class CustodySnapshotChangedError(Exception):
    """The custody snapshot changed since diagnosis; re-diagnose before resolving."""

    def __init__(self, message: str = "Account state changed since it was diagnosed.") -> None:
        super().__init__(message)
        self.detail = "Re-run the diagnosis and confirm the current state before resolving."
```

- [ ] **Step 4: Add `resolve_custody()` to `clerk.py`**:

```python
    async def resolve_custody(
        self, *, operator: str, reason: str, snapshot_version: str
    ) -> diagnosis.CustodyResolutionReceipt:
        """Execute the diagnosed resolution plan, journaling the operator reason.

        Snapshot-guarded: rejects a stale token so the operator never resolves
        against evidence that changed after they looked. Idempotent: an already
        in-sync account resolves to a benign no-op.
        """
        diag = await self.custody_diagnosis()
        if diag.snapshot_version != snapshot_version:
            raise diagnosis.CustodySnapshotChangedError()
        if diag.in_sync:
            return diagnosis.CustodyResolutionReceipt(
                broker=self.broker_id, account_id=diag.account_id, resolved=True,
                receipt_id=uuid4().hex, recorded_at_ms=self._clock(),
                in_sync=True,
            )
        steps: list[diagnosis.CustodyResolutionStepResult] = []
        for step in diag.resolution_plan:
            if step.action_id == "reconcile_now":
                verdict = await self.reconcile_once()
                steps.append(diagnosis.CustodyResolutionStepResult(
                    action_id="reconcile_now", message=f"Reconciliation sweep: {verdict}."))
            elif step.action_id == "record_inventory_baseline":
                baseline = await self.record_inventory_baseline(operator=operator, reason=reason)
                positions = ", ".join(
                    f"{p.symbol} {p.signed_quantity:g}" for p in baseline.positions
                )
                steps.append(diagnosis.CustodyResolutionStepResult(
                    action_id="record_inventory_baseline",
                    message=f"Adopted broker inventory as baseline: {positions or 'flat'}."))
            elif step.action_id == "clear_hold":
                await self.clear_hold(operator=operator, reason=reason)
                steps.append(diagnosis.CustodyResolutionStepResult(
                    action_id="clear_hold", message="Exposure hold cleared."))
        after = await self.custody_diagnosis()
        return diagnosis.CustodyResolutionReceipt(
            broker=self.broker_id, account_id=diag.account_id,
            resolved=after.in_sync, receipt_id=uuid4().hex, recorded_at_ms=self._clock(),
            steps_executed=tuple(steps), in_sync=after.in_sync,
            remaining_divergences=after.divergences,
        )
```

`InventoryBaselineRefusedError` (raised by `record_inventory_baseline` on a prerequisite block) propagates — the router maps it to a 409 with its `.detail`. (`uuid4` is already imported in `clerk.py`.)

- [ ] **Step 5: Run to verify it passes** → PASS.

- [ ] **Step 6: Commit** — `git commit -m "feat(clerk): resolve_custody orchestration + snapshot guard"`.

## Task 2.2: The resolve endpoint

**Files:**
- Modify: `PythonDataService/app/routers/brokers.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_custody_resolve.py` (append endpoint tests)

**Interfaces:**
- Produces: `POST /api/brokers/{broker}/clerk/resolve` → `CustodyResolutionReceipt`.

- [ ] **Step 1: Write the failing endpoint tests** (Style B):

```python
@responses.activate
async def test_resolve_endpoint_requires_the_token(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()
    diag = (await _get("/api/brokers/alpaca/clerk/custody-diagnosis")).json()
    resp = await _post("/api/brokers/alpaca/clerk/resolve", {
        "reason": "adopting broker truth", "snapshot_version": diag["snapshot_version"],
        "confirmation_token": "NOPE", "idempotency_key": "k1"})
    assert resp.status_code == 422


@responses.activate
async def test_resolve_endpoint_409_on_stale_snapshot(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()
    resp = await _post("/api/brokers/alpaca/clerk/resolve", {
        "reason": "x", "snapshot_version": "stale", "confirmation_token": "RESOLVE",
        "idempotency_key": "k2"})
    assert resp.status_code == 409


@responses.activate
async def test_resolve_endpoint_rejects_blank_reason(_alpaca_clerk: None) -> None:
    _wire_one_spy_position()
    diag = (await _get("/api/brokers/alpaca/clerk/custody-diagnosis")).json()
    resp = await _post("/api/brokers/alpaca/clerk/resolve", {
        "reason": "   ", "snapshot_version": diag["snapshot_version"],
        "confirmation_token": "RESOLVE", "idempotency_key": "k3"})
    assert resp.status_code == 422
```

(`_wire_one_spy_position()` registers the account/orders/positions `responses` mocks used across these tests.)

- [ ] **Step 2: Run to verify it fails** → FAIL (404, route missing).

- [ ] **Step 3: Add the endpoint** (control mutation — per-route auth dep, matching `clear_clerk_hold`):

```python
@router.post(
    "/{broker}/clerk/resolve",
    response_model=CustodyResolutionReceipt,
    dependencies=[Depends(require_data_plane_control_secret)],
)
async def resolve_custody(
    broker: str, request: CustodyResolutionRequest
) -> CustodyResolutionReceipt:
    """Resolve Clerk↔broker divergence: run the diagnosed plan, journal the reason.

    A control mutation. The typed token is a UI friction gate; the operator
    identity is injected server-side. A stale snapshot is a 409; a blocked
    prerequisite is a 409 with the blocker's what/why.
    """
    if request.confirmation_token != "RESOLVE":
        raise HTTPException(
            status_code=422,
            detail={"message": "Type RESOLVE to confirm.", "why": "Confirmation token mismatch."},
        )
    clerk = _require_trade_clerk(broker)
    try:
        return await clerk.resolve_custody(
            operator=settings.PANEL_OPERATOR_IDENTITY,
            reason=request.reason,
            snapshot_version=request.snapshot_version,
        )
    except CustodySnapshotChangedError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except InventoryBaselineRefusedError as error:
        raise HTTPException(status_code=409, detail={"message": str(error), "why": error.detail})
    except BrokerError as error:
        _raise_http(error)
```

Add `CustodyResolutionReceipt`, `CustodyResolutionRequest`, `CustodySnapshotChangedError`, `InventoryBaselineRefusedError` to the clerk imports; confirm `settings.PANEL_OPERATOR_IDENTITY` is importable (same identity the panel path uses).

- [ ] **Step 4: Run endpoint tests; add a happy-path resolve test asserting 200 + `resolved: true`.** Then regenerate the OpenAPI contract (`export_openapi_contract.py`).

- [ ] **Step 5: Commit** — `git add … contracts/ && git commit -m "feat(clerk): POST resolve endpoint"`.

## Task 2.3: The required-comment + typed-token confirm dialog

**Files:**
- Create: `Frontend/src/app/components/brokers/alpaca-desk/custody-resolution-confirm-dialog.component.{ts,html,scss}` + `.spec.ts`

**Interfaces:**
- Produces: `CustodyResolutionConfirmDialogComponent`. Inputs: `open: boolean`, `diagnosis: CustodyDiagnosis`, `busy: boolean`, `errorMessage: string | null`. Outputs: `confirmed: EventEmitter<{ reason: string }>`, `cancelled`.

- [ ] **Step 1: Write the failing test** — asserts the confirm button is disabled until the reason is non-blank AND the token equals `RESOLVE`, then enabled, and `confirmed` emits `{ reason }`. Clone the assertion style from any existing dialog spec.

```ts
it('enables confirm only with a reason and the RESOLVE token', async () => {
  const confirmed = vi.fn();
  await render(CustodyResolutionConfirmDialogComponent, {
    inputs: { open: true, diagnosis: divergedDiagnosis(), busy: false, errorMessage: null },
    on: { confirmed },
  });
  const confirm = screen.getByRole('button', { name: /resolve & sync/i }) as HTMLButtonElement;
  expect(confirm.disabled).toBe(true);
  fireEvent.input(screen.getByLabelText(/why did the clerk and broker/i), { target: { value: 'killed mid-fill' } });
  expect(confirm.disabled).toBe(true); // token still empty
  fireEvent.input(screen.getByLabelText(/type RESOLVE/i), { target: { value: 'RESOLVE' } });
  expect(confirm.disabled).toBe(false);
  fireEvent.click(confirm);
  expect(confirmed).toHaveBeenCalledWith({ reason: 'killed mid-fill' });
});
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement the dialog** — clone the structure of `account-desk-recovery-confirm-dialog.component.ts` (native `<dialog>` + `viewChild` + `effect()` showModal/close; `canConfirm` computed gating on `reason.trim().length > 0 && token === 'RESOLVE'`). Signals `reason` and `token`. Template: the delta table + plan summary, the required `<textarea pTextarea>` (label "Why did the Clerk and broker fall out of sync?", help "Required. This becomes part of the audited Clerk recovery record.", `[invalid]="reason().trim().length === 0"`, aria-required/aria-invalid, `role="status"` blocking message), the `Type RESOLVE to confirm` input, and Cancel/Confirm buttons. Confirm disabled unless `canConfirm()`.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat(alpaca-desk): custody-resolution confirm dialog"`.

## Task 2.4: Wire the resolve button, dialog, receipt, and 409 re-diagnosis

**Files:**
- Modify: `Frontend/src/app/services/brokers.service.ts` (add `resolveCustody`), `alpaca-custody-resolution.component.{ts,html}`
- Reuse: `PanelActionReceiptComponent` + `ActionReceiptView`

**Interfaces:**
- Consumes: `BrokersService.resolveCustody(broker, body) : Promise<CustodyResolutionReceipt>`.

- [ ] **Step 1: Add `resolveCustody` to the service** (control mutation POST):

```ts
  resolveCustody(
    broker: string,
    body: { reason: string; snapshot_version: string; confirmation_token: string; idempotency_key: string },
  ): Promise<CustodyResolutionReceipt> {
    return firstValueFrom(
      this.http.post<CustodyResolutionReceipt>(`${this.base}/${broker}/clerk/resolve`, body),
    );
  }
```

- [ ] **Step 2: Write the failing component test** — clicking "Resolve & sync", filling the dialog, and confirming calls `resolveCustody` with the diagnosis `snapshot_version` + `confirmation_token: 'RESOLVE'`, then renders the receipt and re-fetches the diagnosis. Also: a 409 from `resolveCustody` re-fetches the diagnosis and shows an "account state changed" message without a receipt.

- [ ] **Step 3: Run to verify it fails.**

- [ ] **Step 4: Implement the wiring** in `alpaca-custody-resolution.component.ts`: a `confirmOpen` signal; the enabled resolve button opens the dialog; on `confirmed({reason})` call `resolveCustody('alpaca', { reason, snapshot_version: d.snapshot_version, confirmation_token: 'RESOLVE', idempotency_key: crypto.randomUUID() })`; on success map the `CustodyResolutionReceipt` to an `ActionReceiptView` (`{ actionId: 'resolve', outcome: 'success', receiptId: r.receipt_id, recordedAtMs: r.recorded_at_ms, message: firstStepMessage, remediation: null }`), set a receipt signal, and `reload()`; on `HttpErrorResponse` 409 set an "account state changed — re-checking" message and `reload()` (never auto-resubmit); render `<app-panel-action-receipt>` and the error via existing patterns. Enable the button (remove `disabled`) and add `(click)="confirmOpen.set(true)"`.

- [ ] **Step 5: Run tests + typecheck + lint** → green.

- [ ] **Step 6: Commit** — `git commit -m "feat(alpaca-desk): guided resolve + receipt + 409 re-diagnosis"`.

---

# SLICE 3 — Per-bot comment parity (independently shippable)

Delivers: resolving a mutating verb from the per-bot Operator lens captures the same required comment and journals it. This slice is additive — Slices 1–2 deliver the core value without it; ship it in this effort per "full scope", or defer as a follow-up.

## Task 3.1: Thread `reason` through the panel performers

**Files:**
- Modify: `PythonDataService/app/services/broker_v2_panel/action_execution_service.py`, `PythonDataService/app/services/broker_v2_panel/panel_data_source.py`
- Test: `PythonDataService/tests/broker/v2panel/test_action_execution.py`

**Interfaces:**
- Changes: `ActionPerformer = Callable[[str, str | None], Awaitable[str]]` (add `reason`); `execute_action` calls `performer(operator_identity, request.reason)`.

- [ ] **Step 1: Write the failing test** — a `record_inventory_baseline` (or `clear_hold`) action carrying `reason="operator note"` journals that reason on the resulting `BROKER_EVIDENCE_BASELINE` / `HOLD_CLEARED` row (assert via the clerk journal), and a `reconcile_now` action ignores reason.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement:**
  - `action_execution_service.py:74-75` — change the alias to `ActionPerformer = Callable[[str, str | None], Awaitable[str]]`.
  - `action_execution_service.py:357` — `message = await performer(operator_identity, request.reason)`.
  - `panel_data_source.py:787-864` — change each performer signature to `async def _x(operator: str, reason: str | None) -> str:`; in `_clear_hold` use `reason=reason or "Panel clear-hold"`; in `_record_inventory_baseline` use `reason=reason or "Operator confirmed current Alpaca inventory from the bot panel."`; `_reconcile` ignores `reason`. Update every other performer (`_resume`, `_pause`, `_continue`, `_stop`, `_flatten_stop`) to accept and ignore the second parameter.

- [ ] **Step 4: Run to verify pass; run the full v2panel suite** — `podman exec polygon-data-service python -m pytest tests/broker/v2panel -v` (catch any performer-signature fallout).

- [ ] **Step 5: Commit** — `git commit -m "feat(panel): thread operator reason to mutating performers"`.

## Task 3.2: Capture the comment on the Operator-lens gate confirm

**Files:**
- Modify: `Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.service.ts`, `panel-action-button.component.{ts,html}`, `operator-readiness.component.*`
- Test: the co-located specs

**Interfaces:**
- Changes: `runBotAction`/`submitAction` accept an optional `reason`; the gate confirm collects it for `record_inventory_baseline` / `clear_hold`.

- [ ] **Step 1: Write the failing test** — confirming a mutating gate action passes a non-null `reason` into `runAction`'s `PanelActionRequest`; a non-mutating action passes `reason: null`.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement:**
  - `broker-v2-panel.service.ts` — `submitAction` (and the public `runBotAction`) accept `reason: string | null = null` and set it on the `PanelActionRequest` instead of the hardcoded `reason: null`. Thread it through the 409 fresh-token retry path.
  - `panel-action-button.component.ts` — for actions whose `action_id` is `record_inventory_baseline` or `clear_hold`, render a comment-capable confirm (either extend the confirmation flow with a required textarea, or swap in the `CustodyResolutionConfirmDialogComponent`-style required-comment dialog); emit the reason with `triggered`.
  - `operator-readiness.component.ts` / `bot-panel-shell.component.ts` — thread the emitted reason into `runBotAction`.

- [ ] **Step 4: Run tests + typecheck + lint** → green.

- [ ] **Step 5: Commit** — `git commit -m "feat(operator-lens): required comment on mutating gate actions"`.

---

## Final gates (before opening the PR)

- [ ] Project-scope lint: `ruff check PythonDataService/app/ PythonDataService/tests/` and `npx eslint Frontend/src/ --max-warnings 0` — both clean.
- [ ] Full relevant suites: `podman exec polygon-data-service python -m pytest tests/broker/alpaca/clerk tests/broker/v2panel` and `podman exec my-frontend npx ng test --watch=false` (baseline any inherited failures per testing.md).
- [ ] OpenAPI contract regenerated + committed (CI "Verify committed OpenAPI contract").
- [ ] Invoke the `thermo-nuclear-code-quality-review` skill; address every major finding before first push.
- [ ] Update `docs/superpowers/specs/2026-08-03-clerk-broker-custody-resolution-design.md` status → implemented; link the PR to the motivating case.

## Spec Coverage Check

- Diagnosis (delta + explanation + causes + plan + snapshot) → Tasks 1.1–1.4. ✓
- Guided resolve (required comment + token + snapshot 409 + journaled reason + receipt) → Tasks 2.1–2.4. ✓
- Escalation states (resolvable-now / blocked-on-prerequisite / needs-review) → `diagnose_custody` (Task 1.1) + card rendering (1.4). ✓
- `resolution_posture` seam → `CustodyDiagnosis.resolution_posture` field (Task 1.1), defaulted `paper`. ✓
- No journal schema change → reuses `operator`/`reason` (Tasks 2.1, 3.1). ✓
- Backend-authored copy + `receiptLabel` discipline → `_CAUSES`/`_EXPLANATION` map (1.1), verbatim render (1.4). ✓
- Per-bot parity → Slice 3. ✓
- Non-goal (no flatten) → nothing in the plan flattens. ✓

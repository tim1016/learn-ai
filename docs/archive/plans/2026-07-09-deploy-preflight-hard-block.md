> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0026, ADR-0030, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** This plan depends on the retired blocker-design snapshot and predates the current lifecycle/control consolidation.

# Slice 1 — Deploy Preflight + Hard Block — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse a deploy that cannot possibly run — daemon down, broker disconnected, account frozen/not-proven, fleet contaminated, strategy not validated, or instance already running — with the exact blocker and its move shown on the deploy screen, via a backend-authored `OperatorBlocker` contract both surfaces will share.

**Architecture:** A new closed `OperatorBlocker` value type (with a `disposition` enum) is authored server-side by `author_deploy_blockers()` and served from a new `GET /api/live-instances/deploy-preflight`. The deploy form stops re-deriving readiness in TypeScript: it renders the backend blocker list plus a small set of client-side *form* blockers (missing fields, coherence, sizing, legs) in the same shape, and a single `[Deploy & run]` button is disabled unless every blocker is non-blocking. The deploy-only/"start immediately" path is removed.

**Tech Stack:** Python 3.11+ / FastAPI / Pydantic v2 (backend); Angular 21 signals + resource(), Vitest (frontend).

## Global Constraints

- **Time is `int64 ms UTC`** everywhere on the wire; no ISO/`DateTime`. (Not exercised in this slice — no timestamps added.)
- **Backend authors operator semantics** (ADR-0013): all blocker `headline`/`detail`/`label` prose is authored in Python and rendered verbatim in Angular — never composed from enums in TypeScript.
- **Pydantic v2**: `model_validator(mode="after")`, `ConfigDict(extra="forbid")`. No v1 patterns.
- **Angular 21**: standalone (no `standalone: true`), `ChangeDetectionStrategy.OnPush`, `inject()`, `signal()`/`computed()`/`resource()`, `@if`/`@for` with `track`, `[class.x]` bindings, no `any`. Prose renders verbatim (not through `receiptLabel`); the pipe is only for code-like identifiers.
- **Explicit tolerances / no silent catches / no `print`|`console.log`|`Console.WriteLine`.**
- **Lint at project scope before pushing:** `ruff check PythonDataService/app/ PythonDataService/tests/` and `npx eslint Frontend/src/ --max-warnings 0`.
- **Disposition decisions for the deploy context (this slice):** every *backend* deploy blocker is `fix_elsewhere` (a `navigate` move to the page that fixes it) or `wait` (transient broker states, no move). None are `fix_here` — on the deploy screen the cures live elsewhere (engine page, broker page, account monitor, strategy validation). `terminal` and the `retire`/`remove`/`invoke_endpoint` moves are **Slice 2** and are not built here. Client-side *form* blockers (missing fields, coherence, sizing, legs) are `fix_here` with a `confirm_in_form` move.

---

## File Structure

**Backend (create):**
- `PythonDataService/app/schemas/operator_blocker.py` — `Disposition`, `NavigateAction`, `ConfirmInFormAction`, `OperatorAction`, `OperatorMove`, `OperatorBlocker`, `DeployPreflightResponse`.
- `PythonDataService/app/services/deploy_preflight.py` — `author_deploy_blockers(...)` pure function + `DeployPreflightSignals` input dataclass.
- `PythonDataService/tests/schemas/test_operator_blocker.py`
- `PythonDataService/tests/services/test_deploy_preflight.py`
- `PythonDataService/tests/routers/test_deploy_preflight_endpoint.py`

**Backend (modify):**
- `PythonDataService/app/routers/live_instances.py` — add `GET /deploy-preflight` handler + a signal-gathering helper.

**Frontend (create):**
- `Frontend/src/app/api/operator-blocker.types.ts` — TS mirror of the contract.
- `Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.ts` — `buildFormBlockers()` + `resolveBlockerMove()`.
- `Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.spec.ts`

**Frontend (modify):**
- `Frontend/src/app/services/live-runs.service.ts` — add `deployPreflight()`.
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.ts` — preflight resource, merged blockers, single button, delete legacy readiness derivation + deploy-only path.
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.html` — blocker list + single button; remove facts strip + start-immediately checkbox.
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.spec.ts` — update/extend.

---

## Task 1: Backend contract types

**Files:**
- Create: `PythonDataService/app/schemas/operator_blocker.py`
- Test: `PythonDataService/tests/schemas/test_operator_blocker.py`

**Interfaces:**
- Produces: `Disposition` (`Literal["fix_here","fix_elsewhere","wait","terminal"]`); `NavigateAction(kind="navigate", route:str, fragment:str|None=None)`; `ConfirmInFormAction(kind="confirm_in_form", anchor:str)`; `OperatorAction = NavigateAction | ConfirmInFormAction` (superset grows in Slice 2); `OperatorMove(label:str, action:OperatorAction, target:str|None=None)`; `OperatorBlocker(id, severity, disposition, headline, detail, primary_move, secondary_moves, applies_to)`; `DeployPreflightResponse(ready:bool, blockers:list[OperatorBlocker])`.

- [ ] **Step 1: Write the failing test**

```python
# PythonDataService/tests/schemas/test_operator_blocker.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.operator_blocker import (
    NavigateAction,
    OperatorBlocker,
    OperatorMove,
)


def _nav_move() -> OperatorMove:
    return OperatorMove(
        label="Connect the broker",
        action=NavigateAction(kind="navigate", route="/broker", fragment=None),
    )


def test_fix_elsewhere_requires_primary_move():
    with pytest.raises(ValidationError, match="requires a primary_move"):
        OperatorBlocker(
            id="broker_disconnected",
            severity="blocking",
            disposition="fix_elsewhere",
            headline="Broker disconnected",
            detail=None,
            primary_move=None,
            secondary_moves=[],
            applies_to="both",
        )


def test_wait_must_not_carry_a_move():
    with pytest.raises(ValidationError, match="must not carry a primary_move"):
        OperatorBlocker(
            id="broker_soft_lost",
            severity="blocking",
            disposition="wait",
            headline="Broker connection temporarily lost",
            detail=None,
            primary_move=_nav_move(),
            secondary_moves=[],
            applies_to="both",
        )


def test_terminal_requires_at_least_one_move():
    with pytest.raises(ValidationError, match="requires at least one move"):
        OperatorBlocker(
            id="run_poisoned",
            severity="blocking",
            disposition="terminal",
            headline="Can't recover",
            detail=None,
            primary_move=None,
            secondary_moves=[],
            applies_to="run",
        )


def test_valid_fix_elsewhere_blocker_constructs():
    blocker = OperatorBlocker(
        id="broker_disconnected",
        severity="blocking",
        disposition="fix_elsewhere",
        headline="Broker disconnected",
        detail="Connect the IBKR session before deploying.",
        primary_move=_nav_move(),
        secondary_moves=[],
        applies_to="both",
    )
    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "navigate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/schemas/test_operator_blocker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.operator_blocker'`.

- [ ] **Step 3: Write minimal implementation**

```python
# PythonDataService/app/schemas/operator_blocker.py
"""Backend-authored OperatorBlocker contract (disposition taxonomy).

Single atom rendered by both the deploy-preflight and (Slice 2) the
bot-control surfaces. The disposition/move pairing invariant here is the
structural guarantee that no blocker can render without a coherent move.
See docs/superpowers/specs/2026-07-09-operator-blocker-disposition-design.md.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Disposition = Literal["fix_here", "fix_elsewhere", "wait", "terminal"]


class NavigateAction(BaseModel):
    """Move: navigate to another operator page (route + optional fragment)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["navigate"]
    route: str
    fragment: str | None = None


class ConfirmInFormAction(BaseModel):
    """Move: resolve inline on the current form (scroll/focus an anchor)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirm_in_form"]
    anchor: str


# Superset grows in Slice 2 (invoke_capability / invoke_endpoint / redeploy /
# open_runbook / retire_replace / remove). Deploy context only needs these two.
OperatorAction = Annotated[
    NavigateAction | ConfirmInFormAction,
    Field(discriminator="kind"),
]


class OperatorMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    action: OperatorAction
    target: str | None = None


class OperatorBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Literal["blocking", "warning"]
    disposition: Disposition
    headline: str
    detail: str | None = None
    primary_move: OperatorMove | None = None
    secondary_moves: list[OperatorMove] = Field(default_factory=list)
    applies_to: Literal["deploy", "run", "both"]

    @model_validator(mode="after")
    def _disposition_move_pairing(self) -> OperatorBlocker:
        if self.disposition in ("fix_here", "fix_elsewhere") and self.primary_move is None:
            raise ValueError(f"{self.disposition} blocker requires a primary_move")
        if self.disposition == "wait" and self.primary_move is not None:
            raise ValueError("wait blocker must not carry a primary_move")
        if self.disposition == "terminal" and self.primary_move is None and not self.secondary_moves:
            raise ValueError("terminal blocker requires at least one move")
        return self


class DeployPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    blockers: list[OperatorBlocker]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/schemas/test_operator_blocker.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/schemas/operator_blocker.py PythonDataService/tests/schemas/test_operator_blocker.py
git commit -m "feat: add OperatorBlocker disposition contract + pairing invariant"
```

---

## Task 2: `author_deploy_blockers()` pure function

**Files:**
- Create: `PythonDataService/app/services/deploy_preflight.py`
- Test: `PythonDataService/tests/services/test_deploy_preflight.py`

**Interfaces:**
- Consumes: `OperatorBlocker`, `OperatorMove`, `NavigateAction` from Task 1.
- Produces: `DeployPreflightSignals` (frozen dataclass of already-resolved primitives) and `author_deploy_blockers(signals: DeployPreflightSignals) -> list[OperatorBlocker]`.

Signal fields and their sources (wired in Task 3):
`daemon_reachable: bool` · `broker_connection_state: str | None` (`connected|soft_lost|subscriptions_stale|degraded_data_farm|disconnected`; `None`=unavailable data-plane broker snapshot) · `account_frozen: bool` · `account_proven: bool` · `fleet_blocks_starts: bool` · `strategy_deployable: bool` · `instance_already_running: bool`.

Boundary note from the implementation review: deploy-preflight reads
`snapshot_data_plane_broker().connection_state`, the `IbkrClient` state subset.
Broader cockpit states such as `hard_down`, `disabled`, `reconnecting`, and
`recovering` come from broker-health monitor overlays and must not be authored
as deploy-preflight-only branches unless this endpoint is rewired to that
broader source.

- [ ] **Step 1: Write the failing test**

```python
# PythonDataService/tests/services/test_deploy_preflight.py
from __future__ import annotations

from app.services.deploy_preflight import DeployPreflightSignals, author_deploy_blockers


def _healthy() -> DeployPreflightSignals:
    return DeployPreflightSignals(
        daemon_reachable=True,
        broker_connection_state="connected",
        account_frozen=False,
        account_proven=True,
        fleet_blocks_starts=False,
        strategy_deployable=True,
        instance_already_running=False,
    )


def test_healthy_signals_produce_no_blockers():
    assert author_deploy_blockers(_healthy()) == []


def test_daemon_down_is_blocking_fix_elsewhere():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"daemon_reachable": False}))
    ids = {b.id: b for b in blockers}
    assert "daemon_down" in ids
    assert ids["daemon_down"].severity == "blocking"
    assert ids["daemon_down"].disposition == "fix_elsewhere"
    assert ids["daemon_down"].primary_move is not None


def test_broker_disconnected_blocks_deploy():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"broker_connection_state": "disconnected"}))
    ids = {b.id for b in blockers}
    assert "broker_disconnected" in ids


def test_broker_soft_lost_is_wait_with_no_move():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"broker_connection_state": "soft_lost"}))
    match = next(b for b in blockers if b.id == "broker_soft_lost")
    assert match.severity == "blocking"
    assert match.disposition == "wait"
    assert match.primary_move is None


def test_degraded_data_farm_is_blocking_wait():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"broker_connection_state": "degraded_data_farm"}))
    match = next(b for b in blockers if b.id == "broker_data_farm_degraded")
    assert match.severity == "blocking"
    assert match.disposition == "wait"


def test_account_frozen_blocks_deploy():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"account_frozen": True}))
    assert "account_frozen" in {b.id for b in blockers}


def test_account_not_proven_blocks_deploy():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"account_proven": False}))
    assert "account_not_proven" in {b.id for b in blockers}


def test_fleet_contamination_blocks_deploy():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"fleet_blocks_starts": True}))
    assert "fleet_contaminated" in {b.id for b in blockers}


def test_strategy_not_validated_blocks_deploy():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"strategy_deployable": False}))
    assert "strategy_not_validated" in {b.id for b in blockers}


def test_instance_already_running_blocks_deploy():
    blockers = author_deploy_blockers(_healthy().model_copy(update={"instance_already_running": True}))
    assert "instance_already_running" in {b.id for b in blockers}


def test_every_blocker_satisfies_pairing_invariant():
    # Turning every signal unhealthy must still produce only well-formed blockers
    # (construction would raise otherwise). This is the anti-dead-end guarantee.
    unhealthy = DeployPreflightSignals(
        daemon_reachable=False,
        broker_connection_state="disconnected",
        account_frozen=True,
        account_proven=False,
        fleet_blocks_starts=True,
        strategy_deployable=False,
        instance_already_running=True,
    )
    blockers = author_deploy_blockers(unhealthy)
    assert len(blockers) >= 6
    for b in blockers:
        if b.disposition in ("fix_here", "fix_elsewhere"):
            assert b.primary_move is not None
        if b.disposition == "wait":
            assert b.primary_move is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/services/test_deploy_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.deploy_preflight'`.

- [ ] **Step 3: Write minimal implementation**

```python
# PythonDataService/app/services/deploy_preflight.py
"""Author the deploy-context OperatorBlocker list (Slice 1).

Pure projection: the router resolves the seven signals and hands them in;
this function owns the disposition/move authoring so the frontend renders
verbatim. Deploy-context blockers are all ``fix_elsewhere`` (navigate) or
``wait`` — no cures live on the deploy screen itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.operator_blocker import NavigateAction, OperatorBlocker, OperatorMove

BrokerDeployConnectionState = Literal[
    "connected",
    "soft_lost",
    "subscriptions_stale",
    "degraded_data_farm",
    "disconnected",
]


class DeployPreflightSignals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daemon_reachable: bool
    broker_connection_state: BrokerDeployConnectionState | None
    account_frozen: bool
    account_proven: bool
    fleet_blocks_starts: bool
    strategy_deployable: bool
    instance_already_running: bool


def _nav(label: str, route: str, fragment: str | None = None) -> OperatorMove:
    return OperatorMove(label=label, action=NavigateAction(kind="navigate", route=route, fragment=fragment))


def author_deploy_blockers(signals: DeployPreflightSignals) -> list[OperatorBlocker]:
    blockers: list[OperatorBlocker] = []

    if not signals.daemon_reachable:
        blockers.append(
            OperatorBlocker(
                id="daemon_down",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Live engine unavailable",
                detail="Start the engine on this machine, then recheck.",
                primary_move=_nav("Start the engine", "/engine"),
                applies_to="both",
            )
        )

    state = signals.broker_connection_state
    if state is None or state == "disconnected":
        blockers.append(
            OperatorBlocker(
                id="broker_disconnected",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Broker disconnected",
                detail="Connect the IBKR session before deploying.",
                primary_move=_nav("Connect the broker", "/broker"),
                applies_to="both",
            )
        )
    elif state == "soft_lost":
        blockers.append(
            OperatorBlocker(
                id="broker_soft_lost",
                severity="blocking",
                disposition="wait",
                headline="Broker connection temporarily lost",
                detail="Waiting for the broker session to recover.",
                applies_to="both",
            )
        )
    elif state == "degraded_data_farm":
        blockers.append(
            OperatorBlocker(
                id="broker_data_farm_degraded",
                severity="blocking",
                disposition="wait",
                headline="IBKR data farm degraded",
                detail="Waiting for IBKR market-data evidence to recover — don't deploy until healthy.",
                applies_to="both",
            )
        )
    elif state == "subscriptions_stale":
        blockers.append(
            OperatorBlocker(
                id="broker_subscriptions_stale",
                severity="blocking",
                disposition="wait",
                headline="Broker subscriptions stale",
                detail="Resubscribe required — waiting for market-data streams.",
                applies_to="both",
            )
        )

    if signals.account_frozen:
        blockers.append(
            OperatorBlocker(
                id="account_frozen",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Account frozen",
                detail="Resolve the account sick-bay condition before deploying.",
                primary_move=_nav("Open account monitor", "/broker/account-monitor", "account-reconciliation-action"),
                applies_to="both",
            )
        )
    elif not signals.account_proven:
        blockers.append(
            OperatorBlocker(
                id="account_not_proven",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Account not proven",
                detail="Run account reconcile to prove the account is clean before deploying.",
                primary_move=_nav("Open account monitor", "/broker/account-monitor", "account-reconciliation-action"),
                applies_to="both",
            )
        )

    if signals.fleet_blocks_starts:
        blockers.append(
            OperatorBlocker(
                id="fleet_contaminated",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Fleet state blocks new deploys",
                detail="Clear the account fleet state before deploying.",
                primary_move=_nav("Open account monitor", "/broker/account-monitor"),
                applies_to="both",
            )
        )

    if not signals.strategy_deployable:
        blockers.append(
            OperatorBlocker(
                id="strategy_not_validated",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Strategy not validated",
                detail="Promote the strategy in Strategy Validation before deploying.",
                primary_move=_nav("Open Strategy Validation", "/strategy-validation"),
                applies_to="deploy",
            )
        )

    if signals.instance_already_running:
        blockers.append(
            OperatorBlocker(
                id="instance_already_running",
                severity="blocking",
                disposition="fix_elsewhere",
                headline="Deployment name already running",
                detail="A bot with this name is already live. Go to it, or choose a different name.",
                primary_move=_nav("Go to the running bot", "/broker/bots"),
                applies_to="deploy",
            )
        )

    return blockers
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/services/test_deploy_preflight.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/services/deploy_preflight.py PythonDataService/tests/services/test_deploy_preflight.py
git commit -m "feat: author deploy-context operator blockers"
```

---

## Task 3: `GET /deploy-preflight` endpoint

**Files:**
- Modify: `PythonDataService/app/routers/live_instances.py` (add handler + a `_gather_deploy_preflight_signals` helper near the other deploy helpers, ~line 2044)
- Test: `PythonDataService/tests/routers/test_deploy_preflight_endpoint.py`

**Interfaces:**
- Consumes: `DeployPreflightSignals` + `author_deploy_blockers` (Task 2); `DeployPreflightResponse` (Task 1).
- Produces: route `GET /api/live-instances/deploy-preflight?strategy_key&account_id&instance_id` → `DeployPreflightResponse`. `ready == not any(b.severity == "blocking" for b in blockers)`.

Signal sources (from reconnaissance):
- daemon: `await host_daemon_client.fetch_health(settings.live_runner_daemon_url)` → reachable iff result kind is CONNECTED (mirror `GET /daemon-health`, `live_instances.py:3028`).
- broker: `snapshot_data_plane_broker()` (`app/broker/runtime_snapshot.py:130`) → `.connection_state` (enum value → `str` via `.value`, or `None`).
- freeze: `read_account_freeze(Path(settings.live_runs_root).parent, account_id)` (`account_artifacts.py:218`) → frozen iff not None and `cleared_at_ms is None`.
- proven: `get_account_truth_snapshot_provider().get(account_id)` → `assess_account_truth(evidence, now_ms=_now_ms())` (`account_truth_snapshot.py:159`) → proven iff `assessment.status == "pass"`.
- fleet: reuse the existing `GET /account` computation returning `FleetContamination`; call the same service and read `.policy_blocks_starts`.
- validated: `load_strategy_validation_entries(...)` (`strategy_validation_manifest.py:131`); `strategy_deployable = any(e.strategy_key == strategy_key and e.deployable for e in entries)`.
- already running: `any(i.strategy_instance_id == instance_id and i.process_state in ("running","stopping") for i in <instances summary>)` (reuse the `GET ""` summary builder).

- [ ] **Step 1: Write the failing test**

```python
# PythonDataService/tests/routers/test_deploy_preflight_endpoint.py
from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.main import app


@pytest.fixture
def _patch_signals(monkeypatch):
    """Patch the signal-gathering helper so the endpoint test is hermetic."""
    from app.routers import live_instances
    from app.services.deploy_preflight import DeployPreflightSignals

    def _install(**overrides):
        base = dict(
            daemon_reachable=True,
            broker_connection_state="connected",
            account_frozen=False,
            account_proven=True,
            fleet_blocks_starts=False,
            strategy_deployable=True,
            instance_already_running=False,
        )
        base.update(overrides)

        async def _fake(strategy_key, account_id, instance_id):
            return DeployPreflightSignals(**base)

        monkeypatch.setattr(live_instances, "_gather_deploy_preflight_signals", _fake)

    return _install


async def _get(params):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/live-instances/deploy-preflight", params=params)


async def test_preflight_ready_when_all_healthy(_patch_signals):
    _patch_signals()
    resp = await _get({"strategy_key": "spy_ema", "account_id": "DUM1", "instance_id": "bot1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["blockers"] == []


async def test_preflight_blocks_when_broker_down(_patch_signals):
    _patch_signals(broker_connection_state="disconnected")
    resp = await _get({"strategy_key": "spy_ema", "account_id": "DUM1", "instance_id": "bot1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(b["id"] == "broker_disconnected" for b in body["blockers"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/routers/test_deploy_preflight_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'app.routers.live_instances' has no attribute '_gather_deploy_preflight_signals'` (and 404 for the route).

- [ ] **Step 3: Write minimal implementation**

Add imports near the top of `live_instances.py` (with the other service imports):

```python
from app.services.deploy_preflight import (
    DeployPreflightSignals,
    author_deploy_blockers,
)
from app.schemas.operator_blocker import DeployPreflightResponse
```

Add the helper + route (place the route just above `deploy_instance` at line 2079; the helper just above it):

```python
async def _gather_deploy_preflight_signals(
    strategy_key: str,
    account_id: str,
    instance_id: str,
) -> DeployPreflightSignals:
    """Resolve the seven deploy preconditions server-side (see plan Task 3)."""
    settings = get_settings()

    daemon_result, _ = await host_daemon_client.fetch_health(settings.live_runner_daemon_url)
    daemon_reachable = daemon_result.kind == "CONNECTED"

    broker_snapshot = snapshot_data_plane_broker()
    conn = broker_snapshot.connection_state
    broker_connection_state = conn.value if conn is not None else None

    artifacts_root = Path(settings.live_runs_root).parent
    freeze = read_account_freeze(artifacts_root, account_id)
    account_frozen = freeze is not None and freeze.cleared_at_ms is None

    truth_evidence = get_account_truth_snapshot_provider().get(account_id)
    account_proven = assess_account_truth(truth_evidence, now_ms=_now_ms()).status == "pass"

    fleet = _compute_fleet_contamination_for_account(account_id)
    fleet_blocks_starts = fleet.policy_blocks_starts

    entries = load_strategy_validation_entries(strategy_registry_seeds())
    strategy_deployable = any(e.strategy_key == strategy_key and e.deployable for e in entries)

    running_instances = _live_instance_summaries()
    instance_already_running = any(
        i.strategy_instance_id == instance_id and i.process_state in ("running", "stopping")
        for i in running_instances
    )

    return DeployPreflightSignals(
        daemon_reachable=daemon_reachable,
        broker_connection_state=broker_connection_state,
        account_frozen=account_frozen,
        account_proven=account_proven,
        fleet_blocks_starts=fleet_blocks_starts,
        strategy_deployable=strategy_deployable,
        instance_already_running=instance_already_running,
    )


@router.get("/deploy-preflight", response_model=DeployPreflightResponse)
async def deploy_preflight(
    strategy_key: str,
    account_id: str,
    instance_id: str,
) -> DeployPreflightResponse:
    """Server-authored deploy readiness: the blockers standing between the
    operator and a running bot, each with its move. ``ready`` is false iff any
    blocker is ``blocking`` (ADR-0013; see plan Slice 1)."""
    signals = await _gather_deploy_preflight_signals(strategy_key, account_id, instance_id)
    blockers = author_deploy_blockers(signals)
    ready = not any(b.severity == "blocking" for b in blockers)
    return DeployPreflightResponse(ready=ready, blockers=blockers)
```

> **Implementer note:** `_compute_fleet_contamination_for_account`, `strategy_registry_seeds`, and `_live_instance_summaries` are the existing helpers already backing `GET /account` (`live_instances.py:3142`), the strategy-validation load, and `GET ""` (`live_instances.py:1465`). Grep for the exact names used at those routes and reuse them verbatim — do not duplicate the aggregation. If a route inlines the computation rather than exposing a helper, extract the minimal helper as part of this task and call it from both sites (DRY).

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/routers/test_deploy_preflight_endpoint.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the backend project-scope lint + the three new test files**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Run: `podman exec polygon-data-service python -m pytest tests/schemas/test_operator_blocker.py tests/services/test_deploy_preflight.py tests/routers/test_deploy_preflight_endpoint.py -v`
Expected: ruff clean; all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/routers/live_instances.py PythonDataService/tests/routers/test_deploy_preflight_endpoint.py
git commit -m "feat: add GET /deploy-preflight endpoint"
```

---

## Task 4: Frontend contract types + service method

**Files:**
- Create: `Frontend/src/app/api/operator-blocker.types.ts`
- Modify: `Frontend/src/app/services/live-runs.service.ts`

**Interfaces:**
- Produces (TS): `Disposition`, `NavigateAction`, `ConfirmInFormAction`, `OperatorAction`, `OperatorMove`, `OperatorBlocker`, `DeployPreflightResponse`; `LiveRunsService.deployPreflight(params): Promise<DeployPreflightResponse>`.

- [ ] **Step 1: Write the type mirror**

```typescript
// Frontend/src/app/api/operator-blocker.types.ts
// TS mirror of PythonDataService/app/schemas/operator_blocker.py. Backend
// authors all prose (headline/detail/label); the frontend renders verbatim.
export type Disposition = 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';

export interface NavigateAction {
  kind: 'navigate';
  route: string;
  fragment: string | null;
}

export interface ConfirmInFormAction {
  kind: 'confirm_in_form';
  anchor: string;
}

// Superset grows in Slice 2.
export type OperatorAction = NavigateAction | ConfirmInFormAction;

export interface OperatorMove {
  label: string;
  action: OperatorAction;
  target: string | null;
}

export type BlockerSeverity = 'blocking' | 'warning';

export interface OperatorBlocker {
  id: string;
  severity: BlockerSeverity;
  disposition: Disposition;
  headline: string;
  detail: string | null;
  primary_move: OperatorMove | null;
  secondary_moves: OperatorMove[];
  applies_to: 'deploy' | 'run' | 'both';
}

export interface DeployPreflightResponse {
  ready: boolean;
  blockers: OperatorBlocker[];
}
```

- [ ] **Step 2: Add the service method**

In `Frontend/src/app/services/live-runs.service.ts`, add the import and method (mirror `getInstanceStatus`, line 248):

```typescript
// add to imports:
import type { DeployPreflightResponse } from '../api/operator-blocker.types';

// add as a method on LiveRunsService:
deployPreflight(params: {
  strategyKey: string;
  accountId: string;
  instanceId: string;
}): Promise<DeployPreflightResponse> {
  const httpParams = new HttpParams()
    .set('strategy_key', params.strategyKey)
    .set('account_id', params.accountId)
    .set('instance_id', params.instanceId);
  return firstValueFrom(
    this.http.get<DeployPreflightResponse>(`${this.instancesBase}/deploy-preflight`, {
      params: httpParams,
    }),
  );
}
```

- [ ] **Step 3: Type-check**

Run: `podman exec my-frontend npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/api/operator-blocker.types.ts Frontend/src/app/services/live-runs.service.ts
git commit -m "feat: add operator-blocker TS types + deployPreflight service method"
```

---

## Task 5: Form-blocker builder + move renderer (pure)

**Files:**
- Create: `Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.ts`
- Test: `Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.spec.ts`

**Interfaces:**
- Consumes: `OperatorBlocker`, `OperatorMove` (Task 4).
- Produces:
  - `buildFormBlockers(input: FormBlockerInput): OperatorBlocker[]` — client-side, form-derived blockers (`fix_here`, `confirm_in_form` move) for: missing required fields, identity coherence unconfirmed, exposure coherence unconfirmed, sizing error, action-plan legs not ready.
  - `resolveBlockerMove(move: OperatorMove, deps: MoveDispatch): RenderedMove | null` — pure mapping to `{ label, variant, invoke }`.
  - `deployReady(blockers: OperatorBlocker[]): boolean` — `blockers.every(b => b.severity !== 'blocking')`.

- [ ] **Step 1: Write the failing test**

```typescript
// Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.spec.ts
import { describe, expect, it, vi } from 'vitest';
import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { buildFormBlockers, deployReady, resolveBlockerMove } from './deploy-blockers';

describe('buildFormBlockers', () => {
  const ready = {
    missingRequiredFields: [] as string[],
    identityConflictSummary: null,
    exposureConflictSummary: null,
    customSizingError: null,
    actionPlanReady: true,
  };

  it('returns no blockers when the form is complete', () => {
    expect(buildFormBlockers(ready)).toEqual([]);
  });

  it('emits a blocking fix_here blocker listing missing fields', () => {
    const blockers = buildFormBlockers({ ...ready, missingRequiredFields: ['Strategy', 'Deployment name'] });
    expect(blockers).toHaveLength(1);
    expect(blockers[0].id).toBe('missing_required_fields');
    expect(blockers[0].severity).toBe('blocking');
    expect(blockers[0].disposition).toBe('fix_here');
    expect(blockers[0].detail).toContain('Strategy');
  });

  it('emits an identity-coherence blocker with a confirm-in-form move', () => {
    const blockers = buildFormBlockers({ ...ready, identityConflictSummary: 'Symbol mismatch' });
    const match = blockers.find((b) => b.id === 'identity_coherence_unconfirmed');
    expect(match?.disposition).toBe('fix_here');
    expect(match?.primary_move?.action.kind).toBe('confirm_in_form');
  });
});

describe('deployReady', () => {
  it('is false when any blocker is blocking', () => {
    const b: OperatorBlocker = {
      id: 'x', severity: 'blocking', disposition: 'fix_elsewhere', headline: 'h',
      detail: null, primary_move: null, secondary_moves: [], applies_to: 'deploy',
    };
    expect(deployReady([b])).toBe(false);
  });
  it('is true when all blockers are warnings', () => {
    const b: OperatorBlocker = {
      id: 'x', severity: 'warning', disposition: 'wait', headline: 'h',
      detail: null, primary_move: null, secondary_moves: [], applies_to: 'deploy',
    };
    expect(deployReady([b])).toBe(true);
  });
});

describe('resolveBlockerMove', () => {
  it('navigates for a navigate action', () => {
    const navigate = vi.fn();
    const rendered = resolveBlockerMove(
      { label: 'Connect the broker', action: { kind: 'navigate', route: '/broker', fragment: null }, target: null },
      { navigate, focusAnchor: vi.fn() },
    );
    rendered?.invoke();
    expect(navigate).toHaveBeenCalledWith('/broker', null);
    expect(rendered?.variant).toBe('link');
  });

  it('focuses an anchor for a confirm_in_form action', () => {
    const focusAnchor = vi.fn();
    const rendered = resolveBlockerMove(
      { label: 'Confirm identity', action: { kind: 'confirm_in_form', anchor: 'coherence-card' }, target: null },
      { navigate: vi.fn(), focusAnchor },
    );
    rendered?.invoke();
    expect(focusAnchor).toHaveBeenCalledWith('coherence-card');
    expect(rendered?.variant).toBe('primary');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec my-frontend npx ng test --watch=false --include='**/deploy-blockers.spec.ts'`
Expected: FAIL — cannot resolve `./deploy-blockers`.

- [ ] **Step 3: Write minimal implementation**

```typescript
// Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.ts
import type { OperatorBlocker, OperatorMove } from '../../../api/operator-blocker.types';

export interface FormBlockerInput {
  missingRequiredFields: string[];
  identityConflictSummary: string | null;
  exposureConflictSummary: string | null;
  customSizingError: string | null;
  actionPlanReady: boolean;
}

function confirmInForm(label: string, anchor: string): OperatorMove {
  return { label, action: { kind: 'confirm_in_form', anchor }, target: null };
}

export function buildFormBlockers(input: FormBlockerInput): OperatorBlocker[] {
  const blockers: OperatorBlocker[] = [];

  if (input.missingRequiredFields.length > 0) {
    blockers.push({
      id: 'missing_required_fields',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Deployment details incomplete',
      detail: `Missing: ${input.missingRequiredFields.join(', ')}.`,
      primary_move: confirmInForm('Complete the form', 'strategy-section'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.identityConflictSummary !== null) {
    blockers.push({
      id: 'identity_coherence_unconfirmed',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Run identity needs confirmation',
      detail: input.identityConflictSummary,
      primary_move: confirmInForm('Confirm identity', 'identity-coherence-card'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.exposureConflictSummary !== null) {
    blockers.push({
      id: 'exposure_coherence_unconfirmed',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Exposure needs confirmation',
      detail: input.exposureConflictSummary,
      primary_move: confirmInForm('Confirm exposure', 'exposure-launch-decision'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (input.customSizingError !== null) {
    blockers.push({
      id: 'sizing_invalid',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Sizing is invalid',
      detail: input.customSizingError,
      primary_move: confirmInForm('Fix sizing', 'sizing-section'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  if (!input.actionPlanReady) {
    blockers.push({
      id: 'action_plan_incomplete',
      severity: 'blocking',
      disposition: 'fix_here',
      headline: 'Entry/exit legs incomplete',
      detail: 'Add a valid entry leg and a matching close leg before deploying.',
      primary_move: confirmInForm('Fix the legs', 'action-plan-picker-heading'),
      secondary_moves: [],
      applies_to: 'deploy',
    });
  }

  return blockers;
}

export function deployReady(blockers: OperatorBlocker[]): boolean {
  return blockers.every((b) => b.severity !== 'blocking');
}

export interface MoveDispatch {
  navigate(route: string, fragment: string | null): void;
  focusAnchor(anchor: string): void;
}

export interface RenderedMove {
  label: string;
  variant: 'primary' | 'link';
  invoke(): void;
}

export function resolveBlockerMove(move: OperatorMove, deps: MoveDispatch): RenderedMove | null {
  switch (move.action.kind) {
    case 'navigate': {
      const { route, fragment } = move.action;
      return { label: move.label, variant: 'link', invoke: () => deps.navigate(route, fragment) };
    }
    case 'confirm_in_form': {
      const { anchor } = move.action;
      return { label: move.label, variant: 'primary', invoke: () => deps.focusAnchor(anchor) };
    }
    default:
      return null;
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec my-frontend npx ng test --watch=false --include='**/deploy-blockers.spec.ts'`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.ts Frontend/src/app/components/broker/broker-deploy-form/deploy-blockers.spec.ts
git commit -m "feat: add deploy form-blocker builder + move renderer"
```

---

## Task 6: Wire the deploy form to the contract (single hard-blocked button)

**Files:**
- Modify: `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.ts`
- Modify: `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.html`
- Modify (extend): `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.spec.ts`

**Interfaces:**
- Consumes: `LiveRunsService.deployPreflight` (Task 4); `buildFormBlockers`, `deployReady`, `resolveBlockerMove` (Task 5).
- Produces: a merged `blockers()` computed, a `ready()`/`canSubmit` gate, and a single `[Deploy & run]` button.

**6a — Add the preflight resource + merged blockers (component .ts)**

- [ ] **Step 1: Add imports**

Replace the `deploy-readiness` import block (lines 58–67) — keep only `actionPlanDeployReadiness` — and add the new imports:

```typescript
import { actionPlanDeployReadiness } from './deploy-readiness';
import { buildFormBlockers, deployReady, resolveBlockerMove, type RenderedMove } from './deploy-blockers';
import type { OperatorBlocker } from '../../../api/operator-blocker.types';
import { Router } from '@angular/router';
```

Add to the injected deps (near line 118): `private readonly router = inject(Router);`

- [ ] **Step 2: Add the preflight resource + blocker computeds**

Add these members to the class (near the other resources, after `positions`, ~line 144):

```typescript
readonly deployPreflight = resource<DeployPreflightResponse | null, { strategyKey: string; accountId: string; instanceId: string } | null>({
  params: () => {
    const strategyKey = this.strategyKey().trim();
    const accountId = this.accountId().trim();
    const instanceId = this.instanceId().trim();
    if (strategyKey === '' || accountId === '') return null;
    return { strategyKey, accountId, instanceId: instanceId === '' ? '__unnamed__' : instanceId };
  },
  loader: ({ params }) => (params === null ? Promise.resolve(null) : this.svc.deployPreflight(params)),
  defaultValue: null,
});

readonly formBlockers = computed<OperatorBlocker[]>(() =>
  buildFormBlockers({
    missingRequiredFields: this.missingRequiredFields(),
    identityConflictSummary: this.identityCoherenceBlock()?.summary ?? null,
    exposureConflictSummary: this.exposureCoherenceBlock()?.summary ?? null,
    customSizingError: this.customSizingError(),
    actionPlanReady: this.actionPlanReadiness().canDeploy,
  }),
);

readonly blockers = computed<OperatorBlocker[]>(() => [
  ...(this.deployPreflight.value()?.blockers ?? []),
  ...this.formBlockers(),
]);

readonly ready = computed<boolean>(() => deployReady(this.blockers()));

readonly topBlocker = computed<OperatorBlocker | null>(() => {
  const blocking = this.blockers().filter((b) => b.severity === 'blocking');
  return blocking[0] ?? null;
});

renderMove(blocker: OperatorBlocker): RenderedMove | null {
  const move = blocker.primary_move;
  if (move === null) return null;
  return resolveBlockerMove(move, {
    navigate: (route, fragment) =>
      void this.router.navigate([route], fragment ? { fragment } : {}),
    focusAnchor: (anchor) => {
      this.setActiveDeployTab('strategy');
      this.host.nativeElement.querySelector(`#${anchor}`)?.scrollIntoView({ block: 'center' });
    },
  });
}
```

> Add `DeployPreflightResponse` to the `operator-blocker.types` import.

**6b — Remove the deploy-only path + legacy readiness derivation**

- [ ] **Step 3: Delete legacy members**

Delete these members entirely (they are replaced by `blockers`/`ready`/`topBlocker`):
`deployReadinessFacts` (423–435), `nowChecks` (462–471), `deployChecks` (473), `preSubmitBlocker` (598–620), `activeBlocker` (622–626), `blockedReason` (628–630), `stoppedStartLatchStatus` (474–487), `stoppedStartLatch` (488), `instanceStatus` resource (126–132), `instanceAlreadyRunning` (437–442; now server-owned), `commandTitle` (490–492), `commandButtonLabel` (493–495), `deployWithoutStarting` (915–920).

Delete the `startNow` signal (163) and `effectiveStartNow` (489). Replace `effectiveStartNow()` usages:
- `identityCoherenceBlock` (line 343): drop the `!this.effectiveStartNow()` condition — the block applies whenever evidence exists and is unconfirmed.
- `exposureCoherenceBlock` (line 380): same.
- `submit()` request `start` (line 655): set `start: true`.
- `submit()` `start_options` guard (line 684): always attach (remove the `if (this.effectiveStartNow())` wrapper, keep the body).
- `loadInstanceStatus`/`instanceStatus` references: removed with the resource.

- [ ] **Step 4: Rewrite `commandState` / `canSubmit` around `ready()`**

Replace `commandState` (212–238), `commandStatus` (239), and `canSubmit` (632):

```typescript
readonly commandState = computed<DeployCommandState>(() => {
  if (this.busy()) return { kind: 'busy', message: 'Submitting deployment.', canSubmit: false };
  const accepted = this.postSubmitCommandStatus();
  if (accepted !== null) return { kind: 'accepted', message: accepted, canSubmit: false };
  const top = this.topBlocker();
  if (top !== null) return { kind: 'blocked', message: `Can't deploy — ${top.headline.toLowerCase()}.`, canSubmit: false };
  return { kind: 'ready', message: 'Ready to deploy & run.', canSubmit: true };
});
readonly commandStatus = computed<string>(() => this.commandState().message);
readonly canSubmit = computed<boolean>(() => this.ready() && this.commandState().canSubmit);
```

Remove the now-unused `DeployCommandState.actionLink` field and the `AccountProofBlock` import if nothing else references them (the type-check in Step 6 will confirm).

- [ ] **Step 5: Remove the `setStartNow` handler**

Delete `setStartNow` (890–897). Its only caller is the template checkbox removed in 6c.

**6c — Template: blocker list + single button (component .html)**

- [ ] **Step 6: Edit the template**

In `broker-deploy-form.component.html`:
1. **Remove** the "Start trading immediately" checkbox (the control bound to `setStartNow` / `startNow`).
2. **Remove** the readiness-facts region (the block iterating `deployReadinessFacts` / `nowChecks`) and the `deployWithoutStarting` button.
3. **Insert** the blocker list + single button where the command panel renders (near the element bound to `commandStatus` / `canSubmit`):

```html
@if (blockers().length > 0) {
  <ul class="deploy-blockers" aria-label="Deploy blockers">
    @for (blocker of blockers(); track blocker.id) {
      <li class="deploy-blocker" [class.deploy-blocker--blocking]="blocker.severity === 'blocking'">
        <p class="deploy-blocker__headline">{{ blocker.headline }}</p>
        @if (blocker.detail) {
          <p class="deploy-blocker__detail">{{ blocker.detail }}</p>
        }
        @let move = renderMove(blocker);
        @if (move !== null) {
          <button
            type="button"
            class="deploy-blocker__move"
            [class.deploy-blocker__move--primary]="move.variant === 'primary'"
            (click)="move.invoke()"
          >
            {{ move.label }}
          </button>
        }
      </li>
    }
  </ul>
}

<button
  type="button"
  class="deploy-command__submit"
  [disabled]="!canSubmit()"
  (click)="submit()"
>
  Deploy &amp; run
</button>
<p class="deploy-command__status" aria-live="polite">{{ commandStatus() }}</p>
```

> The `@let move = renderMove(blocker)` calls a method per row; that is acceptable here (small list, OnPush, no heavy work). Keep the SCSS minimal — reuse existing deploy-panel tokens; do not introduce Tailwind to this file if it doesn't already use it.

**6d — Component spec**

- [ ] **Step 7: Write/extend the failing component test**

Add to `broker-deploy-form.component.spec.ts` (provide a fake `LiveRunsService.deployPreflight`; follow the existing spec's provider setup):

```typescript
it('disables Deploy & run and names the blocker when preflight returns a blocking blocker', async () => {
  // Arrange: deployPreflight returns a broker_disconnected blocker (fake service).
  // ...render with the fake providers used by the other tests in this file...
  const button = screen.getByRole('button', { name: /deploy & run/i });
  expect(button).toBeDisabled();
  expect(screen.getByText(/broker disconnected/i)).toBeInTheDocument();
  expect(screen.getByText(/can't deploy — broker disconnected/i)).toBeInTheDocument();
});

it('enables Deploy & run when preflight is ready and the form is complete', async () => {
  // Arrange: deployPreflight returns { ready: true, blockers: [] } and all
  // required fields are filled (reuse the existing "happy path" fill helper).
  const button = screen.getByRole('button', { name: /deploy & run/i });
  expect(button).toBeEnabled();
});
```

> **Implementer note:** the existing spec already constructs the component with faked `LiveRunsService`/`BrokerService`/`BrokerConnectivityService`. Extend that same harness with a `deployPreflight` stub returning a `DeployPreflightResponse`. Remove any existing assertions that reference the deleted `deployReadinessFacts` / `Start trading immediately` / `Deploy only` UI.

- [ ] **Step 8: Run the component spec**

Run: `podman exec my-frontend npx ng test --watch=false --include='**/broker-deploy-form.component.spec.ts'`
Expected: PASS (the two new tests + the retained ones).

- [ ] **Step 9: Project-scope frontend lint + type-check**

Run: `npx eslint Frontend/src/ --max-warnings 0`
Run: `podman exec my-frontend npx tsc --noEmit`
Expected: zero warnings, zero type errors. (Fix any dangling references to deleted symbols — e.g. `deploy-readiness.ts` exports no longer imported; leave `deploy-readiness.ts` in place, it still exports `actionPlanDeployReadiness`.)

- [ ] **Step 10: Commit**

```bash
git add Frontend/src/app/components/broker/broker-deploy-form/
git commit -m "feat: drive deploy form from backend preflight; single hard-blocked button"
```

---

## Task 7: Full-suite verification + DoD check

- [ ] **Step 1: Backend project-scope tests**

Run: `podman exec polygon-data-service python -m pytest tests/schemas/test_operator_blocker.py tests/services/test_deploy_preflight.py tests/routers/test_deploy_preflight_endpoint.py tests/routers/test_live_instances.py -v`
Expected: PASS. (Include `test_live_instances.py` to confirm the new route didn't disturb the router.)

- [ ] **Step 2: Frontend deploy-form + blockers suites**

Run: `podman exec my-frontend npx ng test --watch=false --include='**/broker-deploy-form.component.spec.ts' --include='**/deploy-blockers.spec.ts'`
Expected: PASS.

- [ ] **Step 3: Project-scope lint (both stacks)**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/`
Run: `npx eslint Frontend/src/ --max-warnings 0`
Expected: clean.

- [ ] **Step 4: DoD walk-through (manual, documented in the PR description)**

Confirm each acceptance criterion, with the mechanism:
- Broker `disconnected` → `deploy_preflight` returns `ready:false` + `broker_disconnected` → button disabled, "Connect the broker →" shown. ✓
- Daemon down → `daemon_down` blocker → "Start the engine →". ✓
- Account frozen → `account_frozen` blocker → "Open account monitor →". ✓
- Account not proven / fleet contaminated / strategy not validated → each newly enforced (were unchecked before). ✓
- Healthy + complete form → button enabled, "Ready to deploy & run." ✓
- No deploy-only path remains (no "Start trading immediately" checkbox, no "Deploy only" button). ✓

- [ ] **Step 5: Run the thermo-nuclear review before opening the PR** (per repo hard rule — the user invokes it), address every major finding, then push.

---

## Notes for the reviewer

- **Slice boundary:** this slice adds the contract + deploy consumption only. The `OperatorAction` union is intentionally the 2-member deploy subset; Slice 2 extends it with `invoke_capability`/`invoke_endpoint`/`redeploy`/`open_runbook`/`retire_replace`/`remove` and adds `terminal` blockers to the bot-control surface.
- **Anti-drift:** the Slice-3 parity test (a shared blocker id rendering identically on deploy + run surfaces) is not in this slice because the run surface doesn't emit blockers yet.
- **`deploy-readiness.ts` is not deleted** — it still exports `actionPlanDeployReadiness`. Only the readiness-facts / `deployBlocker` / `stoppedStartLatch` exports become unused; a follow-up may prune them once no importer remains.

# Lane D: unscoped routes, auth posture, CONTEXT.md, backend cleanups (#2069, #2075, #2076 backend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four PRs: exempt proven coordinator forwards from compatibility retirement (leads; latent outage), delete the three dead unscoped bot mutations with an absence contract, refuse unpinned mutations in code on clerk_agent and correct CONTEXT.md, then seven verified cleanups.

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose, GitHub Actions.

**Spec:** the GitHub issues named in the title, plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md` — read it first; its Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular: never switch branches in `/Users/inkant/learn-ai` (the clerks bind-mount it); every `./restart.sh` drops both clerks; import-time changes need a no-bot window; run the thermo review once before the first push; fleet tests run unsandboxed.

## Corrections applied by the integrating session

- 22 of the 27 unscoped mutation routes are the agent transport the coordinator forwards to. Deleting them severs fleet routing. Only the three orphans named in Task 2 go.
- Task 5(c) deletes production_adapter(); Lane G item 19 must assert the function is ABSENT rather than keep the BrokerNotSupported assertion (Lane D wins).

---

*The report below was produced by an Opus planning agent on 2026-09-14 against origin/master `e2fdb23f` plus the branches of PR #2081 and PR #2078. Every file:line claim was verified by that agent at that time; re-verify after those PRs merge.*

---

# Plan — unscoped routes, auth posture, CONTEXT.md, backend cleanups

Verified against `origin/master` @ `e2fdb23f` plus `origin/fix/fleet-production-safety` (#2081) and `origin/docs/broker-clerk-fleet-authority` (#2078). Neither open PR touches any line this plan touches except `agent_identity.py` (#2081 edits lines 217–228 only; the `:211-215` early return is unchanged).

---

## 1. Verified facts

### #2069

- **CONFIRMED** `_SAFE_METHODS = frozenset({"GET", "HEAD"})` at `PythonDataService/app/broker/fleet/lane_runtime.py:31`; the guard is `lane_runtime.py:154`. `tests/broker/fleet/test_lane_runtime.py:243` already pins `compatibility_route_family("POST", "/api/brokers/alpaca/bots") is None`.
- **CONFIRMED** all nine cited mutation decorators (issue numbers are the `@router.post(` lines): `broker_bots.py:91` → `POST /{broker}/bots`; `:244` → `POST /{broker}/bots/{strategy_instance_id}/stop`; `broker_v2_panel.py:354` → `POST /{broker}/accounts/{account_id}/bots`; `:577` → `.../bots/{sid}/actions`; `:587` → `POST /{broker}/bots/{sid}/actions`; `:613` → `.../bots/cohort-flatten`; `:651` → `.../bots/cohort-archive`; `broker_configuration.py:391` → `PUT /api/brokers/alpaca/configuration/selection`; `:403` → `POST .../selection/apply`.
- **The issue's list is incomplete.** Enumerating the live app (`FLEET_ROLE=combined`, `FLEET_CONTROL_DIR` set) gives **27** mounted mutation routes under `/api/brokers/` with no `/clerks/` segment, not 9. The others: `broker_v2_panel.py:283,308,334`; `broker_configuration.py:170,217,243,261,292,324,339,370`; `brokers.py:506,528,564,615,648,774`; `run_replay.py:63`.
- **DRIFTED — and this changes the decision.** 22 of those 27 are the **agent surface the coordinator forwards to**: they are the resolution target of a catalog `agent_path_template`. `bot_create` → `broker_v2_panel.py:354`; `bot_panel_action` → `:577`; `bot_cohort_flatten` → `:613`; `bot_cohort_archive` → `:651`; `configuration_selection_stage` / `configuration_selection_apply` → `broker_configuration.py:391` / `:403`. They cannot be retired or deleted without severing fleet routing.
- **Only 5 are orphans** — no catalog operation resolves to them:
  - `POST /api/brokers/{broker}/bots` (`broker_bots.py:91`)
  - `POST /api/brokers/{broker}/bots/{strategy_instance_id}/stop` (`broker_bots.py:244`)
  - `POST /api/brokers/{broker}/bots/{sid}/actions` (`broker_v2_panel.py:587`)
  - `POST /api/brokers/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt` (`run_replay.py:63`)
  - `POST /api/brokers/{broker}/live-envelope/loss-hold/clear` (`brokers.py:774`)
- **Frontend callers: zero for all five.** Every mutation in `Frontend/src` builds its URL through `laneUrl`/`accountUrl` (`Frontend/src/app/fleet/clerk-scoped-url.ts:22,30`). The only three unscoped literals left in non-spec frontend code are all GETs: `brokers.service.ts:133` (`live-verdict`), `:199` (`order-groups`), `broker-v2-panel.service.ts:94` (`panel-profile`).
- **Script callers: zero for all five.** `scripts/dev/fleet/_api.py` uses `GET /api/brokers/{b}/bots` (`:95`) and the **account-scoped** `POST .../accounts/{acct}/bots` (`:180`) and `.../bots/{sid}/actions` (`:152`) — none of the orphans.
- **Test callers:** `POST /{broker}/bots` → `tests/routers/test_broker_bots.py:127,171,193,250`; `.../stop` → `:142,161,198,250`; `.../runs/{run_id}/replay-receipt` → `tests/routers/test_run_replay.py:80,92`; `loss-hold/clear` → `tests/routers/test_brokers_live_envelope.py:30,54`. **`POST /{broker}/bots/{sid}/actions` (`broker_v2_panel.py:587`, `run_action_unscoped`) has no caller anywhere — frontend, script, or test.**
- **CONFIRMED** the doc defect: `docs/design/fleet-b-route-inventory.md:191` heads the list "Unscoped **reads** the compatibility window keeps", and `:197` says `broker_bots` covers "list/**create/stop**", `:201` includes `/bots/{sid}/actions`, `:204-205` includes the whole `/api/brokers/alpaca/configuration/**` write surface.
- **CONFIRMED** retirement is all-families-or-nothing: `RETIRED_COMPATIBILITY_ROUTE_FAMILIES` (`compatibility_retirement.py:29-37`) is consumed as a whole set by `_validate_consumer_inventory` (`:missing_families`) and `_validate_retirement_receipt` (`set(families) != RETIRED_COMPATIBILITY_ROUTE_FAMILIES`); `write_route_state` takes no family argument.
- **CONFIRMED** the gate is clerk-only: `FleetLaneRuntimeMiddleware` is installed under `if _ROLE_RUNS_CLERK:` (`app/main.py:943,957-961`), while the coordinator's own compatibility router mounts under `if _FLEET_ROLE == "fleet_coordinator":` (`app/main.py:1236-1240`).
- **CONFIRMED** `POST /{broker}/bots` accepts `mode` verbatim (`broker_bots.py:97-107`) and calls `BotTaskRegistry.deploy`, which is a thin wrapper over `deploy_with_admission` (`app/services/bot_runner.py:393-419`). The scoped deploy goes through `panel_deploy.deploy_alpaca_paper_bot` → `deploy_with_admission` (`app/services/broker_v2_panel/panel_deploy.py:196`). So the unscoped route skips the panel preflight but not the runner admission.

### #2075

- **CONFIRMED, exact line.** `CONTEXT.md:2121` — the *Clerk-scoped surface* bullet ends "…it never serves an unscoped agent family itself, and a clerk agent mounts none of the routing surface."
- **The two unscoped reads the coordinator serves** are in `app/routers/fleet_compatibility_reads.py`: `GET /api/brokers/{broker}/live-verdict` (`:23-26`, delegating to `brokers.get_live_verdict`) and `GET /api/brokers/{broker}/panel-profile` (`:29-42`, delegating to `panel_profile_service.panel_profile_for`). Both are `brokers_lane_extras` / `broker_v2_panel` families under `compatibility_route_family`, and both are live frontend call sites (`brokers.service.ts:133`, `broker-v2-panel.service.ts:94`).
- **CONFIRMED** `app/broker/fleet/agent_identity.py:211-215` — `if "x-fleet-clerk-id" not in request_headers:` → `await self.app(...); return`. Unchanged on #2081.
- **WRONG path in the issue.** There is no `app/routers/data_plane_control.py`. The guard lives at `app/security/data_plane_control.py` (`require_data_plane_control_secret` `:52-66`, `_lane_forward_is_authorized` `:30-49`). The **test asserting 200** is `tests/broker/fleet/test_b_scoped_contracts.py:589` `test_the_composed_auth_policy_honors_both_caller_families`, assertion at **`:644`** (`browser = await client.post(...)` at `:640`).
- **CONFIRMED** `compose.fleet.yaml:97-100` (coordinator `ports:` with the comment "The coordinator is the only public fleet surface. Clerk agents have no host port and are reachable only on fleet-private.") and `:48-49` (the `x-clerk-agent` anchor's `networks: - fleet-private`).

### #2076 backend bullets

| # | Claim | Verdict |
|---|---|---|
| a | `draining` read, written nowhere | **CONFIRMED.** Read at `service.py:1206` and `:1702`. The only durable writer is `store.update_clerk_lifecycle` (`store.py:393-405`), called at `service.py:344` and `:448` — both with `RETIRED`. Schema permits the transition (`schema.py:79,262`). |
| b | Two diagnostic facts dropped before the wire | **CONFIRMED.** Computed at `service.py:1526,1528-1533`, placed in the observation at `:1561,1564`, handed to `adapter.provider_summary(observation)` at `:1571`. `AlpacaProviderAdapter.provider_summary` (`fleet_adapter.py:738-757`) copies only `confirmed_account_id`, `confirmed_binding_generation`, `endpoint_mode`, `authority_state`, `detail`. `ClerkDescriptor.public_fields` (`records.py:279-293`) has no other channel. `_project_lifecycle` (`service.py:1681-1713`) returns `STARTING` from two branches and `DEGRADED` from two. |
| c | `production_adapter()` phantom | **CONFIRMED, exact lines.** `provider.py:348-350`; `PRODUCTION_PROVIDER_ADAPTERS: Mapping[...] = {}` at `:297`; zero production callers (only `service.py:71,160` use the constant as a default). Live registry `fleet_composition.py:27-29`. **Stale test name CONFIRMED**: `tests/broker/fleet/test_import_isolation.py:96` — `test_the_production_adapter_registry_stays_empty_until_phase_2` — but Phase 2 landed (`fleet_composition.py` is Alpaca-live). |
| d | `bots_deploy_apply` declares a non-existent agent path | **CONFIRMED, and measured.** Catalog declares `POST /accounts/{account_id}/bots/deploy` → agent path `/api/brokers/alpaca/accounts/{account_id}/bots/deploy` (`fleet_adapter.py:344-351`). That path is mounted **GET-only** (`broker_v2_panel.py:257-258`). Resolving all **77** catalog operations against `app.routes` with Starlette `route.matches()`: **76 resolve `Match.FULL`; `bots_deploy_apply` alone resolves `Match.PARTIAL`** (path hits, method does not). No test checks this. |
| e | Two `assert isinstance` on the delivery path | **CONFIRMED.** `delivery.py:324` (`DeliveryResult`) and `:333` (`StreamDeliveryResult`), both in `LocalLaneDelivery`. |
| f | `routing.py:644` reaches into `service._store` | **CONFIRMED**, sole occurrence of `_store` in that module. |
| g | `broker_clerks.py` silent-empty for an unregistered broker | **CONFIRMED** — handler at `:109`, the unfiltered projection + return at `:111-119`. `service.adapters()` exists (`service.py:182-184`) and `BrokerNotSupported` is already imported in the module's sibling helper (`:157`). |
| h | `_PRIVATE_HOST_CACHE` has no TTL or eviction | **CONFIRMED** — `internal_http.py:56`, `dict[str, None]`, written at `:115`, read at `:92`, never cleared. Positive verdicts only (refusals re-resolve). Keys come from approved-endpoint rows and compose config, never from request data. |
| i | `_fence_writable_roots` docstring narrows its own scope | **CONFIRMED.** Docstring at `fleet_boot.py:390` says "…in the clerk-agent role"; the body (`:395-413`) has no role check and the call site (`:180`) runs for every enrolled lane including `combined`. |

### Facts the issue text did not contain

- **`compatibility_route_family` already matches 15 catalog operations' agent paths.** Measured: `configuration_owner_read`, `configuration_profiles_list`, `configuration_profile_read`, `configuration_revisions_list`, `configuration_revision_read`, `configuration_selection_read`, `configuration_nicknames_read`, `configuration_credential_slots`, `configuration_desk_state`, `configuration_events`, `activities_read`, `clerk_status_read`, `custody_diagnosis_read`, `portfolio_history_read`, `portfolio_history_proof_read`.
- **The retirement 410 is not exempted for coordinator forwards.** `lane_runtime.py:518` computes `family`; `:521` exempts the coordinator header **only for measurement** (`measurement_family`); the 410 at `:550` tests `family`. Applying Delivery-E retirement on a clerk agent today would 410 the coordinator's own forwarded reads for those 15 operations — the entire desk-configuration surface plus clerk status.
- **`restart.sh:21` is an unconditional `podman compose down`** — no service filter, no guard.

---

## 2. Decisions for the owner

**#2069 — delete vs extend: delete the three dead orphans, do not extend the mechanism.** Because 22 of the 27 unscoped mutations *are* the agent transport the coordinator forwards to, widening `_SAFE_METHODS` would arm an operator ceremony that permanently 410s fleet command delivery; only 5 routes are orphans and only 3 of those have a fleet successor.

**#2069 corollary (not in the issue, higher severity): exempt authenticated coordinator forwards from the retirement 410.** Because the mechanism as written already treats 15 live catalog agent paths as retirable compatibility reads, so the first Delivery-E ceremony takes out desk configuration and clerk status on a running lane.

**#2075 — refuse unpinned mutations in code: yes, on `clerk_agent` only.** Because a documented invariant enforced solely by a gitignored `compose.override.yaml` (see #2078 §5) is one lost file away from being false, and a role-scoped refusal costs one branch while leaving the `combined` posture — where the browser legitimately mutates without a pin — byte-for-byte unchanged.

---

## 3. Task breakdown

Environment for every Python command below: `cd /Users/inkant/learn-ai/PythonDataService`. Tests under `tests/broker/fleet/` bind sockets — run those with `dangerouslyDisableSandbox: true`.

---

### Task 1 — Exempt authenticated coordinator forwards from compatibility retirement

**Files**
- modify `PythonDataService/app/broker/fleet/lane_runtime.py`
- modify `PythonDataService/tests/broker/fleet/test_lane_runtime.py`

**Failing regression test** — append to `tests/broker/fleet/test_lane_runtime.py`:

```python
async def test_a_retired_lane_still_serves_an_authenticated_coordinator_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery E retires the browser alias, never the fleet's own transport."""
    from app.broker.fleet.compatibility_retirement import (
        CompatibilityRouteState,
        write_route_state,
    )
    from app.broker.fleet.delivery import COORDINATOR_TOKEN_HEADER
    from app.config import fleet_settings

    evidence = CompatibilityReadEvidence(tmp_path)
    write_route_state(
        state_path=tmp_path / "compatibility" / "route_state.json",
        state=CompatibilityRouteState.RETIRED,
        retirement_receipt=_eligible_receipt(),
    )
    monkeypatch.setattr(fleet_settings, "COORDINATOR_SERVICE_TOKEN", "svct_" + "a" * 32)

    inner = FastAPI()

    @inner.get("/api/brokers/alpaca/clerk/status")
    async def _status() -> dict[str, str]:
        return {"state": "ready"}

    inner.add_middleware(FleetLaneRuntimeMiddleware, config=None, evidence=evidence)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=inner), base_url="http://test"
    ) as client:
        browser = await client.get("/api/brokers/alpaca/clerk/status")
        forwarded = await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={
                COORDINATOR_TOKEN_HEADER: "svct_" + "a" * 32,
                "X-Fleet-Broker": "alpaca",
                "X-Fleet-Clerk-Id": "clk_test",
            },
        )
        spoofed = await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": "clk_test"},
        )

    assert browser.status_code == 410
    assert browser.json()["reason"] == "compatibility_read_retired"
    assert forwarded.status_code == 200
    assert spoofed.status_code == 410
```

`_eligible_receipt()` is the receipt builder the file already uses for its retirement tests; reuse it rather than minting a second one.

**Run / expected failure**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_lane_runtime.py::test_a_retired_lane_still_serves_an_authenticated_coordinator_forward -q
```
(`dangerouslyDisableSandbox: true`)
Expected: `AssertionError: assert 410 == 200` on the `forwarded` assertion.

**Minimal implementation** — in `lane_runtime.py`:

1. Add `import hmac` to the stdlib block (after `import contextlib`).
2. Below `_SAFE_METHODS` (line 31), add:

```python
#: A coordinator dispatch proves itself with the env-only service token *and*
#: the pinned lane identity — the same pair `app.security.data_plane_control`
#: requires. A header alone is client-forgeable and must stay retirable.
_COORDINATOR_TOKEN_HEADER_BYTES = b"x-fleet-coordinator-token"


def _is_authenticated_coordinator_forward(headers: Mapping[bytes, bytes]) -> bool:
    """Whether this request is the coordinator's own forwarded operation."""
    from app.config import fleet_settings

    supplied = headers.get(_COORDINATOR_TOKEN_HEADER_BYTES, b"")
    expected = (fleet_settings.COORDINATOR_SERVICE_TOKEN or "").strip().encode("utf-8")
    if not expected or not supplied:
        return False
    if b"x-fleet-clerk-id" not in headers or b"x-fleet-broker" not in headers:
        return False
    return hmac.compare_digest(supplied, expected)
```

3. Replace lines 518–521 with:

```python
        forwarded = _is_authenticated_coordinator_forward(headers)
        family = (
            None
            if forwarded
            else compatibility_route_family(method, str(scope.get("path", "")))
        )
        # An unauthenticated copy of the pin must not bypass E retirement, so
        # measurement drops only what the proven forward already excluded.
        measurement_family = None if b"x-fleet-clerk-id" in headers else family
```

The retirement branch at `:550` and the invalid-state branch at `:522` then both see `family is None` for a proven forward and the request passes through untouched.

**Run / expected pass**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet/test_lane_runtime.py -q
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: all `test_lane_runtime.py` tests pass (the existing measurement tests are unaffected — they set no coordinator token, so `COORDINATOR_SERVICE_TOKEN` is empty and `_is_authenticated_coordinator_forward` returns `False`).

**Commit**
```
fix(fleet): exempt proven coordinator forwards from compatibility retirement

Fifteen catalog operations' agent paths — the whole configuration surface,
clerk status, activities, portfolio history — are matched by
compatibility_route_family. The 410 at lane_runtime.py:550 tested `family`,
which the x-fleet-clerk-id exemption at :521 never reached: it only
suppressed measurement. Applying Delivery E on a running lane would have
410'd the coordinator's own forwarded reads.

Exempt on the unforgeable pair (coordinator service token + pinned lane
identity), the same evidence app/security/data_plane_control.py requires.
A client-supplied header copy is still retirable.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 2 — Delete the three dead unscoped bot mutations, with an absence contract

**Files**
- create `PythonDataService/tests/contracts/test_unscoped_bot_mutation_retirement.py`
- modify `PythonDataService/app/routers/broker_bots.py` (delete `deploy_bot` `:91-110`, `stop_bot` `:244-261`, drop two imports)
- modify `PythonDataService/app/routers/broker_v2_panel.py` (delete `run_action_unscoped` `:587-597`)
- modify `PythonDataService/app/schemas/broker_bots.py` (delete `DeployBotRequest` `:83`, `StopBotRequest` `:381`)
- modify `PythonDataService/tests/routers/test_broker_bots.py` (retarget four call sites)
- modify `docs/design/fleet-b-route-inventory.md`
- regenerate `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts`

**Failing regression test** — new file:

```python
"""Structural contract for the unscoped bot-mutation retirement (#2069).

The fleet's own transport is the *account-scoped* surface: every route here
had no catalog operation resolving to it, no frontend caller (Frontend builds
every mutation URL through fleet/clerk-scoped-url.ts) and no script caller.
The two unscoped mutations that remain — the replay-receipt recompute and the
loss-hold clear — are deliberately retained: they are operator routes with no
fleet successor, tracked as stranded in docs/design/fleet-b-route-inventory.md.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_APPLICATION_ROOT = REPOSITORY_ROOT / "Frontend" / "src" / "app"

RETIRED_UNSCOPED_BOT_MUTATIONS = {
    ("POST", "/api/brokers/{broker}/bots"),
    ("POST", "/api/brokers/{broker}/bots/{strategy_instance_id}/stop"),
    ("POST", "/api/brokers/{broker}/bots/{sid}/actions"),
}

RETAINED_STRANDED_UNSCOPED_MUTATIONS = {
    ("POST", "/api/brokers/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt"),
    ("POST", "/api/brokers/{broker}/live-envelope/loss-hold/clear"),
}

SUCCESSOR_ACCOUNT_SCOPED_MUTATIONS = {
    ("POST", "/api/brokers/{broker}/accounts/{account_id}/bots"),
    ("POST", "/api/brokers/{broker}/accounts/{account_id}/bots/{sid}/actions"),
}

RETIRED_REQUEST_SCHEMAS = ("DeployBotRequest", "StopBotRequest")


def _registered() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_unscoped_bot_mutation_routes_are_absent() -> None:
    registered = _registered()

    assert registered.isdisjoint(RETIRED_UNSCOPED_BOT_MUTATIONS)
    assert registered >= SUCCESSOR_ACCOUNT_SCOPED_MUTATIONS
    assert registered >= RETAINED_STRANDED_UNSCOPED_MUTATIONS


def test_retired_unscoped_deploy_and_stop_bodies_are_absent() -> None:
    """The request shapes had exactly one consumer each; both routes are gone."""
    import app.schemas.broker_bots as broker_bot_schemas

    for name in RETIRED_REQUEST_SCHEMAS:
        assert not hasattr(broker_bot_schemas, name), name


def test_the_frontend_builds_no_unscoped_bot_mutation_url() -> None:
    """Every Frontend mutation goes through fleet/clerk-scoped-url.ts."""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in FRONTEND_APPLICATION_ROOT.rglob("*.ts")
        if not path.name.endswith(".spec.ts")
        and path.name != "broker.types.ts"
    )
    for literal in ("}/bots`", "}/bots/${", "/bots/stop"):
        assert literal not in sources, literal
```

**Run / expected failure**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/contracts/test_unscoped_bot_mutation_retirement.py -q
```
Expected: two failures —
`test_unscoped_bot_mutation_routes_are_absent` → `AssertionError: assert {('POST', '/api/brokers/{broker}/bots'), ('POST', '/api/brokers/{broker}/bots/{sid}/actions'), ('POST', '/api/brokers/{broker}/bots/{strategy_instance_id}/stop')} is disjoint from …` (pytest renders it as `assert <set>.isdisjoint(<set>)` returning False), and
`test_retired_unscoped_deploy_and_stop_bodies_are_absent` → `AssertionError: DeployBotRequest`.

**Minimal implementation**

1. `app/routers/broker_bots.py`: delete the `@router.post("/{broker}/bots", …)` decorator and `async def deploy_bot` body (lines 91–110 inclusive) and the `@router.post("/{broker}/bots/{strategy_instance_id}/stop", …)` decorator and `async def stop_bot` body (lines 244–261 inclusive). Remove `DeployBotRequest,` and `StopBotRequest,` from the `app.schemas.broker_bots` import (lines 27–28). `_resolve_broker`, `_require_registry`, `_raise_runner_error`, `_require_account_binding`, `BotRunnerError` and `UnknownBotError` all keep other users — do not touch them.
2. `app/routers/broker_v2_panel.py`: delete lines 587–597 (`@router.post("/{broker}/bots/{sid}/actions", …)` through `return await _run_action(broker, account_id, sid, request)`). `_resolve_default_account` retains six other callers (`:250,421,718,764,829`) — leave it.
3. `app/schemas/broker_bots.py`: delete `class DeployBotRequest` (from `:83` to the line before the next top-level `class`) and `class StopBotRequest` (`:381` likewise). Remove them from `__all__` if present.
4. `tests/routers/test_broker_bots.py`: `test_deploy_stop_button_rule_end_to_end` (`:124`), `test_stop_unknown_bot_is_404` (`:158`), `test_invalid_strategy_instance_id_is_422` (`:168`), `test_current_and_previous_runs_are_lazy_read_only_views` (`:190`) and `test_run_reads_reject_unknown_bot_and_foreign_cursor` (`:244`) drive deploy/stop over HTTP. Replace each `await client.post("/api/brokers/alpaca/bots", …)` with a direct `await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")` on the `api` fixture's registry, and each `await client.post(f"/api/brokers/alpaca/bots/{_SID}/stop", json={})` with `await registry.stop("alpaca", _SID)`. The routes under test in these cases are the surviving GETs; the mutations were only setup. `test_stop_unknown_bot_is_404` and `test_invalid_strategy_instance_id_is_422` lose their subject — delete both, since the same refusals are proven at the service layer by `tests/services/test_bot_runner_*`.
5. `docs/design/fleet-b-route-inventory.md`: change `:191` from "Unscoped reads the compatibility window keeps" to "Unscoped **reads** the compatibility window keeps. Unscoped *mutations* are out of scope for this mechanism: `_SAFE_METHODS` in `app/broker/fleet/lane_runtime.py:31` matches `GET`/`HEAD` only, and 22 of the 27 unscoped mutation routes are the agent surface a catalog `agent_path_template` forwards to — retiring them would sever fleet routing, not a browser alias." Change `:197` from "(list/create/stop, runs current/history, instance read)" to "(list, runs current/history, instance read — create and stop were deleted by #2069, not retained)". Delete `actions` from the `/bots/{sid}/(panel|actions|chart/*|evidence)` list at `:201`. Change `:204-205` to say the `configuration/**` write half is the agent surface the coordinator forwards to, not a retirable alias. Add, under the "One stranded route" heading style used in `docs/broker-clerk-fleet-authority.md`, a short "Stranded unscoped mutations" subsection naming `POST …/runs/{run_id}/replay-receipt` and `POST …/live-envelope/loss-hold/clear` as retained-with-no-fleet-path, alongside the existing `GET …/order-groups`.
6. Regenerate contracts: `python scripts/export_openapi_contract.py` then, from `Frontend/`, `npm run codegen:openapi`.

**Run / expected pass**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/contracts/test_unscoped_bot_mutation_retirement.py \
  tests/routers/test_broker_bots.py tests/routers/test_run_replay.py \
  tests/routers/test_brokers_live_envelope.py tests/broker/v2panel -q
python scripts/export_openapi_contract.py --check
ruff check PythonDataService/app/ PythonDataService/tests/
```

**Commit**
```
feat(broker)!: delete three dead unscoped bot mutation routes (#2069)

POST /api/brokers/{broker}/bots, .../bots/{sid}/stop and .../bots/{sid}/actions
had no frontend caller (Frontend builds every mutation URL through
fleet/clerk-scoped-url.ts), no script caller, and no catalog operation
resolving to them. The unscoped deploy additionally skipped the panel
preflight and accepted mode='trade'.

The compatibility mechanism was never the answer: 22 of the 27 unscoped
mutation routes ARE the agent surface a catalog agent_path_template forwards
to. Deleting the orphans is; the two stranded operator mutations
(replay-receipt recompute, loss-hold clear) stay and are documented as such.

Corrects docs/design/fleet-b-route-inventory.md, which listed create/stop/
actions and the whole configuration write surface under "retained reads".

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 3 — CONTEXT.md: the two false broker-clerk-fleet claims

**Files**
- modify `CONTEXT.md` (lines 2121 and 2124)

**Test** — `tests/contracts/test_documentation_contract.py::test_validate_repository_current_repository_passes` walks every Markdown link in the repo (`scripts/check_documentation_contract.py`, `MARKDOWN_LINK`). Adding a broken relative link to `CONTEXT.md` fails it. No new test: the corrections are prose, and their subject is already pinned by Task 2's absence test and Task 4's negative test.

**Replacement wording**

`CONTEXT.md:2121`, replace from "The coordinator resolves" to end of bullet:

> The coordinator resolves, fences and forwards; a clerk agent mounts none of the routing surface. The one exception is deliberate and bounded: during the pre-cutover compatibility window the coordinator also serves two unscoped agent-family **reads** — `GET /api/brokers/{broker}/live-verdict` and `GET /api/brokers/{broker}/panel-profile` (`app/routers/fleet_compatibility_reads.py`) — because the browser's fleet ingress is the coordinator and those two reads have no clerk-scoped successor yet. No agent, generic broker router or mutation surface is exposed by that bridge.

`CONTEXT.md:2124`, replace the first sentence:

> **Composed auth** — public routes demand the data-plane control secret; an internal forward presents the per-clerk coordinator service token, accepted at the same guard (`app/security/data_plane_control.py`). The browser secret is *intended* to terminate at the coordinator, and on a `clerk_agent` that intent is enforced in code: a mutation carrying no `X-Fleet-Clerk-Id` pin and no proven coordinator token is refused `broker_and_clerk_required` before the handler (`app/broker/fleet/agent_identity.py`). Deployment topology is the second layer, not the only one — clerk agents have no host port and sit on `fleet-private` (`compose.fleet.yaml`). Cleartext internal traffic never leaves the private network boundary — an `http://` destination naming a public address is refused before any byte is sent.

**Run / expected pass**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q
```

**Commit**
```
docs(context): correct two false broker-clerk-fleet claims (#2075)

CONTEXT.md:2121 said the coordinator "never serves an unscoped agent family
itself"; app/routers/fleet_compatibility_reads.py serves two — live-verdict
and panel-profile. The bullet now names them and says why they are bounded.

CONTEXT.md:2124 stated the browser secret terminates at the coordinator as if
it were code. It is now, on clerk_agent, and the bullet cites the guard and
names topology as the second layer rather than the only one.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

Sequence this **after** Task 4 so the 2124 wording is true when it lands.

---

### Task 4 — A clerk agent refuses unpinned mutations in code

**"Unpinned" means, precisely:** an HTTP request whose method is in `{POST, PUT, PATCH, DELETE}`, arriving at a process whose `FLEET_ROLE == "clerk_agent"`, carrying neither `X-Fleet-Clerk-Id` nor a valid `X-Fleet-Coordinator-Token`. GET/HEAD are untouched (health checks, the qualification router — verified GET-only at `app/routers/fleet_qualification.py:195,224,233` — and diagnostic reads keep working). `combined` and `fleet_coordinator` are untouched.

**Refusal:** reuse `BrokerAndClerkRequired` from `app/broker/fleet/errors.py:44-48` — `reason: "broker_and_clerk_required"`, `status_code: 400`, docstring already "A clerk-scoped request without both path identities (PRD FR-070)". No new vocabulary member, so the 25-reason-code count and any frontend union are unchanged.

**Files**
- modify `PythonDataService/app/broker/fleet/agent_identity.py`
- modify `PythonDataService/app/main.py`
- modify `PythonDataService/tests/broker/fleet/test_b_scoped_contracts.py` (harness + the 200 assertion)
- modify `PythonDataService/tests/broker/fleet/test_identity_middleware_role_scope.py` (arrives with #2081)

**Failing regression test** — append to `tests/broker/fleet/test_b_scoped_contracts.py`:

```python
async def test_a_clerk_agent_refuses_an_unpinned_mutation_in_code(
    tmp_path: Path,
) -> None:
    """Topology is the second layer, not the only one (#2075)."""
    from app.broker.fleet.errors import BrokerAndClerkRequired
    from app.config import fleet_settings, settings

    agent = _build_agent_app(
        {"broker": "alpaca", "clerk_id": CLERK_ID,
         "routing_epoch": EPOCH, "binding_generation": 3},
        refuse_unpinned_mutations=True,
    )

    @agent.post("/guarded")
    async def guarded() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @agent.get("/guarded")
    async def guarded_read() -> PlainTextResponse:
        return PlainTextResponse("ok")

    server = _RealServer(agent)
    server.start()
    original_secret = settings.DATA_PLANE_CONTROL_SECRET
    original_token = fleet_settings.COORDINATOR_SERVICE_TOKEN
    settings.DATA_PLANE_CONTROL_SECRET = "test-plane-secret"
    fleet_settings.COORDINATOR_SERVICE_TOKEN = COORDINATOR_TOKEN
    try:
        async with httpx.AsyncClient(base_url=server.base_url, timeout=5.0) as client:
            unpinned = await client.post(
                "/guarded", headers={"X-Data-Plane-Control-Secret": "test-plane-secret"}
            )
            assert unpinned.status_code == BrokerAndClerkRequired.status_code
            assert unpinned.json()["reason"] == BrokerAndClerkRequired.reason
            assert unpinned.json()["next_step"]

            unpinned_read = await client.get("/guarded")
            assert unpinned_read.status_code == 200

            forwarded = await client.post(
                "/guarded",
                headers={
                    COORDINATOR_TOKEN_HEADER: COORDINATOR_TOKEN,
                    "X-Fleet-Broker": "alpaca",
                    "X-Fleet-Clerk-Id": CLERK_ID,
                },
            )
            assert forwarded.status_code == 200
    finally:
        settings.DATA_PLANE_CONTROL_SECRET = original_secret
        fleet_settings.COORDINATOR_SERVICE_TOKEN = original_token
        server.stop()
```

**Run / expected failure**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  "tests/broker/fleet/test_b_scoped_contracts.py::test_a_clerk_agent_refuses_an_unpinned_mutation_in_code" -q
```
(`dangerouslyDisableSandbox: true`)
Expected: `TypeError: _build_agent_app() got an unexpected keyword argument 'refuse_unpinned_mutations'`. After adding the harness parameter but before the middleware change: `AssertionError: assert 200 == 400`.

**Minimal implementation**

1. `agent_identity.py` — `FleetIdentityMiddleware.__init__` (line 187) gains a keyword:

```python
    def __init__(self, app: Any, *, refuse_unpinned_mutations: bool = False) -> None:
        """Wrap one ASGI app; a lane agent may also fence unpinned mutations."""
        self.app = app
        self._refuse_unpinned_mutations = refuse_unpinned_mutations
```

2. Replace lines 211–215 with:

```python
        if "x-fleet-clerk-id" not in request_headers:
            # Browser-direct traffic carries no pinned identity; the echo is
            # the forwarded lane's contract, not the public API's. On a lane
            # agent, though, an unpinned *mutation* has no legitimate caller:
            # the coordinator always pins, and the browser reaches the
            # coordinator. Refusing here makes the composed-auth claim code
            # rather than a property of a gitignored compose overlay (#2075).
            if self._refuse_unpinned_mutations and scope.get(
                "method", "GET"
            ).upper() in _MUTATING_METHODS and not _is_coordinator_forward(
                request_headers
            ):
                await _send_refusal(send, _unpinned_mutation_refusal())
                return
            await self.app(scope, receive, send)
            return
```

with, near `_pin_mismatch` (module level):

```python
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_coordinator_forward(request_headers: Mapping[str, str]) -> bool:
    """Whether this request proves itself as the coordinator's own dispatch."""
    import hmac

    from app.broker.fleet.delivery import COORDINATOR_TOKEN_HEADER
    from app.config import fleet_settings

    supplied = request_headers.get(COORDINATOR_TOKEN_HEADER.lower(), "")
    expected = (fleet_settings.COORDINATOR_SERVICE_TOKEN or "").strip()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _unpinned_mutation_refusal() -> BrokerAndClerkRequired:
    """The §10.4 family a lane agent answers an unpinned mutation with."""
    return BrokerAndClerkRequired(
        "This process serves one clerk lane and accepts mutations only from "
        "the fleet coordinator, which pins the broker and clerk it dispatched.",
        next_step="Send the command to the coordinator's clerk-scoped route "
        "at /api/brokers/{broker}/clerks/{clerk_id}/… instead.",
    )
```

`_send_refusal` is a three-line helper writing `http.response.start` with `error.status_code` and `[(b"content-type", b"application/json")]` then the `json.dumps(error.detail()).encode()` body — factor the existing `:234-246` send pair into it and call it from both sites, so the router-freeze principle's net-lines discipline is honoured. (`agent_identity.py` is ~330 lines, not a frozen live-control router; the freeze in `.claude/rules/python.md:47-66` currently has no subject and does not bind this file.)

Import `BrokerAndClerkRequired` at module top from `app.broker.fleet.errors` — permitted by `tests/broker/fleet/test_import_isolation.py` (`FORBIDDEN_PREFIXES` covers providers and provider routers only).

3. `app/main.py`: change the `if _ROLE_RUNS_CLERK:` block that #2081 introduces around line 977 to

```python
if _ROLE_RUNS_CLERK:
    # Only a separately deployed lane agent fences unpinned mutations. The
    # combined posture IS the browser's data plane and must stay byte-for-byte
    # the historical single process (#2075).
    app.add_middleware(
        FleetIdentityMiddleware,
        refuse_unpinned_mutations=_FLEET_ROLE == "clerk_agent",
    )
```

4. `tests/broker/fleet/test_b_scoped_contracts.py:44` — `_build_agent_app` gains `refuse_unpinned_mutations: bool = False` and passes it to `agent.add_middleware(FleetIdentityMiddleware, refuse_unpinned_mutations=refuse_unpinned_mutations)` at `:61`.

**Which existing test must change, and why that is correct**

`tests/broker/fleet/test_b_scoped_contracts.py:644` — `assert browser.status_code == 200` inside `test_the_composed_auth_policy_honors_both_caller_families`. It must become an explicit two-posture assertion: with `refuse_unpinned_mutations=False` (the `combined` posture) the browser secret still yields 200; the new test covers the `clerk_agent` posture at 400. That is correct because the assertion never proved a *policy* — it proved that `require_data_plane_control_secret` accepts a valid secret, which is true on every role. The docstring above it ("The browser secret terminates at the coordinator") described a posture the assertion contradicted: it pinned the agent *accepting* browser traffic. Splitting it makes the test say what the docstring says.

**Run / expected pass**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_b_scoped_contracts.py \
  tests/broker/fleet/test_identity_middleware_role_scope.py \
  tests/broker/fleet/test_import_isolation.py -q
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/test_data_plane_control_security.py -q
ruff check PythonDataService/app/ PythonDataService/tests/
```

**Commit**
```
feat(fleet): a clerk agent refuses unpinned mutations in code (#2075)

"The browser secret terminates at the coordinator" was documentation. An
unpinned request skipped FleetIdentityMiddleware entirely
(agent_identity.py:211-215) and the agent accepted the browser control
secret. What prevented browser-direct mutation was compose network topology
— delivered, per #2078 §5, entirely by a gitignored compose.override.yaml.

On FLEET_ROLE=clerk_agent only, a POST/PUT/PATCH/DELETE carrying neither
X-Fleet-Clerk-Id nor a proven coordinator token is now refused
broker_and_clerk_required (400) before the handler. Reads are untouched, and
combined stays byte-for-byte the historical single process.

The composed-auth test's `assert browser.status_code == 200` now asserts both
postures: it only ever proved the secret guard accepts a valid secret, which
its own docstring contradicted.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

### Task 5 — Backend cleanups, grouped by file

**Disposition.** Fix now: **b, c, d, e, f, g, i**. Defer: **a, h**.

- **Defer (a) `draining`.** The state is durably representable, the forward-only trigger (`schema.py:262`) already admits it, and both readers refuse correctly. Writing it needs a drain ceremony (quiesce in-flight routing, wait for the runner, then retire) that is a design decision, not a cleanup. Track as its own issue; the dead read is safe.
- **Defer (h) `_PRIVATE_HOST_CACHE`.** Keys come from approved-endpoint rows and compose config, never from request data, and only *private* verdicts are cached (refusals re-resolve every call). The DNS-rebinding shape needs a hostile controller of an approved endpoint row, which is the host ceremony. A TTL is cheap but changes a boundary check on the live credential path; not worth bundling into a cleanup PR.

**Files**
- create `PythonDataService/tests/contracts/test_fleet_agent_path_templates_resolve.py`  *(d)*
- modify `PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py` *(d, b)*
- modify `PythonDataService/app/broker/fleet/provider.py` *(c)*
- modify `PythonDataService/app/broker/fleet/delivery.py` *(e)*
- modify `PythonDataService/app/broker/fleet/routing.py` *(f)*
- modify `PythonDataService/app/broker/fleet/service.py` *(f)*
- modify `PythonDataService/app/broker/alpaca/clerk/fleet_boot.py` *(i)*
- modify `PythonDataService/app/routers/broker_clerks.py` *(g)*
- modify `PythonDataService/tests/broker/fleet/test_import_isolation.py` *(c, test rename)*
- modify `docs/design/fleet-b-route-inventory.md` *(d, drop the `bots_deploy_apply` row at `:90`)*
- regenerate the two contract files

**Failing regression test (d) — the valuable deliverable** — new file:

```python
"""Every catalog agent_path_template resolves to a mounted route (#2076).

The operation catalog is the single routing contract: the coordinator's
forwarding allowlist, the agent's mounts, the exported OpenAPI and the
generated frontend builders all derive from it. Nothing checked that an
agent_path_template names a route the agent actually serves, so
`bots_deploy_apply` declared POST .../bots/deploy against a GET-only mount —
a command the coordinator would persist a routing attempt for, dispatch, and
collect a 405 on.
"""

from __future__ import annotations

import re

from starlette.routing import Match

from app.broker.fleet_composition import production_provider_adapters
from app.main import app

#: Path parameters may carry Starlette converters; `{order_ref:path}` must be
#: filled with something multi-segment or the route under-matches.
_PATH_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)(?::([a-z]+))?\}")


def _concrete(template: str) -> str:
    return _PATH_PARAM.sub(
        lambda match: "seg/one" if match.group(2) == "path" else "probe-value",
        template,
    )


def _resolves(method: str, path: str) -> bool:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    return any(route.matches(scope)[0] is Match.FULL for route in app.routes)


def test_every_catalog_agent_path_resolves_to_a_mounted_route() -> None:
    unresolved = sorted(
        (operation.operation_id, operation.method, operation.agent_path_template)
        for adapter in production_provider_adapters().values()
        for operation in adapter.operations()
        if not _resolves(operation.method, _concrete(operation.agent_path_template))
    )

    assert unresolved == [], (
        "each entry declares an agent path this process does not serve at that "
        f"method: {unresolved}"
    )


def test_the_probe_detects_a_method_that_is_not_mounted() -> None:
    """The check would be vacuous if PARTIAL matches counted."""
    assert _resolves("GET", "/api/brokers/alpaca/accounts/x/bots/deploy")
    assert not _resolves("DELETE", "/api/brokers/alpaca/accounts/x/bots/deploy")
```

**Run / expected failure**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/contracts/test_fleet_agent_path_templates_resolve.py -q
```
Expected:
```
AssertionError: each entry declares an agent path this process does not serve
at that method: [('bots_deploy_apply', 'POST',
'/api/brokers/alpaca/accounts/{account_id}/bots/deploy')]
assert [('bots_deploy_apply', ...)] == []
```
(measured — that is the exact and only entry today, out of 77 operations)

**Minimal implementation, per file**

- **`fleet_adapter.py` (d):** delete the `_op("bots_deploy_apply", "POST", "/accounts/{account_id}/bots/deploy", capability=Capability.DEPLOY, idempotency=_DURABLE, account=True)` entry at `:344-351`. `Capability.DEPLOY` survives on `bots_deploy_read` (`:337-343`), so the capability vocabulary is unchanged. Bump `_ADAPTER_VERSION` (`:25`) from `"alpaca-fleet.4"` to `"alpaca-fleet.5"` — the catalog is the contract and `require_protocol_compatible` pairs builds by protocol version, not adapter version, so this is a label, but a stale one misreports the served surface.
- **`fleet_adapter.py` (b):** in `provider_summary` (`:738-757`), after the `confirmed_binding_generation` entry, add
  ```python
  "confirmed_by_current_session": observation.get("confirmed_by_current_session"),
  "multiple_effective_assignments": observation.get("multiple_effective_assignments"),
  ```
  Those two are the only facts separating `starting`-because-never-confirmed from `starting`-because-a-superseded-session-confirmed, and `degraded`-because-the-registry-is-corrupt from `degraded`-because-the-agent-said-so (`service.py:1701-1712`). Add a `ClerkDirectorySummary` assertion to `tests/broker/fleet/test_directory_and_aggregation.py` beside `:220`.
- **`provider.py` (c):** delete `production_adapter` (`:348-350`) and its `__all__` entry (`:363`). `PRODUCTION_PROVIDER_ADAPTERS` (`:297`) stays — `service.py:160` uses it as the fail-closed default. Extend its comment to name `app/broker/fleet_composition.py:27-29` as the live registry so the next reader does not repeat the investigation.
- **`test_import_isolation.py` (c):** rename `test_the_production_adapter_registry_stays_empty_until_phase_2` (`:96`) to `test_the_broker_neutral_package_holds_no_provider_registry` and replace the `production_adapter("alpaca")` body (`:97-105`) with an assertion that `dict(PRODUCTION_PROVIDER_ADAPTERS) == {}` and that `production_adapter` is absent from the module. Same rename reasoning applies to `tests/broker/fleet/test_a2_alpaca_lane.py:91`, whose name is accurate — leave it.
- **`delivery.py` (e):** replace `assert isinstance(result, DeliveryResult)` (`:324`) and `assert isinstance(result, StreamDeliveryResult)` (`:333`) with
  ```python
  if not isinstance(result, DeliveryResult):
      raise DeliveryIdentityMismatch(
          "the in-process handler returned "
          f"{type(result).__name__}, not a DeliveryResult"
      )
  ```
  and the `StreamDeliveryResult` equivalent. `DeliveryIdentityMismatch` is already the module's typed failure and already in `__all__` (`:345`). The precedent is `fleet_boot.py:148-154`, whose comment says "`assert` would vanish under `python -O`".
- **`routing.py` + `service.py` (f):** add to `FleetControlService`, beside `adapters()` (`service.py:182-184`):
  ```python
  def approved_endpoint(self, clerk_id: str) -> ApprovedEndpointRecord | None:
      """One lane's approved internal destination (routing reads it, never sets it)."""
      return self._store.read_approved_endpoint(clerk_id)
  ```
  and change `routing.py:644` to `endpoint = service.approved_endpoint(session.clerk_id)`.
- **`fleet_boot.py` (i):** in `_fence_writable_roots`'s docstring (`:386-394`), replace "in the clerk-agent role" with "for every enrolled lane — `combined`-with-a-marker included, since two enrolled combined processes on one host share the same shared artifacts tree exactly as two agents would". Docstring only; the body is correct as written.
- **`broker_clerks.py` (g):** in `list_broker_clerks_for_broker` (`:109-119`), before reading the directory:
  ```python
      service = _fleet_service(request)
      if broker not in service.adapters():
          from app.broker.fleet.errors import BrokerNotSupported

          return _refuse(
              BrokerNotSupported(
                  f"No production adapter serves {broker!r}.",
                  next_step="Use a provider this deployment supports; adding one "
                  "is a reviewed code change, not a request parameter.",
              )
          )
      directory = service.directory()
  ```
  This is the same refusal `_lookup_operation` (`:152-158`) already raises for the same condition, so the directory and the operation routes stop disagreeing about whether `GET /api/brokers/tradier/clerks` is a 404 or an empty 200. Add `test_an_unregistered_broker_is_a_typed_404_not_an_empty_lane_list` to `tests/broker/fleet/test_b_scoped_contracts.py`.

**Run / expected pass**
```
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/contracts/test_fleet_agent_path_templates_resolve.py \
  tests/broker/fleet tests/routers/test_broker_clerks.py -q
python scripts/export_openapi_contract.py --check
ruff check PythonDataService/app/ PythonDataService/tests/
```
(the `tests/broker/fleet` run needs `dangerouslyDisableSandbox: true`)

**Commit**
```
refactor(fleet): close seven verified backend gaps (#2076)

- catalog: delete `bots_deploy_apply`, which declared POST against a GET-only
  mount — a command the coordinator would persist an attempt for and collect a
  405 on. New contract test resolves all 77 agent_path_templates against the
  real router and fails on a method mismatch, not just a missing path.
- directory: carry confirmed_by_current_session and
  multiple_effective_assignments to the wire; they are the only facts
  separating the two meanings of `starting` and the two of `degraded`.
- provider: delete `production_adapter()`, which resolved against a
  deliberately-empty constant, had no callers, and always refused "alpaca".
  Its pinning test's name outlived Phase 2.
- delivery: two `assert isinstance` on the local dispatch path vanish under
  `python -O`; raise the module's own typed DeliveryIdentityMismatch instead.
- routing: `service._store` reach-in becomes `service.approved_endpoint()`.
- broker_clerks: an unregistered broker is BrokerNotSupported, matching what
  the operation routes already do, not a silent empty 200.
- fleet_boot: `_fence_writable_roots` runs for every enrolled lane; its
  docstring said clerk-agent only.

Deferred with reasons in the issue: the unwritten `draining` state (needs a
drain ceremony, not a cleanup) and _PRIVATE_HOST_CACHE's missing TTL (keys are
host-ceremony data, not request data).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
```

---

## 4. Grouping advice

**PR boundaries — four PRs, in this order.**

| PR | Tasks | Why it stands alone |
|---|---|---|
| **A** | Task 1 | A latent production defect on a running lane. Ships alone so it can be reviewed and merged without waiting on contract regeneration. Touches one file plus its test. |
| **B** | Task 2 | Route deletions. Regenerates `contracts/openapi/python-data-service.openapi.json` and `Frontend/src/app/api/broker.types.ts`. |
| **C** | Task 4 then Task 3 | Auth posture + the CONTEXT.md bullet that describes it. One PR, two commits, in that order — the doc must not claim a fence that is not merged. |
| **D** | Task 5 | Cleanups. Also regenerates both contract files. |

**Sequential vs parallel.** A and C are independent of everything and of each other — run in parallel. **B and D must be sequential** (either order): both regenerate `python-data-service.openapi.json` and `broker.types.ts`, which are whole-file generated artifacts and will conflict irreconcilably. Land B, rebase D, regenerate, push. Both also want #2081 merged first, so `agent_identity.py` and `restart.sh` are not moving underneath them — C touches `agent_identity.py` directly and **will conflict** with #2081's edit to `:217-228` if #2081 lands second.

**`./restart.sh` safety with a running bot on the Live lane: every one of these is unsafe to deploy that way, and so is every other change.** `restart.sh:21` is an unconditional `podman compose down` with no service filter — the Live clerk agent goes down with the rest, taking a running bot's process with it, on `./restart.sh` for any reason. #2081 fixes the *reap* and *health-verdict* halves of that script but not the teardown, which is the script's stated purpose. Additional per-PR notes:

- **PR A** changes middleware installed on every clerk request. A restart is required for it to take effect; no in-place reload path exists.
- **PR B** deletes routes. Deploying it while a bot runs is safe *after* the restart — nothing calls the deleted routes — but there is a brief window where a coordinator built from the new image and an agent still on the old one disagree about the mounted surface. Since the fleet runs one image (`FLEET_IMAGE`), this cannot actually split; noted so a future two-image posture does not inherit the assumption.
- **PR C** is the one to stage deliberately. If `FLEET_COORDINATOR_SERVICE_TOKEN` is misconfigured for a lane, every forwarded command to that lane becomes a 400 `broker_and_clerk_required` instead of reaching the handler. Verify `deploy/fleet/env/*.env` (or the live `compose.override.yaml`) provisions the token per clerk before restarting, and check `GET /api/broker-clerks` shows both lanes `ready` before issuing a command.
- **PR D** is the safest — six of the seven changes are internal, and the seventh (`bots_deploy_apply` removal) removes an operation that returns 405 today.

Recommendation: land A, B, D in one restart window while the Live lane is flat, then C in a second window after verifying the coordinator token provisioning.

---

## 5. Risks the issue text missed

1. **The compatibility mechanism can already break the fleet, today, on reads.** 15 catalog operations' agent paths are matched by `compatibility_route_family`; the 410 at `lane_runtime.py:550` is not exempted for coordinator forwards because the `x-fleet-clerk-id` check at `:521` only suppresses measurement. The first Delivery-E ceremony on a clerk agent takes out the desk-configuration surface and clerk status on a running lane. This is strictly more urgent than anything the issue asked for, and it is why Task 1 leads.

2. **"Delete the unscoped mutation routes outright" would sever fleet routing if taken literally.** 22 of the 27 unscoped mutations are the resolution target of a catalog `agent_path_template`. The issue reads them as browser leftovers; they are the transport. Any implementer who greps for "unscoped mutation" and deletes will break `bot_create`, `bot_panel_action`, both cohort operations, the manual-order ticket surface and the entire configuration write path.

3. **Two of the five orphans are operator-recovery routes with no fleet successor.** `POST .../live-envelope/loss-hold/clear` implements ADR 0059 D4 — the guarded clear on a real-money loss hold. `POST .../runs/{run_id}/replay-receipt` is the only way to generate a receipt the GET then serves (`run_replay.py:55-57` literally tells the caller to POST it). Deleting either removes operator capability with nothing to route to. They are retained and documented as stranded, joining `GET …/order-groups` from #2078 §8.

4. **`bots_deploy_apply` is not merely unreachable — it is a routable command that 405s.** The coordinator mounts `POST /api/brokers/{broker}/clerks/{clerk_id}/accounts/{account_id}/bots/deploy` (it is in the committed OpenAPI at `contracts/openapi/python-data-service.openapi.json:50991` and in `Frontend/src/app/api/broker.types.ts:2045`). `LaneRouter.deliver_command` persists a routing attempt *before* dispatch. Anyone who calls it gets a durable attempt row followed by a 405 the refusal vocabulary has no word for — and per #2078 §7, no refusal reason code renders in the frontend at all, so the operator sees a blank error against a real audit row.

5. **`compatibility_route_family` is prefix-greedy and will silently capture future routes.** Its final line is `return "broker_bots"` for *any* unmatched `GET /api/brokers/{b}/bots/...` path. Adding an agent-served bots read under a new sub-path automatically enrols it into a retirable family with no declaration and no test. The per-family retirement the issue asks for would make this worse, not better, because the default family becomes retirable independently.

6. **Deleting routes moves two generated artifacts that CI gates independently.** `.github/workflows/ci.yml:296` runs `export_openapi_contract.py --check`; `Frontend/package.json:18` runs `codegen:check` with `git diff --exit-code`. The exporter forces `FLEET_ROLE=combined` **and** sets `FLEET_CONTROL_DIR` (`scripts/export_openapi_contract.py:52-60`), so the committed contract covers the clerk *and* coordinator surfaces — but **not** `fleet_compatibility_reads.router`, which mounts only under `_FLEET_ROLE == "fleet_coordinator"` (`app/main.py:1236`). The coordinator's two unscoped reads are therefore absent from the committed OpenAPI and from `broker.types.ts`: the frontend's live `panel-profile` call at `broker-v2-panel.service.ts:94` is typed against the *clerk* mount, not the coordinator alias it actually hits in fleet posture. Worth a follow-up issue; out of scope here.

7. **`BrokerAndClerkRequired` is 400, and the frontend renders no fleet reason code.** Task 4's refusal is correct on the wire and invisible in the browser (#2078 §7: 0 of 25 reason codes appear in `Frontend/src/`, and all three parsers read `error.error.detail.*` while the fleet returns a flat body). The fence works; an operator who trips it sees a generic error. #2081 ships `Frontend/src/app/shared/errors/refusal-body.ts` which fixes the parser half — sequence Task 4 after it so the refusal is at least readable.

---

### Critical files for implementation
- `/Users/inkant/learn-ai/PythonDataService/app/broker/fleet/lane_runtime.py`
- `/Users/inkant/learn-ai/PythonDataService/app/broker/fleet/agent_identity.py`
- `/Users/inkant/learn-ai/PythonDataService/app/routers/broker_bots.py`
- `/Users/inkant/learn-ai/PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py`
- `/Users/inkant/learn-ai/CONTEXT.md`

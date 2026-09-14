# Lane C: fleet boot order and structural gates (#2064, #2063, #2073, #2071) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One sequential PR of seven commits: open the volume identity gate before any writer, wire the provider served-context gate at the routing seam, make the writable-root and nested-root fences falsifiable with DDL behind them, and report the registry recovery hold once.

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose, GitHub Actions.

**Spec:** the GitHub issues named in the title, plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md` — read it first; its Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular: never switch branches in `/Users/inkant/learn-ai` (the clerks bind-mount it); every `./restart.sh` drops both clerks; import-time changes need a no-bot window; run the thermo review once before the first push; fleet tests run unsandboxed.

## Corrections applied by the integrating session

- Task 5 (schema v3 UNIQUE index) must be preceded by the duplicate-root query against a COPY of the production registry named in Risk 2; a failing migration closes routing for every lane.
- Tasks 1, 4 and 5 are import-time boot changes: deploy only in a no-bot window with a fresh registry backup.

---

*The report below was produced by an Opus planning agent on 2026-09-14 against origin/master `e2fdb23f` plus the branches of PR #2081 and PR #2078. Every file:line claim was verified by that agent at that time; re-verify after those PRs merge.*

---

I have verified everything. Here is the plan.

---

# Fleet boot order and structural gates — one sequential lane (#2064, #2063, #2073, #2071)

Planned against `origin/master` (`e2fdb23f`) **plus** `origin/fix/fleet-production-safety` (#2081) and `origin/docs/broker-clerk-fleet-authority` (#2078). All paths below are absolute-from-repo-root under `/Users/inkant/learn-ai/`.

**Line-number drift from #2081.** #2081 inserts 28 lines into `open_fleet_lane`'s `combined` branch (around `fleet_boot.py:107-120`) and changes only `main.py:967-969` (middleware install). So after #2081 merges: `fleet_boot.py` `FleetRegistryStore.open` 160→**188**, `_fence_writable_roots` call 180→**208**, gate 185→**213**, `_fence_writable_roots` def 385→**413**. `main.py:1–960` is untouched by #2081; every `main.py` citation below survives the merge unchanged. No semantic conflict with any task here.

---

## 1. Verified facts

**#2064 — boot order**

- **CONFIRMED** `PythonDataService/app/main.py:377-382` — `handover = (contextlib.nullcontext(None) if worker_refusal is not None or not _ROLE_RUNS_CLERK else selection_handover())`, entered by `with handover as handover_refusal:` at `:382`.
- **CONFIRMED** `PythonDataService/app/main.py:383-386` — the comment "The fleet lane opens BEFORE any profile or custody database: the volume identity gate, then session registration (ADR 0062 addendum)." It sits *inside* the `with handover` block, i.e. three lines *after* the profiles DB has already opened. Wrong as written.
- **CONFIRMED** `PythonDataService/app/main.py:391-393` — `fleet_lane = await open_fleet_lane(settings=fleet_settings, volume_root=resolve_clerk_dir())`.
- **CONFIRMED** `PythonDataService/app/main.py:409` — `alpaca_broker = AlpacaBroker(settings=alpaca_settings)`. The broker-client half of Decision 2 holds exactly: `391 < 409`.
- **CONFIRMED** the DB chain: `worker_lifecycle.py:95` `service_factory().selection_handover()` → `broker_configuration/runtime.py:70` `get_broker_configuration_service()` → `:78` `build_service()` → `:63` `ProfilesStore.open(clerk_dir=root)` → `broker_configuration/store.py:140` `db_path.parent.mkdir(parents=True, exist_ok=True)`, `:144` `sqlite3.connect(...)`, `:147-148` `schema.configure_connection(conn)` + `cls._establish(conn)` (create/migrate). Exactly `store.py:140-148`.
- **CONFIRMED** `PythonDataService/app/main.py:222` `with installation_worker() as refusal:` → `worker_lifecycle.py:71` `try_advisory_file_lock(root / "broker_configuration" / "worker")` → `app/utils/advisory_lock.py:15` `target.parent.mkdir(parents=True, exist_ok=True)` and `:17` `open(lock_path, "a+b")`. Artifact: `<clerk_dir>/broker_configuration/.worker.lock`, written on the unverified volume, *before* `_service_lifespan` is even entered.
- **CONFIRMED, with a correction** `fleet_boot.py:159-162` opens `FleetRegistryStore.open(control_dir=settings.CONTROL_DIR)` 25 lines before the gate at `:185`. **The issue text is WRONG about which volume**: `CONTROL_DIR` is the *coordinator's control volume*, not the clerk volume ADR 0062 Decision 2 governs. It is also **unavoidable**: `LocalPresence.expectation()` (`presence.py:135`) reads the registry to produce the expectation the gate compares against. Circular by construction — this one cannot be reordered, only documented.
- **CONFIRMED** the gate `fleet_boot.py:185` → `_verify_root_against_expectation` (`fleet_boot.py:416-427`) → `app/broker/fleet/volume.py:201` `verify_volume_identity`, which raises **`ClerkVolumeMountUnproven`** (`volume.py:75/85/90/95`, missing/symlinked/non-dir root), **`ClerkVolumeIdentityMissing`** (`volume.py:220`, no marker), or **`ClerkVolumeIdentityMismatch`** (`volume.py:238`, marker disagrees with the registry). All are `FleetControlError`, `status_code = 409`.
- **CONFIRMED, and the issue missed it** `fleet_boot.py:180` calls `_fence_writable_roots` *before* the gate at `:185`. It is read-only (`resolve()` + comparison), so not a Decision-2 violation, but it means a fence refusal masks an identity refusal — the #2064 test must keep the fenced roots inside the volume or it will assert the wrong exception.

**#2063 — `validate_served_context`**

- **CONFIRMED** declared `app/broker/fleet/provider.py:283`; `ServedContext` at `provider.py:214-229`.
- **CONFIRMED** implemented `app/broker/alpaca/clerk/fleet_adapter.py:759-782`.
- **CONFIRMED** `LaneRouter._resolve` at `app/broker/fleet/routing.py:467-491` builds no `ServedContext`; it calls `self._service.resolve_route(...)` at `:485-491` and returns `(clerk, session, assignment)`. Callers: `routing.py:174` (`deliver_read`), `:237` (`stream_read`), `:318` (`deliver_command`).
- **CONFIRMED** repo-wide the symbol exists in exactly four files: `provider.py:283`, `fleet_adapter.py:759`, `tests/broker/fleet/conftest.py:151`, `tests/broker/fleet/test_a2_alpaca_lane.py:71`. `ServedContext` is constructed only at `test_a2_alpaca_lane.py:79`.
- **CONFIRMED** `served_context_refusals` is declared at `conftest.py:129` and read only at `conftest.py:152`. Zero tests set it.
- **CONFIRMED** the docstring/body mismatch: `fleet_adapter.py:764-766` promises "a bot action is not servable by a lane whose reported authority is shadow"; the body (`:768-782`) checks only (a) `BOT_ACTION` requires a non-`None` `account_id` and (b) `account_id` must match `_ALPACA_UUID`. No shadow check exists.
- **New fact the issue missed:** both the adapter (`fleet_adapter.py:772,780`) and the fake (`conftest.py:153`) raise **`LookupError`**, not a `FleetControlError`. `app/routers/broker_clerks.py:133,221,283` catch only `FleetControlError`, so wiring this in raw would turn a provider refusal into an HTTP 500. The precedent at `tests/broker/fleet/test_provider_conformance.py:242` ("surfaces as its own error") is a *service*-level path with no HTTP surface; the routed path is different.

**#2073 — structural fences**

- **CONFIRMED (a)** `_fence_writable_roots` defined `fleet_boot.py:385-413`, called once at `:180`. The only test-side contact is `tests/broker/fleet/test_a2_alpaca_lane.py:283-294` `_fence_satisfying_roots`, which monkeypatches `IbkrSettings(live_runs_root=str(volume_root / "live_runs" / "runs"), live_bars_root=str(volume_root / "live_bars"))` — i.e. always *inside* the volume. Deleting the fence body fails nothing.
- **CONFIRMED (b)** `app/broker/fleet/service.py:260-274` — `new_root = canonical_root.resolve()` then `for active in self._store.list_clerks(): ... if new_root == existing_root or new_root.is_relative_to(existing_root) or existing_root.is_relative_to(new_root): raise ClerkVolumeAlreadyRegistered`. The first `with self._store.transaction() as conn:` is at `:322`. The loop is outside it. `store.transaction()` (`store.py:226-243`) does use `BEGIN IMMEDIATE`, so the fix is to move the check inside, not to invent a lock.
- **CONFIRMED** no DDL backstop: `app/broker/fleet/schema.py:88-94` declares `ux_clerks_worker_key`, `ux_clerks_volume_id`, `ux_clerks_volume_attestation` — **none on `volume_root`**. The 13 triggers are at `schema.py:151,157,234,250,258,269,276,282,288,297,304,313,330,338,347,371,377` (fresh DDL), all `BEFORE UPDATE`/`BEFORE DELETE` with `RAISE(ABORT, '<lowercase prose>')`.
- **CONFIRMED (c)** `service.py:511` `volume_root: Path | None = None` in `register_agent_session`, consumed at `:578-579` `if volume_root is not None: self._verify_volume(clerk, volume_root)`. `service.py:692` same in `reserve_assignment`, consumed at `:731-732`.
- **CONFIRMED** `LocalPresence.register` (`presence.py:152-159`) and `LocalPresence.reserve` (`presence.py:169-171`) pass no `volume_root`. `RemotePresence.register` (`presence.py:351-362`) passes none either — **and cannot**: `presence.py:323-326` states the design ("the coordinator never inspects an agent-local path"), which ADR 0062 addendum item 6 pins ("equal path strings in two containers are two mounts"). The issue's framing is therefore **partly WRONG**: `RemotePresence` passing none is correct by design; the real gap is `LocalPresence`, which shares the filesystem and could pass one.
- **CONFIRMED** the only test touching the parameter is `tests/broker/fleet/test_provisioning_and_volume.py:168-189`, which passes it explicitly. No test asserts omission.
- **CONFIRMED** `scripts/manage_broker_fleet.py:544` verifies, then `:559` registers and `:565` reserves without `volume_root` — same "adjacent, not structural" pattern.

**#2071 — the duplicated fence**

- **CONFIRMED** `app/broker/fleet/recovery.py:71-89` — `RegistryRecoveryState` carries one derived bit, `routing_closed` (`:84-89`).
- **CONFIRMED** `scripts/manage_broker_fleet.py:361` `"assignment_mutation_closed": state.routing_closed` and `:430` identical. Literal copies.
- **CONFIRMED, and the issue missed it** `manage_broker_fleet.py:408` (`_reconcile_registry`) emits `routing_closed` **without** `assignment_mutation_closed`. Three sites report the hold; only two report the "second fence". The CLI is internally inconsistent as well as duplicative.
- **CONFIRMED** one bit really does close both: `recovery.py:394-417` `require_routing_open` is called from `service.py:169-174` `_require_registry_recovery_open`, which is called at `service.py:219, 428, 473, 705, 851, 996, 1075, 1193` — provisioning, reservation, confirmation, release, retirement and `resolve_route` alike.
- **CONFIRMED** the ADR text is ambiguous rather than wrong: Consequences (`docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md:74`) says "identity conflict keeps routing disabled"; the addendum's closing paragraph (`:167-170`) says "routing and assignment creation stay closed" — two nouns, one mechanism, never reconciled.
- **CONFIRMED** the only pins on CLI output are `tests/broker/fleet/test_fleet_cli.py:456-457` and `:474-475`, plus the runbook prose at `docs/runbooks/fleet-e-registry-recovery-exercise.md:64`. **No contract snapshot** in `contracts/` mentions either name.

---

## 2. Decisions for the owner

**#2063 — Wire it, do not delete.** ADR 0062 Decision 5 names "provider safety gates" as part of what the agent validates before any mutation, and the Alpaca body already encodes a real invariant (a non-UUID account id cannot be honoured on any custody path) that no generic check covers — deleting it removes a gate, whereas wiring it costs ~12 lines.
*If the owner picks delete instead:* remove `provider.py:283-290` and `fleet_adapter.py:759-782`, delete `ServedContext` (`provider.py:214-229`) and its `__all__` entry (`:362`), delete `conftest.py:129,151-155` and `test_a2_alpaca_lane.py:70-87`, and amend ADR 0062 Decision 5 to say the agent-side provider gates run inside the clerk's own handlers, not at the routing seam. Tasks 3 and 4 below disappear; everything else in the lane is unaffected.

**#2073(b) — Both, not either.** A `UNIQUE` partial index on `(deployment_namespace, volume_root)` plus a `BEFORE INSERT` `RAISE(ABORT)` trigger doing exact prefix comparison, *and* moving the Python loop inside the existing `BEGIN IMMEDIATE` transaction — because the index cannot express containment, the trigger cannot produce the typed `ClerkVolumeAlreadyRegistered` refusal on its own, and the transaction alone leaves no evidence in the DDL where every other isolation invariant lives.

**#2073(c) — Keep optional at the service; make it structural at `LocalPresence`.** ADR 0062 addendum item 6 forbids the coordinator inspecting an agent-local path, so making `volume_root` required in `register_agent_session` would break the split-process posture the fleet exists for; the honest fence is to require it in `LocalPresence.__init__`, where the caller provably shares the filesystem.

**#2071 — Rename and correct the ADR; do not implement a second fence.** The hold is one ceremony-scoped bit by construction — `require_routing_open` is the sole enforcement and there is no ceremony that could move a second bit independently — so a second fence would be unreachable state pretending to be a control.

---

## 3. Task breakdown

Execution order is forced: Task 1 changes the `_service_lifespan` signature that Task 2's test boots; Task 5 bumps `SCHEMA_VERSION`, which every later registry test opens.

---

### Task 1 — Move the clerk-volume gate ahead of every writer (#2064)

**Files**
- modify `PythonDataService/app/main.py`
- create `PythonDataService/tests/broker/fleet/test_boot_opens_nothing_before_the_gate.py`

**Failing regression test** (new file, full content):

```python
"""ADR 0062 Decision 2: nothing opens on the clerk volume before its gate.

A boot against a volume whose marker disagrees with the registry must leave
the volume byte-identical: no profiles database, no installation lock file,
not even the ``broker_configuration/`` directory the two of them share.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet.volume import marker_path
from app.broker.fleet_composition import production_provider_adapters

_PROBE = (
    "import sys; sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]; "
    "import asyncio, json; "
    "from app.main import app, lifespan\n"
    "async def _boot():\n"
    "    try:\n"
    "        async with lifespan(app):\n"
    "            return {'refused': None}\n"
    "    except BaseException as exc:\n"
    "        return {'refused': type(exc).__name__}\n"
    "print(json.dumps(asyncio.run(_boot())))"
)


def test_a_mismatched_volume_marker_leaves_no_writer_artifact(tmp_path: Path) -> None:
    """The gate refuses before the profiles DB and the installation lock exist."""
    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "paper"
    volume_root.mkdir(parents=True)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    try:
        provisioned = service.provision_clerk(
            broker="alpaca",
            display_label="Paper",
            volume_root=volume_root,
            attestation_id="learn-ai-alpaca-paper",
            deployment_namespace="compose:test",
        )
    finally:
        service.close()

    # Poison the marker exactly as a restored-from-another-lane volume would:
    # the registry expects the provisioned volume_id, the volume claims another.
    marker = json.loads(marker_path(volume_root).read_text(encoding="utf-8"))
    marker["volume_id"] = "vol_ffffffffffffffffffffffff"
    marker_path(volume_root).write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )

    service_root = Path(__file__).resolve().parents[3]
    environment = {
        **os.environ,
        "PYTHONPATH": str(service_root),
        "DATA_PLANE_CONTROL_SECRET": "",
        "FLEET_ROLE": "clerk_agent",
        "FLEET_CONTROL_DIR": str(control_dir),
        "FLEET_CLERK_ID": provisioned.clerk.clerk_id,
        "FLEET_WORKER_KEY": provisioned.clerk.worker_key,
        "FLEET_DEPLOYMENT_NAMESPACE": "compose:test",
        "FLEET_MAX_INFLIGHT_REQUESTS": "1",
        "FLEET_MAX_INFLIGHT_STREAMS": "1",
        "FLEET_REQUEST_QUEUE_LIMIT": "0",
        "FLEET_REQUEST_QUEUE_TIMEOUT_MS": "0",
        "ALPACA_CLERK_DIR": str(volume_root),
        # Inside the volume, so _fence_writable_roots cannot mask the gate.
        "IBKR_LIVE_RUNS_ROOT": str(volume_root / "live_runs" / "runs"),
        "IBKR_LIVE_BARS_ROOT": str(volume_root / "live_bars"),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=environment,
        cwd=service_root,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome["refused"] == "ClerkVolumeIdentityMismatch"

    # The proof: the gate ran before anything opened on the volume.
    assert not (volume_root / "broker_configuration").exists()
```

**Run command and expected failure**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_boot_opens_nothing_before_the_gate.py -q
```

Fails on the last assertion: `AssertionError: assert not True` — `<tmp>/volumes/paper/broker_configuration` exists, holding `.worker.lock` (from `main.py:222`) and `profiles.db` (from `main.py:377-382`). The `refused` assertion already passes today, because the gate does fire — just too late.

**Minimal implementation** — three edits in `PythonDataService/app/main.py`:

1. Add a module-level helper immediately above `lifespan` (before current line 206):

```python
async def _open_verified_fleet_lane():
    """Run the ADR 0062 Decision 2 gate before any writer opens on the volume."""
    if not _ROLE_RUNS_CLERK:
        return None
    from app.broker.alpaca.clerk.fleet_boot import open_fleet_lane
    from app.broker_configuration.runtime import resolve_clerk_dir

    return await open_fleet_lane(
        settings=fleet_settings, volume_root=resolve_clerk_dir()
    )
```

2. Rewrite the body of `lifespan` after the `fleet_coordinator` early return (current `:220-247`) to open the lane first and own its close:

```python
    from app.broker.alpaca.clerk.fleet_boot import close_fleet_lane
    from app.broker_configuration.worker_lifecycle import installation_worker

    # ADR 0062 Decision 2: the volume identity gate runs before ANY writer
    # opens on the clerk volume — before the installation lock file, before
    # the profiles database, before the broker client.
    fleet_lane = await _open_verified_fleet_lane()
    try:
        with installation_worker() as refusal:
            started = False
            try:
                async with _service_lifespan(
                    app, worker_refusal=refusal, fleet_lane=fleet_lane
                ):
                    started = True
                    yield
            except BaseException:
                if not started:
                    from app.broker_configuration.worker_lifecycle import close_failed_startup

                    await close_failed_startup()
                raise
    finally:
        await close_fleet_lane(fleet_lane)
```

3. In `_service_lifespan`: add `fleet_lane=None` to the signature (current `:249`); delete `fleet_lane = None` (`:373`) and the whole `if _ROLE_RUNS_CLERK:` open block (`:387-393`); replace the comment at `:383-386` with one that is true of what remains:

```python
    with handover as handover_refusal:
        # The fleet lane is already open and its volume proven (see
        # ``lifespan``); the profiles database opens here, after the gate.
```

and delete the lane close at `:849-852` (keep the heartbeat cancel at `:845-848`), since `lifespan` now owns it.

**Observable behaviour on a healthy volume: unchanged.** The lane open was never fenced by the installation lock — `main.py:387` `if _ROLE_RUNS_CLERK:` is independent of `worker_refusal`, so a process that *loses* the installation lock opens and registers its lane today exactly as it will after the reorder. On a healthy volume the gate passes and every subsequent step runs in the same order against the same state; only the relative position of the lock file and the profiles DB changes, and both are still created.

**Run command and expected pass**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_boot_opens_nothing_before_the_gate.py \
  tests/broker/fleet/test_a2_alpaca_lane.py -q
```

Expect `19 passed`.

**Commit**

```
fix(fleet): open the clerk volume's identity gate before any writer

ADR 0062 Decision 2 requires the volume gate to pass before any database
writer or broker client opens. The broker-client half held; the database
half did not — selection_handover() created and migrated the profiles
database, and installation_worker() wrote a lock file, both on a volume
whose identity had not been proven. The lane now opens in lifespan(),
ahead of both, and owns its own close.
```

---

### Task 2 — A negative test for `_fence_writable_roots` (#2073a)

**Files**
- modify `PythonDataService/tests/broker/fleet/test_a2_alpaca_lane.py`

**Failing regression test** — append after `test_an_enrolled_agent_without_any_coordinator_destination_refuses` (current `:918`):

```python
async def test_a_lane_root_outside_the_clerk_volume_refuses_before_authority(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writable-root fence is load-bearing: an escaping root refuses."""
    from app.broker.ibkr import config as ibkr_config

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        root = Path(provisioned.clerk.volume_root)
        shared = tmp_path / "shared-artifacts"
        # The deployment default: both roots on the shared artifacts tree,
        # which is exactly the two-lanes-one-root posture the fleet prevents.
        monkeypatch.setattr(
            ibkr_config,
            "get_settings",
            lambda: ibkr_config.IbkrSettings(
                live_runs_root=str(shared / "live_runs" / "runs"),
                live_bars_root=str(shared / "live_bars"),
            ),
        )
        (shared / "live_runs" / "runs").mkdir(parents=True)
        (shared / "live_bars").mkdir(parents=True)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )
        from app.broker.alpaca.clerk.fleet_boot import FleetBootRefused, open_fleet_lane

        with pytest.raises(FleetBootRefused, match="escapes the clerk volume"):
            await open_fleet_lane(settings=settings, volume_root=root)
        # The refusal precedes session registration: nothing was registered.
        assert service._store.read_session(provisioned.clerk.clerk_id) is None
    finally:
        service.close()
```

**Run and expected failure** — run against a checkout with `fleet_boot.py:399-413`'s loop body deleted (the mutation this test exists to kill) and it must fail. Against current code it passes immediately, which is the wrong shape for a red-first test, so verify by mutation:

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_a2_alpaca_lane.py -q -k outside_the_clerk_volume
```

With the fence body stubbed to `return`: `Failed: DID NOT RAISE <class 'app.broker.alpaca.clerk.fleet_boot.FleetBootRefused'>`. Restore the body; expect `1 passed`.

**Implementation** — none. This task is pure coverage; the fence is already correct. State that in the PR body so a reviewer does not look for a missing production diff.

**Run and expected pass**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_a2_alpaca_lane.py -q
```

Expect `19 passed`.

**Commit**

```
test(fleet): pin the writable-root fence against deletion

_fence_writable_roots had no negative test — every existing test pointed
the lane roots inside the volume, so removing the fence body failed
nothing. Verified by mutation: stubbing the loop makes this test fail.
```

---

### Task 3 — Wire `validate_served_context` into `LaneRouter._resolve` (#2063)

**Files**
- modify `PythonDataService/app/broker/fleet/routing.py`
- modify `PythonDataService/app/broker/fleet/errors.py` (docstring only)
- modify `PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py` (docstring only)
- modify `PythonDataService/tests/broker/fleet/test_provider_conformance.py`

**Failing regression test** — append to `test_provider_conformance.py`:

```python
async def test_a_provider_refusing_the_served_context_closes_the_route(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """The provider safety gate runs at the routing seam, before dispatch."""
    from app.broker.fleet.errors import BrokerClerkCapabilityUnavailable
    from app.broker.fleet.routing import LaneRouter
    from tests.broker.fleet.conftest import FakeProviderAdapter, bind_lane, provision_lane

    strict = FakeProviderAdapter(
        provider_id="fake_alpha",
        served_context_refusals=["account_read"],
    )
    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": strict, "fake_beta": FakeProviderAdapter("fake_beta")},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="served", tmp_path=control_dir.parent
    )
    bind_lane(service, lane, account="acct-served")

    def _never(broker: str, session):
        raise AssertionError("dispatch must not be reached past a provider refusal")

    router = LaneRouter(service=service, delivery_for=_never)
    operation = next(
        op for op in strict.operations() if op.operation_id == "account_read"
    )
    with pytest.raises(BrokerClerkCapabilityUnavailable, match="refuses to serve"):
        await router.deliver_read(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation=operation,
            path_params={},
            query={},
        )
```

**Run and expected failure**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_provider_conformance.py -q -k served_context
```

`AssertionError: dispatch must not be reached past a provider refusal` — `_resolve` returns cleanly today and `deliver_read` calls `self._delivery_for` at `routing.py:191`.

**Implementation** — in `routing.py`, replace the `return` at `:485-491` with:

```python
        resolved = self._service.resolve_route(
            broker=broker,
            clerk_id=clerk_id,
            expected_binding_generation=expected_binding_generation,
            expected_account_id=expected_account,
            readiness=operation.readiness,
        )
        _clerk, session, assignment = resolved
        if assignment is not None:
            # ADR 0062 Decision 5 / addendum 3: the provider's own safety gate
            # answers for execution operations only. A configuration-access
            # operation must stay routable for a lane whose binding is broken,
            # which is precisely the lane a provider gate would refuse.
            from app.broker.fleet.provider import ServedContext

            context = ServedContext(
                broker=broker,
                clerk_id=clerk_id,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
                account_id=assignment.canonical_external_account_id,
                capability=operation.capability,
                effective_binding_generation=assignment.confirmed_binding_generation,
            )
            try:
                self._service.adapters()[broker].validate_served_context(context)
            except FleetControlError:
                raise
            except Exception as exc:
                raise BrokerClerkCapabilityUnavailable(
                    f"Provider {broker!r} refuses to serve "
                    f"{operation.operation_id} on clerk {clerk_id}: {exc}",
                    next_step="The provider's own safety gates must pass before "
                    "this operation routes; repair the lane's configuration and "
                    "retry against its current resource.",
                ) from exc
        return resolved
```

Add `BrokerClerkCapabilityUnavailable` to the existing `from app.broker.fleet.errors import ...` at the top of `routing.py`.

Widen the `errors.py:143` docstring by one clause: `"""The provider adapter does not declare the requested capability, or cannot honor the served context for this lane (FR-006)."""` — reusing the family, not minting a shape.

Fix the docstring/body mismatch at `fleet_adapter.py:762-766`: delete the shadow-lane sentence and replace with what the body does — *"this hook adds the two fleet-visible invariants the generic layer cannot check: a bot action names the effective account it targets, and every served account id is a canonical Alpaca UUID, because the Alpaca authority keys every custody path by that exact value."* The shadow-lane rule is not implementable here (`ServedContext` carries no authority state) and belongs to the clerk's own handlers.

**Run and expected pass**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_provider_conformance.py \
  tests/broker/fleet/test_b_scoped_contracts.py \
  tests/broker/fleet/test_sessions_and_routing.py \
  tests/broker/fleet/test_routing_attempts.py -q
```

**Commit**

```
fix(fleet): dispatch the provider served-context gate at the routing seam

ADR 0062 Decision 5 declares a provider safety gate that had no production
caller: validate_served_context was declared, implemented for Alpaca and
dispatched nowhere. LaneRouter._resolve now builds the ServedContext for
execution operations and maps a provider refusal onto the existing
broker_clerk_capability_unavailable family, so it reaches the wire as a 409
instead of escaping as an unhandled LookupError. The Alpaca docstring is
corrected to describe the invariants its body actually enforces.
```

---

### Task 4 — Make the `LocalPresence` volume gate structural (#2073c)

**Files**
- modify `PythonDataService/app/broker/fleet/presence.py`
- modify `PythonDataService/app/broker/alpaca/clerk/fleet_boot.py`
- modify `PythonDataService/app/broker/fleet/service.py` (docstrings only)
- modify `PythonDataService/tests/broker/fleet/test_provisioning_and_volume.py`

**Failing regression test** — append to `test_provisioning_and_volume.py`:

```python
async def test_local_presence_reverifies_the_volume_at_registration_and_reservation(
    control_dir: Path, fleet_service
) -> None:
    """The co-located transport cannot register a lane it has not re-proven."""
    from app.broker.fleet.presence import LocalPresence

    lane: Lane = provision_lane(
        fleet_service, broker="fake_alpha", label="structural", tmp_path=control_dir.parent
    )
    presence = LocalPresence(fleet_service, volume_root=lane.volume_root)
    marker_path(lane.volume_root).unlink()
    with pytest.raises(ClerkVolumeIdentityMissing):
        await presence.register(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            agent_instance_id="agnt_bbbb0000bbbb0000bbbb0000",
            endpoint_ref=None,
            adapter_version="test.1",
            fleet_protocol_version=2,
        )
    with pytest.raises(ClerkVolumeIdentityMissing):
        await presence.reserve(
            broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-1"
        )
```

**Run and expected failure**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_provisioning_and_volume.py -q -k local_presence_reverifies
```

`TypeError: LocalPresence.__init__() got an unexpected keyword argument 'volume_root'`.

**Implementation**

In `presence.py`, `LocalPresence.__init__` (`:129-131`) becomes:

```python
    def __init__(self, service: FleetControlService, *, volume_root: Path) -> None:
        """Bind the control service and the mounted root this process serves.

        Required, not optional: the in-process transport is the one that
        provably shares a filesystem with the volume, so it re-proves the
        root on every registration and reservation rather than trusting an
        adjacent call to have done it. ``RemotePresence`` supplies none by
        design — the coordinator never inspects an agent-local path (ADR 0062
        addendum 6); its agent proves the root locally in ``verify_volume``.
        """
        self._service = service
        self._volume_root = volume_root
```

Pass `volume_root=self._volume_root` in `LocalPresence.register` (`:152-159`) and `LocalPresence.reserve` (`:169-171`).

In `fleet_boot.py:163`, change `presence = LocalPresence(owned_service)` to `presence = LocalPresence(owned_service, volume_root=volume_root)`.

In `service.py`, add one sentence to both docstrings (`:516-533` and `:694-703`): *"``volume_root`` is the co-located caller's re-proof of the mounted root. It is optional because the coordinator process may not have the volume mounted at all; the agent's own gate (``fleet_boot.open_fleet_lane``) is the authority, and ``LocalPresence`` — the one transport that does share the filesystem — always supplies it."*

**Run and expected pass**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_provisioning_and_volume.py \
  tests/broker/fleet/test_a2_alpaca_lane.py \
  tests/broker/fleet/test_admission_probes_2026_09_13.py -q
```

**Commit**

```
fix(fleet): make the co-located volume gate structural, not adjacent

LocalPresence shares a filesystem with the clerk volume and could always
re-prove it, but passed no volume_root, leaving the gate one refactor away
from silence. Its constructor now requires the root and both register and
reserve re-verify. RemotePresence still passes none: the coordinator never
inspects an agent-local path (ADR 0062 addendum 6), and its agent proves
the root locally before registering.
```

---

### Task 5 — Give the nested-root fence a transaction and DDL behind it (#2073b)

**Files**
- modify `PythonDataService/app/broker/fleet/schema.py`
- modify `PythonDataService/app/broker/fleet/service.py`
- modify `PythonDataService/tests/broker/fleet/test_provisioning_and_volume.py`
- modify `PythonDataService/tests/broker/fleet/test_schema_migration.py`

**Failing regression test** — append to `test_provisioning_and_volume.py`:

```python
def test_two_racing_provisionings_of_nested_roots_cannot_both_land(
    control_dir: Path, fleet_service
) -> None:
    """The nested-root refusal survives a race: the DDL is the backstop."""
    import sqlite3

    parent_root = control_dir.parent / "volumes" / "raced"
    nested_root = parent_root / "inner"
    nested_root.mkdir(parents=True)
    fleet_service.provision_clerk(
        broker="fake_alpha",
        display_label="outer",
        volume_root=parent_root,
        attestation_id="vol-outer",
    )
    # The loser of a race reaches insert_clerk with a stale pre-check result.
    # Bypassing the Python loop is exactly what that race produces.
    record = fleet_service._store.read_clerk(
        fleet_service._store.list_clerks()[0].clerk_id
    )
    with pytest.raises(sqlite3.IntegrityError, match="one physical volume"):
        with fleet_service._store.transaction() as conn:
            conn.execute(
                "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, "
                "volume_id, volume_root, deployment_namespace, "
                "volume_attestation_kind, volume_attestation_id, lifecycle_state, "
                "created_at_ms, retired_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisioned', 1, NULL)",
                (
                    "clrk_raced0000000000000000aa",
                    "fake_alpha",
                    "wkey_raced000000000000000000",
                    "inner",
                    "vol_raced0000000000000000000",
                    str(nested_root),
                    record.deployment_namespace,
                    record.volume_attestation_kind,
                    "vol-inner",
                ),
            )
```

**Run and expected failure**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_provisioning_and_volume.py -q -k racing_provisionings
```

`Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>` — the insert succeeds today.

**Implementation**

In `schema.py`, add to the fresh DDL template beside `ux_clerks_volume_attestation` (`:92-94`):

```sql
CREATE UNIQUE INDEX ux_clerks_volume_root
    ON clerks(deployment_namespace, volume_root) WHERE lifecycle_state <> 'retired';
```

and add a trigger in the invariants block after `trg_clerks_lifecycle_forward` (`:266`), matching the surrounding style — plain SQL in the `WHEN` clause, lowercase prose in the `RAISE`:

```sql
-- FR-020/021: writable subtrees of one mounted volume never host two clerks.
-- The service's pre-check produces the typed refusal; this is the backstop a
-- race cannot step over. ``substr`` rather than ``LIKE``: a volume root
-- containing ``_`` or ``%`` must not over-match a wildcard pattern.
CREATE TRIGGER trg_clerks_volume_root_not_nested
BEFORE INSERT ON clerks
FOR EACH ROW WHEN NEW.lifecycle_state <> 'retired' AND EXISTS (
    SELECT 1 FROM clerks existing
    WHERE existing.lifecycle_state <> 'retired'
      AND existing.deployment_namespace = NEW.deployment_namespace
      AND (substr(NEW.volume_root, 1, length(existing.volume_root) + 1)
               = existing.volume_root || '/'
           OR substr(existing.volume_root, 1, length(NEW.volume_root) + 1)
               = NEW.volume_root || '/')
)
BEGIN
    SELECT RAISE(ABORT, 'one clerk, one physical volume: a volume root never nests inside another active clerk''s root');
END;
```

Set `SCHEMA_VERSION = 3` (`schema.py:33`) and register `SCHEMA_MIGRATIONS[2]` with the same two statements (the index first, so the migration fails loudly if a live registry already violates exact equality).

In `service.py`, move the loop at `:260-274` inside the transaction: delete it from its current position and re-issue it as the first statements inside `with self._store.transaction() as conn:` at `:322`, reading `self._store.list_clerks_on(conn)` (add that method to `store.py` beside `read_clerk_on`). Extend the existing `except sqlite3.IntegrityError` handler at `:325` to keep producing `ClerkVolumeAlreadyRegistered` — the trigger's `RAISE(ABORT)` surfaces as `sqlite3.IntegrityError`, which that handler already catches, so the public refusal is unchanged.

Add a v2→v3 case to `test_schema_migration.py` modelled on `test_a_migrated_registry_carries_the_fresh_v2_fences` (`:186`): open a v2 registry, migrate, assert `ux_clerks_volume_root` and `trg_clerks_volume_root_not_nested` are present in `sqlite_master` and that a nested insert aborts.

**Run and expected pass**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/ -q
```

**Commit**

```
feat(fleet): put DDL behind the nested-volume-root fence

The one structural-isolation invariant with no schema behind it was a
Python loop outside any transaction. Schema v3 adds a partial UNIQUE index
on (deployment_namespace, volume_root) and a BEFORE INSERT trigger doing
exact prefix comparison, and the service's pre-check moves inside the
existing BEGIN IMMEDIATE so check and insert are one write. The public
refusal is unchanged: the trigger's abort surfaces through the existing
IntegrityError handler as clerk_volume_already_registered.
```

---

### Task 6 — One recovery hold, named once (#2071)

**Files**
- modify `PythonDataService/app/broker/fleet/recovery.py`
- modify `PythonDataService/app/broker/fleet/service.py`
- modify `PythonDataService/scripts/manage_broker_fleet.py`
- modify `PythonDataService/tests/broker/fleet/test_registry_recovery.py`
- modify `PythonDataService/tests/broker/fleet/test_fleet_cli.py`
- modify `docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md`
- modify `docs/runbooks/fleet-e-registry-recovery-exercise.md`

**Failing regression test** — replace `test_fleet_cli.py:456-457` and `:474-475`:

```python
    restored = json.loads(capsys.readouterr().out)
    assert restored["mutations_closed"] is True
    # One hold, reported once: the CLI never claims two independent fences
    # over a single RegistryRecoveryState bit.
    assert "assignment_mutation_closed" not in restored
    assert restored["rollback_topology"] == "d_compatible"
```

```python
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["mutations_closed"] is False
    assert "assignment_mutation_closed" not in closeout
    assert closeout["empty_inventory_attestation"]["operator"] == "fleet-owner"
```

**Run and expected failure**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_fleet_cli.py -q
```

`KeyError: 'mutations_closed'`.

**Implementation**

`recovery.py:84-89` — rename the property and say what it holds:

```python
    @property
    def mutations_closed(self) -> bool:
        """One hold: routing AND assignment mutation, closed together.

        There is no second bit. ``require_recovery_hold_clear`` is the sole
        enforcement and every mutating service path calls it, so a restored
        registry refuses routing, provisioning, reservation, confirmation,
        release and retirement off this one derived value.
        """
        if not self.required_clerk_ids:
            return self.empty_inventory_attestation is None
        return set(self.required_clerk_ids) != set(self.reconciled_clerk_ids)
```

Rename `require_routing_open` → `require_recovery_hold_clear` (`recovery.py:394`, `__all__` at `:622`-ish, and `service.py:169-174`'s import and call). Update the three internal reads at `recovery.py:404` and the two `RegistryRecoveryState(...)` sites that read the property.

`manage_broker_fleet.py` — `:360-361` becomes a single `"mutations_closed": state.mutations_closed,`; `:408` becomes `"mutations_closed": state.mutations_closed,`; `:429-430` becomes a single `"mutations_closed": state.mutations_closed,`. Three sites, one key, no copies.

`test_registry_recovery.py` — rename the six assertions at `:127, 137, 195, 385, 414, 428` and the test name at `:71` to `test_registry_restore_holds_routing_and_assignment_mutation_closed_until_all_lanes_reconcile`.

ADR `0062-broker-clerk-fleet-control-plane.md` — append one bullet to Consequences (after `:74`):

> - Registry recovery is **one** hold, not two: while any original lane is unreconciled, routing and assignment mutation are refused together off a single derived state bit. The addendum's "routing and assignment creation stay closed" names two effects of one fence, not two fences.

`docs/runbooks/fleet-e-registry-recovery-exercise.md:64` — `routing_closed: false` → `mutations_closed: false`.

**Run and expected pass**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/test_fleet_cli.py tests/broker/fleet/test_registry_recovery.py -q
```

**Commit**

```
fix(fleet): report the registry recovery hold once, not twice

manage_broker_fleet reported routing_closed and assignment_mutation_closed
as two fences over one RegistryRecoveryState bit — the second was a literal
copy at two of the three sites and absent at the third. There is one hold
and one enforcement point; the property, the guard and the CLI key now say
so, and ADR 0062's Consequences records it.
```

---

### Task 7 — Close the lane: lint, ADR/doc reconciliation, thermo gate

**Files** — no new ones.

```
cd /Users/inkant/learn-ai && ruff check PythonDataService/app/ PythonDataService/tests/
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/fleet/ tests/broker/alpaca/clerk/ tests/broker_configuration/ -q
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.run_fast_tests
```

Then invoke `thermo-nuclear-code-quality-review` before the first push (CLAUDE.md hard rule, one-shot per PR). If #2078 has merged by this point, strike rows 4, 5, 8 and 9 from its ranked table (`docs/broker-clerk-fleet-authority.md:260,261,264,265`) and the matching Divergences rows (`:343,348`), and rewrite the Decision-2 row to record that the `fleet_boot.py:159-162` registry open is on the *control* volume and is structurally unavoidable.

---

## 4. Grouping advice

**One PR for the lane, not four.** Tasks 1 and 4 both move where the volume gate sits relative to its callers; Task 5 bumps `SCHEMA_VERSION`, which every test in Tasks 2–4 opens a registry for; Task 3's error-family widening touches the same `errors.py` a reviewer reads for Task 6. Four PRs would rebase over each other three times and split one argument ("the gates are real now") across four review threads. Keep them as seven commits in one branch so each is independently revertable, and list the four issue numbers in the PR body.

**One exception:** if Task 5's migration turns out to fail on the live registry (see Risks), split it out and ship Tasks 1–4 and 6 first.

**Import-time boot changes — do NOT deploy via `./restart.sh` while the Live lane carries a running bot:**

- **Task 1** is the dangerous one. It rewrites `lifespan` and `_service_lifespan` — the clerk's entire startup and shutdown sequence. A `./restart.sh` drops `alpaca-live-clerk` mid-session: open orders lose their in-process writer, the trade-updates websocket closes, and the new process re-registers under a fresh `routing_epoch` and must re-confirm its binding before it routes again (`service.py:1251-1264`). Deploy only when the Live lane holds no running bot and the session is closed.
- **Task 5** changes `SCHEMA_VERSION`, so the first coordinator start after deploy runs a migration inside `FleetRegistryStore.open`. If it fails the registry does not open and *every* lane loses routing, not just the one restarting. Deploy with the same window and a fresh registry backup taken first (`manage_broker_fleet backup-registry`).
- **Task 4** changes `LocalPresence.__init__`'s signature, reached at import-adjacent boot time via `fleet_boot.py:163`. Same restart window as Task 1.
- **Tasks 2, 3, 6** are safe to deploy at any time: Task 2 is test-only; Task 3 touches only the request path inside an already-booted coordinator; Task 6 touches only `recovery.py` properties and a CLI script that runs as a host ceremony, never in the serving process.

Note that #2081 is already in this category (`main.py:969` middleware install moves under `if _ROLE_RUNS_CLERK`), so if both land in the same deploy window they share one restart.

---

## 5. Risks the issue text missed

1. **A lane opened at `main.py:391` leaks on a later startup failure — today, before any of this work.** The `close_fleet_lane` call lives in `_service_lifespan`'s `finally` at `:849-852`, attached to the `try: yield` at `:839`. Any startup exception between `:391` and `:839` — an Alpaca binding refusal, a custody open failure — skips it, leaving an `httpx` client or an open `FleetRegistryStore` behind and, for the local transport, a registry session row claiming a live agent. Task 1's restructure fixes this incidentally by moving the close into `lifespan`'s unconditional `finally`; call that out in the PR body so a reviewer does not read it as scope creep.

2. **Task 5's UNIQUE index can refuse to install on the live registry.** `CREATE UNIQUE INDEX ux_clerks_volume_root ... WHERE lifecycle_state <> 'retired'` fails if two non-retired clerks already share a `volume_root` string in one namespace. `migrate_schema` (`schema.py:762-781`) runs the whole migration in one transaction and re-raises, so the registry then refuses to open — correct fail-closed behaviour, catastrophic timing. Run the equivalent `SELECT deployment_namespace, volume_root, count(*) FROM clerks WHERE lifecycle_state <> 'retired' GROUP BY 1,2 HAVING count(*) > 1` against a *copy* of the production registry before deploying.

3. **`_fence_writable_roots` masks the identity gate.** Because it runs at `fleet_boot.py:180`, five lines before `:185`, a deployment with escaping lane roots gets `FleetBootRefused` and never learns its marker is also wrong. Task 2's test makes the fence real but does not fix the ordering; consider whether the identity gate should move ahead of the writable-root fence in a follow-up (it is read-only, so there is no correctness reason for it to be first).

4. **`SCHEMA_VERSION = 3` interacts with the D-compatible rollback posture.** `recovery.py:56` pins `D_COMPATIBLE_SCHEMA_VERSION = 2`, and `manage_broker_fleet.py:351` passes `SCHEMA_VERSION` as `max_schema_version` on a normal restore. After the bump, a v2 backup restored normally will migrate forward to v3 on open, while `--d-compatible` will not — meaning the two restore paths now produce different schema versions from one backup. That is probably intended, but nothing tests it; add a case to `test_registry_recovery.py` asserting a v2 backup restores and migrates under the normal path and stays at v2 under `--d-compatible`.

5. **`FLEET_PROTOCOL_VERSION` is *not* bumped by any task here, and that is deliberate but fragile.** `provider.py:41` pins it at 2, and `require_protocol_compatible` (`provider.py:328-345`) refuses on any mismatch including `None`. Task 3 changes when the provider gate runs, which an older agent build cannot observe (the gate runs coordinator-side), and Task 4 changes only an in-process constructor. So no bump is needed — but if a reviewer adds a payload field to `RegistrationRequest` for Task 4 (a plausible "improvement"), the version must bump and every lane must restart in lockstep. Say so in the PR body.

6. **Task 6's rename reaches a runbook a human follows during an incident.** `docs/runbooks/fleet-e-registry-recovery-exercise.md:64` instructs the operator to repeat a command until `routing_closed: false`. If the rename ships and the runbook edit is dropped in review, the operator watches for a key that no longer exists during a registry restore — the single worst moment for that. Keep the runbook edit in the same commit as the CLI change, not a follow-up.

7. **The `served_context_refusals` knob will still be dead for `stream_read` and `deliver_command` after Task 3** unless `_resolve` is the shared path — it is (`routing.py:174, 237, 318` all call it), so the gate covers all three. But the new test exercises only `deliver_read`. Add a second assertion over `deliver_command` with the fake's `bot_action` operation if the owner wants the durable-key path pinned too; it needs a valid `CommandEnvelope`, since `_validated_envelope` (`routing.py:315-317`) runs *before* `_resolve`.

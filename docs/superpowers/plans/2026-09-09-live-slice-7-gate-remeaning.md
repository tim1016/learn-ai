# ADR 0059 Slice 7 — Gate re-meaning, the live authority, and the first real order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `real_live` custody world exists and is selected at boot on three-way mode agreement; every one of Decision 11's gates admits it exactly as the ADR says; an ENTER on the live authority passes admission → per-instance arming → envelope and reaches the real Alpaca trade port; an unarmed instance's every ENTER refuses, a lost arming is refused and warned about once under `LIVE_VERDICT_TRANSITION_HALT` with no desired state written; the deploy wire, the verdict and the contract say so.

**Architecture:** One new selector (`live_authority.py`) mirrors the shadow selector over the real trade port and the same `compose_repository_runtime`; one new pure gate (`live_arming_gate.py`) holds the arming snapshot the existing `LiveEnvelopeSync` refreshes every tick from a single ledger read (the sealed bindings arrive as an injected callable); one new admission (`sqlite/arming_admission.py`) sits between `require_admission` and the envelope inside `accept_enter`. Start/Resume admission gains an arming fact that refuses only an unreadable ledger. The cutover ceremony admits `live` evidence on the strength of a shadow receipt; the deploy view offers `live`; a live instance arms on its twin's receipt; the verdict reads any facade authority. Nothing in this slice pauses a bot.

**Tech Stack:** Python 3.12, Pydantic v2 (settings, wire schemas), the Clerk's SQLite spine and JSONL sealed ledgers, FastAPI, Angular 22 + Vitest (one deploy-form spec), `openapi-typescript` codegen.

**Spec:** `docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md` (binding; Rulings R1–R16), arguing from `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` Decisions 1, 3, 8, 10, 11 and Consequence 7. Predecessors: `docs/references/alpaca-shadow-authority.md`, `alpaca-live-envelope.md`, `alpaca-live-arming.md`.

## Global Constraints

- Never commit secrets; `.env` only. **No new environment variable.** `DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL` and every `ALPACA_LIVE_*` already exist.
- Never edit sealed artifacts: `app/lean_sidecar/trading_calendar.py`, `app/utils/timestamps.py`, `app/utils/session_anchors.py`, `app/engine/consolidators/trade_bar_consolidator.py`, anything in `registry.py`'s `artifact_paths`.
- **No SQLite schema change** (`app/broker/alpaca/clerk/sqlite/schema.py` untouched). **No change to the arming ledger's record shape** (`LiveArmingRecord` / `LiveDisarmRecord` fields, `schema_version=1`).
- **One OpenAPI regeneration**, in Task 9, after every schema edit (Tasks 6, 7). Until then `export_openapi_contract.py --check` is expected to drift; from Task 9 on it must be **green**. Never regenerate twice.
- Explicit-path staging only: `git add <paths>` — never `git add -A`, never `git add .`, never `git stash` (shared checkout; other sessions' untracked files exist). Every commit message ends with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No silent exception handlers. Structured logging only: `logger.<level>(message, extra={"action": "...", ...})`. No `print()` in `app/`.
- Temporal rigor: every stamp is `int64 ms UTC` from the injected clock / `repo.clock()` / the caller's `now_ms`; never a wall clock in a test.
- File size: no *new* module may cross 1,000 lines; `runtime.py` (1,491), `bot_runner.py` (1,700+) and `cutover.py` (943) are already large and may grow **only by the lines this plan specifies**; every new behaviour goes into the new modules named here.
- Reason codes are SCREAMING_SNAKE, prefixed `LIVE_ARMING_` for the three admission codes, plus the ADR's `LIVE_VERDICT_TRANSITION_HALT`, `LIVE_CONTROL_UNAUTHENTICATED` and the reused `LIVE_MODE_DISAGREEMENT`, `LIVE_ENVELOPE_MISSING`, `LIVE_SHADOW_INCOMPLETE`.
- Python commands run from `cd /Users/inkant/learn-ai/.claude/worktrees/slice-7/PythonDataService`:
  - tests: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <paths> -q -p no:cacheprovider`
  - lint: `.venv/bin/ruff check app/ tests/ scripts/`
  - contract: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check`
  - import smoke: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main"`
- Frontend commands run from `cd /Users/inkant/learn-ai/.claude/worktrees/slice-7/Frontend`:
  - spec: `npx ng test --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts'` (exact spec path, never a directory glob)
  - lint: `npx eslint src/ --max-warnings 0`; codegen: `npm run codegen:check`
- Known pre-existing failures to ignore: none. If a suite named in a task fails before your change, baseline it against `origin/master` and say so in the PR description.

---

## File map

| File | Responsibility |
|---|---|
| `app/schemas/account_authority.py` | `CustodyWorld` gains `real_live`; `world_admits_account_mode` becomes one closed table. |
| `app/broker/alpaca/clerk/account_authority.py` | `LIVE_EVIDENCE_ACCOUNT_PREFIX`, `live_evidence_account_id_for_strategy`, `evidence_account_id_for` for `real_live`, `bind_real_alpaca_ports(account_mode=)`. |
| `app/services/sqlite_clerk_compat.py` | `projection_authority_kind(account_id, custody_world)` — the kind derived from the world, never a literal. |
| `app/broker/alpaca/clerk/live_arming.py` | Four new codes; `ARMING_ADMISSION_REASON_CODES`. |
| `app/broker/alpaca/clerk/live_arming_gate.py` (new) | `ArmingSnapshot`, `ArmingGate`. Pure. |
| `app/broker/alpaca/clerk/sqlite/arming_admission.py` (new) | `require_arming_admission` — the sibling of `envelope_admission.py`. |
| `app/broker/alpaca/clerk/sqlite/enter.py` | `accept_enter(..., arming=)`; the chain admission → arming → envelope. |
| `app/broker/alpaca/clerk/sqlite/uncertainty.py` | Arming refusals are transient (retry next clock). |
| `app/broker/alpaca/clerk/sqlite/runtime.py` | Facade carries `live_arming`; the `sqlite`+`live` construction invariant; `account_mode` property; passes the gate at ENTER. |
| `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py` | One ledger read per tick feeds the sealed envelope and the arming snapshot; the once-per-transition `live_verdict_transition_halt` warning; the mid-session mode-disagreement path. |
| `app/broker/alpaca/clerk/active_runtime.py` | `compose_repository_runtime(..., arming_gate=, instance_seals=)`. |
| `app/broker/alpaca/clerk/live_authority.py` (new) | `select_live_clerk_runtime`; `LIVE_CONTROL_UNAUTHENTICATED`; `HaltSeam`. |
| `app/broker/alpaca/clerk/active_authority.py` | The live fork: activation present ⇒ live, absent ⇒ shadow; `primary_custody_world` admits `real_live`; new selector params. |
| `app/main.py` | Threads `live_state_root`, the `halt` closure and the control flag into the selector. |
| `app/broker/alpaca/clerk/sqlite/cutover.py`, `scripts/manage_alpaca_sqlite_clerk.py` | `paper \| live` evidence; the shadow-receipt precondition; the CLI's configured-mode check for live evidence. |
| `app/schemas/run_admission.py`, `app/services/run_admission.py` | `ArmingAdmissionFact`; the `UNREADABLE` gate; the not-armed note on an admitted decision. |
| `app/services/live_arming_admission.py` (new) | `live_arming_admission_fact` — the resolver Start and Resume share. |
| `app/services/bot_start_admission.py`, `app/services/bot_resume_admission.py` | The arming fact composed into the facts. |
| `app/services/bot_runner.py` | One constructor keyword threading the resolver into both admissions — nothing else. |
| `app/services/run_replay_proof.py` | `ledger_account_id_for` reads under the writer's rule (R13). |
| `app/schemas/broker_bots.py`, `app/services/broker_v2_panel/paper_deploy_service.py`, `panel_deploy.py` | `live` on the wire; the live world's cards, copy and receipt. |
| `app/broker/alpaca/clerk/shadow_receipt.py`, `live_arming_ceremony.py` | `current_for_account`; `InstanceSeal.program`; the twin-identity receipt lookup. |
| `app/services/alpaca_live_verdict.py` | Observations widened to every facade authority; two live copy rows. |
| `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts` (+ spec, fixtures) | The Live card is selectable when offered. |
| `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts` | Regenerated once (Task 9). |
| `docs/references/alpaca-live-authority.md` (new), `alpaca-live-arming.md`, `alpaca-live-envelope.md`, `alpaca-shadow-authority.md`, `docs/architecture/engine-authority-map.md`, `CONTEXT.md` | Docs (R16). |
| `tests/broker/alpaca/clerk/live_authority_fixtures.py` (new) | One live activation, one live-sealed binding, one recording trade port — shared by Tasks 4, 6, 8. |

---

### Task 1: Worlds — `real_live` becomes a custody world

**Files:**
- Modify: `PythonDataService/app/schemas/account_authority.py:15-31`
- Modify: `PythonDataService/app/broker/alpaca/clerk/account_authority.py:20-53, 185-205, 218-230, 276-302`
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py:149-154, 451-477`
- Modify: `PythonDataService/app/services/sqlite_clerk_compat.py:299-304, 371`
- Modify: `PythonDataService/app/services/run_replay_proof.py:840-856`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py` (exists — append)

**Interfaces:**
- Produces: `CustodyWorld = Literal["real_paper", "shadow", "real_live"]`; `world_admits_account_mode(world, account_mode) -> bool` (closed table); `LIVE_EVIDENCE_ACCOUNT_PREFIX = "live-evidence:"`; `live_evidence_account_id_for_strategy(sid) -> str`; `bind_real_alpaca_ports(*, account_id, read, trade, account_mode: Literal["paper", "live"] = "paper")`; `authority_kind_in_world(account_id: str, custody_world: CustodyWorld) -> AccountAuthorityKind` (in `clerk/account_authority.py`, used by the compat seam and the replay reader).

- [ ] **Step 1: Write the failing tests**

Append to `tests/broker/alpaca/clerk/test_account_worlds.py`:

```python
import pytest

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    bind_real_alpaca_ports,
    evidence_account_id_for,
    require_real_account_id,
)
from app.schemas.account_authority import world_admits_account_mode


def test_each_world_admits_exactly_one_account_mode() -> None:
    """ADR 0059 D1 / slice 7 R8 #13: one closed table, no world admits two modes."""
    assert world_admits_account_mode("real_paper", "paper") is True
    assert world_admits_account_mode("real_paper", "live") is False
    assert world_admits_account_mode("shadow", "live") is True
    assert world_admits_account_mode("shadow", "paper") is False
    assert world_admits_account_mode("real_live", "live") is True
    assert world_admits_account_mode("real_live", "paper") is False
    assert world_admits_account_mode("real_live", None) is False


def test_real_ports_bind_the_live_world_only_when_told_the_learned_mode() -> None:
    live = bind_real_alpaca_ports(account_id="9LIVE0001", read=object(), trade=object(), account_mode="live")
    paper = bind_real_alpaca_ports(account_id="PA-TEST", read=object(), trade=object())
    assert live.authority_kind == "real_live"
    assert paper.authority_kind == "real_paper"


def test_a_live_instance_retains_its_bars_under_its_own_namespace() -> None:
    assert (
        evidence_account_id_for(mode="trade", strategy_instance_id="ema-live-1", custody_kind="real_live")
        == "live-evidence:ema-live-1"
    )
    with pytest.raises(AccountAuthorityIdentityError):
        require_real_account_id("live-evidence:ema-live-1")


@pytest.mark.parametrize(
    ("account_id", "world", "expected"),
    [
        ("PA-TEST", "real_paper", "real_paper"),
        ("shadow:9LIVE0001", "shadow", "shadow"),
        ("9LIVE0001", "real_live", "real_live"),
        # The id's own namespace wins over a world label that cannot apply to it.
        ("shadow:9LIVE0001", "real_live", "shadow"),
        ("sim:ema-1", "real_paper", "synthetic"),
    ],
)
def test_authority_kind_in_world_follows_the_world_never_a_default(account_id: str, world: str, expected: str) -> None:
    from app.broker.alpaca.clerk.account_authority import authority_kind_in_world

    assert authority_kind_in_world(account_id, world) == expected


@pytest.mark.parametrize(("world", "expected"), [("real_live", "live-evidence:ema-live-1"), ("real_paper", "paper:ema-live-1")])
def test_a_bindings_replay_ledger_is_read_where_the_primary_world_wrote_it(
    monkeypatch: pytest.MonkeyPatch, world: str, expected: str
) -> None:
    """R13: the writer (`PrimaryAccountBindingAuthority.source_bars`) and the reader name one namespace."""
    from app.services import run_replay_proof
    from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan

    monkeypatch.setattr(run_replay_proof, "primary_custody_world", lambda: world)
    binding = BrokerBotBinding(
        strategy_instance_id="ema-live-1",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id="9LIVE0001",
        run_id="ema-live-1-run-1",
        created_at_ms=1_757_000_000_000,
    )
    assert run_replay_proof.ledger_account_id_for(binding) == expected
```

If the file already pins `world_admits_account_mode("shadow", "paper") is True`, change that assertion to `is False` — the tightening is spec R8 #13 and is deliberate.

- [ ] **Step 2: Run them to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_account_worlds.py -q -p no:cacheprovider`
Expected: FAIL — `bind_real_alpaca_ports() got an unexpected keyword argument 'account_mode'`, `ImportError: cannot import name 'authority_kind_in_world'`, `AttributeError: ... has no attribute 'primary_custody_world'`, and the `real_live` table rows.

- [ ] **Step 3: Widen the worlds table**

In `app/schemas/account_authority.py` replace lines 15–31 with:

```python
# The worlds a *primary* authority can custody in — the subset of
# `AuthorityKind` a boot can select for the account a binding files its
# evidence against. The isolated `synthetic` world is never a primary
# selection. `real_live` joined in ADR 0059 slice 7.
CustodyWorld = Literal["real_paper", "shadow", "real_live"]

# The one account mode each world may custody (ADR 0059 D1, slice 7 R8 #13).
# The shadow world reads the real-money account it was activated for, so it
# admits only `live`; the real-live world custodies that same account for
# real. Written once, here, so no gate re-derives it.
_MODE_ADMITTED_BY_WORLD: dict[str, str] = {
    "real_paper": "paper",
    "shadow": "live",
    "real_live": "live",
}


def world_admits_account_mode(world: CustodyWorld, account_mode: str | None) -> bool:
    """Whether ``world`` may custody an account the broker reports in ``account_mode``.

    ``None`` — no observation — admits nothing: an unobserved account is not
    a paper account and not a live one.
    """
    return account_mode is not None and _MODE_ADMITTED_BY_WORLD[world] == account_mode
```

- [ ] **Step 4: The live evidence namespace and the mode-aware real port binding**

In `app/broker/alpaca/clerk/account_authority.py`, after the `SHADOW_EVIDENCE_ACCOUNT_PREFIX` block (line 41) add:

```python
LIVE_EVIDENCE_ACCOUNT_PREFIX = "live-evidence:"
"""Instance-scoped evidence namespace for real-live retained source bars (slice 7).

Custody is the live account id itself; every instance that trades on it keeps
its own retained-bar ledger here, exactly as a paper instance keeps
``paper:<instance>`` and a shadow instance ``shadow-evidence:<instance>``. A
live instance's warmup then reads exactly what *it* retained, never its
former paper or shadow self's ledger, and the name is honest.
"""
```

Add `LIVE_EVIDENCE_ACCOUNT_PREFIX,` to `_RESERVED_PREFIXES` (after `SHADOW_EVIDENCE_ACCOUNT_PREFIX,`) and extend the refusal message at line 76–77 to `"real Alpaca ports refuse the reserved sim:/shadow:/paper:/shadow-evidence:/live-evidence: account identities"`.

After `shadow_evidence_account_id_for_strategy` add:

```python
def live_evidence_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the isolated real-live source-bar namespace for one instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{LIVE_EVIDENCE_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"
```

In `evidence_account_id_for`, before the final `return paper_evidence_account_id_for_strategy(...)`, insert:

```python
    if custody_kind == "real_live":
        return live_evidence_account_id_for_strategy(strategy_instance_id)
```

and extend its docstring's list with "``live-evidence:`` on the real-live authority".

Replace `bind_real_alpaca_ports` (lines 218–230) with:

```python
def bind_real_alpaca_ports(
    *,
    account_id: str,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    account_mode: Literal["paper", "live"] = "paper",
) -> AccountBoundBrokerPorts:
    """Create a real-account composition only after rejecting reserved namespaces.

    ``account_mode`` is what the caller positively learned from the broker
    (ADR 0054 / ADR 0059 D1); it decides ``real_paper`` versus ``real_live``
    and is never inferred from the id's shape.
    """
    return AccountBoundBrokerPorts(
        account_id=require_real_account_id(account_id),
        authority_kind=authority_kind_for_account(account_id, account_mode=account_mode),
        read=read,
        trade=trade,
    )
```

After `authority_kind_for_account` add the one predicate the compat seam and the replay reader share:

```python
def authority_kind_in_world(account_id: str, custody_world: CustodyWorld) -> AccountAuthorityKind:
    """The read-contract kind for a custody id, in the world the primary authority custodies in.

    The id's own reserved namespace (``sim:``, ``shadow:``) always decides; a
    real id is ``real_live`` only when the primary world is the live one
    (ADR 0059 slice 7). Derived, never defaulted: the wire's ``real_paper``
    default is exactly the misstatement a live account must never carry, and
    the writer of a live instance's evidence ledger and the reader of it must
    agree on which namespace that is (design R13).
    """
    return authority_kind_for_account(
        account_id, account_mode="live" if custody_world == "real_live" else "paper"
    )
```

Add `"LIVE_EVIDENCE_ACCOUNT_PREFIX",`, `"authority_kind_in_world",` and `"live_evidence_account_id_for_strategy",` to `__all__` (alphabetical positions).

- [ ] **Step 5: The selector's world reader and the paper path's explicit mode**

In `app/broker/alpaca/clerk/active_authority.py`:
- Line 150–154: `bind_real_alpaca_ports(account_id=account.account_id, read=read, trade=trade, account_mode="paper")`.
- Line 460: `return kind if kind in ("real_paper", "shadow", "real_live") else None`.
- Lines 463–477, replace the docstring's parenthetical "(the synthetic world is never primary, and ``real_live`` is unconstructible until ADR 0059 slice 7)" with "(the synthetic world is never primary)". Behaviour unchanged.

- [ ] **Step 6: The compat seam and the replay reader derive the kind from the world**

In `app/services/sqlite_clerk_compat.py`: import `authority_kind_in_world` from `app.broker.alpaca.clerk.account_authority` (beside `authority_kind_for_account`, which this module then no longer uses — drop that import if it becomes unused). Replace line 304 with `authority_kind=authority_kind_in_world(projection.account_id, custody_world),` and the five-line comment above it (lines 299–303) with one: `# Derived from the id and the primary's world, never asserted (slice 4 / slice 7, R12).` In `sqlite_custody_diagnosis` replace line 371 with `authority_kind=authority_kind_in_world(projection.account_id, custody_world_or_paper(primary_custody_world())),`.

In `app/services/run_replay_proof.py` (lines 840–856), import `custody_world_or_paper` and `primary_custody_world` from `app.broker.alpaca.clerk.active_authority` and `authority_kind_in_world` from `app.broker.alpaca.clerk.account_authority` (module level, beside the existing `authority_kind_for_account` import, which this function then no longer uses), and replace the `custody_kind = (...)` expression with:

```python
    # Read under the rule the writer used (design R13): the ledger was retained
    # by `PrimaryAccountBindingAuthority.source_bars` in the primary world's
    # kind, so a live instance's proof must not go looking in `paper:`.
    custody_kind = (
        "real_paper"
        if binding.sealed_account_id is None
        else authority_kind_in_world(
            binding.sealed_account_id, custody_world_or_paper(primary_custody_world())
        )
    )
```

- [ ] **Step 7: Run the tests and the neighbours**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_account_worlds.py tests/broker/alpaca/clerk/test_active_authority.py tests/broker/v2panel/test_panel_deploy_shadow.py tests/services/test_run_replay_proof.py tests/broker/alpaca/clerk/sqlite/test_account_operator_posture.py -q -p no:cacheprovider`
Expected: PASS. If a listed test file does not exist, drop it from the list; if any test pinned "shadow admits a paper account", that pin changes with R8 #13 — update it and name the change in the commit body.

- [ ] **Step 8: Commit**

```bash
git add PythonDataService/app/schemas/account_authority.py PythonDataService/app/broker/alpaca/clerk/account_authority.py PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/app/services/sqlite_clerk_compat.py PythonDataService/app/services/run_replay_proof.py PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py
git commit -m "feat(alpaca): real_live is a custody world (ADR 0059 slice 7, R8 #13, R13)

One closed world→mode table; real ports bind the learned mode; a live
instance retains its bars under live-evidence:; the wire's authority kind
is derived from the primary's world, never the real_paper default.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The arming gate and its admission — codes, `ArmingGate`, `require_arming_admission`, `accept_enter(arming=)`

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/live_arming.py:40-73, 416-442`
- Create: `PythonDataService/app/broker/alpaca/clerk/live_arming_gate.py`
- Create: `PythonDataService/app/broker/alpaca/clerk/sqlite/arming_admission.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py:70-73, 149-213`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py:766-773`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_live_arming_gate.py` (new), `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_arming_admission.py` (new), `PythonDataService/tests/broker/alpaca/clerk/test_live_arming.py` (the closed-set count)

**Interfaces:**
- Consumes: `arming_status`, `ArmingStatus`, `LedgerRecord`, `LiveArmingRecord.create` (`live_arming.py`); `OBSERVATION_MAX_AGE_MS`, `LiveEnvelopeValues` (`live_envelope.py`); `AdmissionBlockedError`, `Capability`, `CapabilityDecision` (`sqlite/uncertainty.py`).
- Produces (Tasks 3, 4, 6 rely on these exact names):
  - codes `LIVE_ARMING_REQUIRED`, `LIVE_ARMING_UNOBSERVED`, `LIVE_ARMING_LEDGER_INVALID`, `LIVE_VERDICT_TRANSITION_HALT`, `LIVE_MODE_DISAGREEMENT` (the adapter's own code, restated here so the gate can name it); `ARMING_ADMISSION_REASON_CODES: frozenset[str]`
  - `ArmingSnapshot(observed_at_ms: int, live_account_id: str, records: tuple[LedgerRecord, ...], seals: Mapping[str, str], configured_envelope: LiveEnvelopeValues)` with `status_for(strategy_instance_id, *, now_ms) -> ArmingStatus` and `armed_instance_ids(now_ms) -> frozenset[str]`
  - `ArmingGate(*, observation_max_age_ms=OBSERVATION_MAX_AGE_MS)` with `publish(snapshot)`, `invalidate(why: str, *, reason_code: str = LIVE_ARMING_LEDGER_INVALID)`, `invalid_why: str | None`, `invalid_reason_code: str | None`, `latest_snapshot() -> ArmingSnapshot | None`, `fresh_snapshot(now_ms) -> ArmingSnapshot | None`
  - `require_arming_admission(gate: ArmingGate, *, strategy_instance_id: str, now_ms: int) -> ArmingStatus`
  - `accept_enter(..., arming: ArmingGate | None = None)`

- [ ] **Step 1: Write the failing gate tests**

`tests/broker/alpaca/clerk/test_live_arming_gate.py`:

```python
"""The per-instance arming gate is a pure cache of one ledger read (ADR 0059 slice 7, R5)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.broker.alpaca.clerk.live_arming import (
    ARMING_ADMISSION_REASON_CODES,
    ARMING_REASON_CODES,
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_LEDGER_INVALID,
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
    LIVE_VERDICT_TRANSITION_HALT,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS
from tests.broker.alpaca.clerk.live_arming_fixtures import ARMED_AT_MS, ARMING_SID
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES

SEAL = "a" * 64
NOW = ARMED_AT_MS + 60_000
ONE_WEEK_MS = 7 * 86_400_000


def _record(*, envelope=TEST_ENVELOPE_VALUES, armed_at_ms: int = ARMED_AT_MS) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=ARMING_SID,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=envelope.arming_max_sessions,
    )


def _snapshot(records, *, observed_at_ms: int = NOW, seals=None) -> ArmingSnapshot:
    return ArmingSnapshot(
        observed_at_ms=observed_at_ms,
        live_account_id=LIVE_ACCT,
        records=tuple(records),
        seals={ARMING_SID: SEAL} if seals is None else seals,
        configured_envelope=TEST_ENVELOPE_VALUES,
    )


def test_the_new_codes_are_in_the_closed_set_and_the_admission_subset_is_closed() -> None:
    for code in (
        LIVE_ARMING_REQUIRED,
        LIVE_ARMING_UNOBSERVED,
        LIVE_ARMING_LEDGER_INVALID,
        LIVE_VERDICT_TRANSITION_HALT,
    ):
        assert code in ARMING_REASON_CODES
        assert code == code.upper()
    assert ARMING_ADMISSION_REASON_CODES <= ARMING_REASON_CODES
    assert LIVE_VERDICT_TRANSITION_HALT not in ARMING_ADMISSION_REASON_CODES


def test_a_gate_starts_unobserved_and_publishing_makes_a_fresh_snapshot() -> None:
    gate = ArmingGate()
    assert gate.fresh_snapshot(NOW) is None
    gate.publish(_snapshot([_record()]))
    fresh = gate.fresh_snapshot(NOW)
    assert fresh is not None
    assert fresh.status_for(ARMING_SID, now_ms=NOW).state == "armed"


@pytest.mark.parametrize("age_ms", [OBSERVATION_MAX_AGE_MS + 1, -1])
def test_a_stale_or_future_snapshot_is_not_fresh(age_ms: int) -> None:
    gate = ArmingGate()
    gate.publish(_snapshot([_record()], observed_at_ms=NOW - age_ms))
    assert gate.fresh_snapshot(NOW) is None
    assert gate.latest_snapshot() is not None


def test_invalidate_drops_the_snapshot_and_publish_clears_the_fault() -> None:
    gate = ArmingGate()
    gate.publish(_snapshot([_record()]))
    gate.invalidate("digest does not verify")
    assert gate.invalid_why == "digest does not verify"
    assert gate.invalid_reason_code == LIVE_ARMING_LEDGER_INVALID
    assert gate.latest_snapshot() is None
    gate.publish(_snapshot([_record()]))
    assert gate.invalid_why is None
    assert gate.invalid_reason_code is None


def test_a_gate_can_be_invalidated_under_the_mode_disagreement_code() -> None:
    from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT
    from app.broker.contract.errors import BrokerAccountModeDisagreement

    gate = ArmingGate()
    gate.invalidate("the broker answered paper", reason_code=LIVE_MODE_DISAGREEMENT)
    assert gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT
    # One string, restated: the adapter's exception and the gate name the same code.
    assert BrokerAccountModeDisagreement("m", broker="alpaca", detail="d").reason_code == LIVE_MODE_DISAGREEMENT


def test_armed_instance_ids_lists_exactly_the_armed_instances() -> None:
    snapshot = _snapshot([_record()])
    assert snapshot.armed_instance_ids(NOW) == frozenset({ARMING_SID})
    assert _snapshot([_record()], seals={}).armed_instance_ids(NOW) == frozenset()


def test_status_is_derived_at_the_callers_instant_not_the_snapshots() -> None:
    """A lapse at the ET-date boundary is enforced when asked, not at the next tick."""
    one_session = replace(TEST_ENVELOPE_VALUES, arming_max_sessions=1)
    snapshot = ArmingSnapshot(
        observed_at_ms=NOW,
        live_account_id=LIVE_ACCT,
        records=(_record(envelope=one_session, armed_at_ms=ARMED_AT_MS),),
        seals={ARMING_SID: SEAL},
        configured_envelope=one_session,
    )
    assert snapshot.status_for(ARMING_SID, now_ms=NOW).state == "armed"
    lapsed = snapshot.status_for(ARMING_SID, now_ms=NOW + ONE_WEEK_MS)
    assert (lapsed.state, lapsed.reason_code) == ("lapsed", LIVE_ARMING_LAPSED)


def test_an_instance_with_no_seal_on_disk_is_seal_changed_not_armed() -> None:
    snapshot = _snapshot([_record()], seals={})
    assert snapshot.status_for(ARMING_SID, now_ms=NOW).reason_code == "LIVE_ARMING_SEAL_CHANGED"
```

- [ ] **Step 2: Write the failing admission tests**

`tests/broker/alpaca/clerk/sqlite/test_arming_admission.py` (the `envelope_repo` and `active_instance` fixtures come from `tests/broker/alpaca/clerk/sqlite/conftest.py`, exactly as `test_envelope_admission.py` uses them; `ENVELOPE_T0` is that conftest's pinned clock):

```python
"""Per-instance arming at the ENTER seam (ADR 0059 slice 7, R5).

``accept_enter`` gains a third gate between ``require_admission`` and the
envelope. Driven through the public entry point, like the envelope tests:
what is pinned is which ENTER is admitted, which refusal each fact produces,
in which order, and that a refusal writes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_LEDGER_INVALID,
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_UNOBSERVED,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    TRANSIENT_ADMISSION_REASON_CODES,
    AdmissionBlockedError,
)
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0

SEAL = "a" * 64
ONE_WEEK_MS = 7 * 86_400_000


def _record(sid: str, *, envelope=TEST_ENVELOPE_VALUES, armed_at_ms: int = T0 - 60_000) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=sid,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=envelope.arming_max_sessions,
    )


def _arming(sid: str, *records: LiveArmingRecord, observed_at_ms: int = T0, envelope=TEST_ENVELOPE_VALUES) -> ArmingGate:
    gate = ArmingGate()
    gate.publish(
        ArmingSnapshot(
            observed_at_ms=observed_at_ms,
            live_account_id=LIVE_ACCT,
            records=tuple(records),
            seals={sid: SEAL},
            configured_envelope=envelope,
        )
    )
    return gate


def _envelope(observed: bool = True) -> LiveEnvelopeGate:
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False)
    if observed:
        gate.publish(
            AccountObservation(
                observed_at_ms=T0,
                broker_cash_usd=100_000.0,
                cash_available_usd=100_000.0,
                last_equity_usd=100_000.0,
                unrealized_pl_usd=0.0,
                position_count=0,
            )
        )
    return gate


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _accept(repo: ClerkSqliteRepository, sid: str, run_id: str, *, arming: ArmingGate | None, envelope: LiveEnvelopeGate | None, decision_id: str = "d1"):
    return accept_enter(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=sid,
        decision_id=decision_id,
        lifecycle_run_id=run_id,
        leg=_leg(),
        arming=arming,
        envelope=envelope,
        reference_price=100.0,
    )


def _refusal(exc_info: pytest.ExceptionInfo[AdmissionBlockedError]) -> str | None:
    return exc_info.value.decision.reason_code


def test_an_armed_instance_is_admitted_through_all_three_gates(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    accepted = _accept(envelope_repo, sid, run_id, arming=_arming(sid, _record(sid)), envelope=_envelope())
    assert accepted.created
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == pytest.approx(100.0)


def test_no_gate_means_no_arming_check_paper_and_shadow_unchanged(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    assert _accept(envelope_repo, sid, run_id, arming=None, envelope=_envelope()).created


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        pytest.param(ArmingGate(), LIVE_ARMING_UNOBSERVED, id="never-refreshed"),
        pytest.param(_arming("x", observed_at_ms=T0 - OBSERVATION_MAX_AGE_MS - 1), LIVE_ARMING_UNOBSERVED, id="stale-snapshot"),
    ],
)
def test_an_unobserved_gate_refuses_closed(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str], gate: ArmingGate, expected: str
) -> None:
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == expected


def test_an_invalid_ledger_refuses_before_freshness_is_even_asked(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    gate = _arming(sid, _record(sid))
    gate.invalidate("row 3: digest does not verify")
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == LIVE_ARMING_LEDGER_INVALID
    assert "digest does not verify" in (exc_info.value.decision.why or "")


def test_a_mid_session_mode_disagreement_refuses_under_its_own_code(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT

    sid, run_id = active_instance
    gate = _arming(sid, _record(sid))
    gate.invalidate("the broker answered paper", reason_code=LIVE_MODE_DISAGREEMENT)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == LIVE_MODE_DISAGREEMENT


def test_a_never_armed_instance_is_required_and_nothing_is_written(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    before = envelope_repo.control_meta_snapshot().control_revision
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=_arming(sid), envelope=_envelope())
    assert _refusal(exc_info) == LIVE_ARMING_REQUIRED
    assert envelope_repo.control_meta_snapshot().control_revision == before
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == 0.0


def test_a_lapsed_instance_refuses_with_its_own_code(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    one_session = replace(TEST_ENVELOPE_VALUES, arming_max_sessions=1)
    gate = _arming(sid, _record(sid, envelope=one_session, armed_at_ms=T0 - ONE_WEEK_MS), envelope=one_session)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == LIVE_ARMING_LAPSED


def test_arming_runs_before_the_envelope_so_an_unarmed_instance_never_reserves_cash(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    """Both gates would refuse; the arming refusal is the one that names the ENTER."""
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=_arming(sid), envelope=_envelope(observed=False))
    assert _refusal(exc_info) == LIVE_ARMING_REQUIRED
    assert _refusal(exc_info) != LIVE_ENVELOPE_UNOBSERVED


def test_every_arming_refusal_retries_on_the_next_clock() -> None:
    from app.broker.alpaca.clerk.live_arming import ARMING_ADMISSION_REASON_CODES

    assert ARMING_ADMISSION_REASON_CODES <= TRANSIENT_ADMISSION_REASON_CODES
```

- [ ] **Step 3: Run both files to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming_gate.py tests/broker/alpaca/clerk/sqlite/test_arming_admission.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError` on `live_arming_gate` and on the four codes.

- [ ] **Step 4: The codes**

In `app/broker/alpaca/clerk/live_arming.py`, after line 55 (`LIVE_SHADOW_INCOMPLETE = ...`) add:

```python
# Slice 7 (ADR 0059 D11): the admission-time codes. ``REQUIRED`` is an instance
# the ledger has never named; ``UNOBSERVED`` is a gate with no fresh snapshot;
# ``LEDGER_INVALID`` is a snapshot the last refresh could not verify. The
# fourth is ADR 0059 D8's own name for the pause a lost arming causes.
LIVE_ARMING_REQUIRED = "LIVE_ARMING_REQUIRED"
LIVE_ARMING_UNOBSERVED = "LIVE_ARMING_UNOBSERVED"
LIVE_ARMING_LEDGER_INVALID = "LIVE_ARMING_LEDGER_INVALID"
LIVE_VERDICT_TRANSITION_HALT = "LIVE_VERDICT_TRANSITION_HALT"
# The adapter's own refusal (`BrokerAccountModeDisagreement.reason_code`),
# restated so the gate can name it when the broker's mode moves mid-session
# (design R2); a test pins the two strings equal.
LIVE_MODE_DISAGREEMENT = "LIVE_MODE_DISAGREEMENT"
```

Add the first four names to `ARMING_REASON_CODES` (the set is now seventeen; `LIVE_MODE_DISAGREEMENT` is the adapter's code and stays out of it). After it add:

```python
# What ``sqlite/arming_admission.require_arming_admission`` may refuse with —
# the three codes above plus the per-instance states ``arming_status`` names.
# ``uncertainty.py`` classifies all of them transient: the reaction to a lost
# arming is the tick rule's pause, never a fatal halt from inside admission.
ARMING_ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        LIVE_ARMING_REQUIRED,
        LIVE_ARMING_UNOBSERVED,
        LIVE_ARMING_LEDGER_INVALID,
        LIVE_ARMING_LAPSED,
        LIVE_ARMING_REVOKED,
        LIVE_ARMING_SEAL_CHANGED,
        LIVE_ARMING_FUTURE_DATED,
        LIVE_ENVELOPE_DISAGREEMENT,
        LIVE_MODE_DISAGREEMENT,
    }
)
```

Add `"ARMING_ADMISSION_REASON_CODES"`, `"LIVE_ARMING_LEDGER_INVALID"`, `"LIVE_ARMING_REQUIRED"`, `"LIVE_ARMING_UNOBSERVED"`, `"LIVE_MODE_DISAGREEMENT"`, `"LIVE_VERDICT_TRANSITION_HALT"` to `__all__`. In `tests/broker/alpaca/clerk/test_live_arming.py`, the closed-set test that counts thirteen codes now counts **seventeen** — update that one integer and nothing else.

- [ ] **Step 5: The gate**

Create `app/broker/alpaca/clerk/live_arming_gate.py`:

```python
"""The per-instance arming gate a live authority admits ENTERs against (ADR 0059 D3/D11, slice 7).

Formula: ``status_for(sid, now_ms) = arming_status(records, seal_hash=seals[sid], now_ms)``
  over one snapshot of the ledger and the runner's sealed bindings; the
  snapshot is fresh iff ``0 <= now_ms - observed_at_ms <= max_age``.
Reference: design R5 in
  ``docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md``.
Canonical implementation: this file. The status rule is ``live_arming.py``;
  the refresh is ``sqlite/live_envelope_sync.py``; the admission is
  ``sqlite/arming_admission.py``.
Validated against: ``tests/broker/alpaca/clerk/test_live_arming_gate.py``.

Pure: no I/O, no clock. The sync publishes a snapshot it stamped with the
repository clock; admission derives the instance's state at *its own*
``now_ms``, so a lapse at the ET-date boundary is enforced at the instant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_LEDGER_INVALID,
    ArmingStatus,
    LedgerRecord,
    arming_status,
    instance_ids,
)
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS, LiveEnvelopeValues


@dataclass(frozen=True)
class ArmingSnapshot:
    """One verified read of the ledger and the sealed bindings, at one instant."""

    observed_at_ms: int
    live_account_id: str
    records: tuple[LedgerRecord, ...]
    # ``strategy_instance_id`` -> the instance's current sealed-program hash.
    # An instance absent here has no sealed binding on this account any more,
    # which ``arming_status`` reads as a change from whatever was armed.
    seals: Mapping[str, str]
    configured_envelope: LiveEnvelopeValues

    def status_for(self, strategy_instance_id: str, *, now_ms: int) -> ArmingStatus:
        return arming_status(
            self.records,
            live_account_id=self.live_account_id,
            strategy_instance_id=strategy_instance_id,
            seal_hash=self.seals.get(strategy_instance_id),
            configured_envelope=self.configured_envelope,
            now_ms=now_ms,
        )

    def armed_instance_ids(self, now_ms: int) -> frozenset[str]:
        """Every instance the ledger names that is ``armed`` at ``now_ms``."""
        return frozenset(
            sid for sid in instance_ids(self.records) if self.status_for(sid, now_ms=now_ms).state == "armed"
        )


class ArmingGate:
    """The snapshot one authority admits ENTERs against.

    A process-local cache of local evidence, never a custody fact — the
    ``LiveEnvelopeGate`` precedent. ``invalidate`` drops the snapshot so a
    ledger nobody can read — or a broker whose mode no longer agrees (R2) —
    admits nothing, and says why under which code.
    """

    def __init__(self, *, observation_max_age_ms: int = OBSERVATION_MAX_AGE_MS) -> None:
        self._max_age_ms = observation_max_age_ms
        self._snapshot: ArmingSnapshot | None = None
        self._invalid: tuple[str, str] | None = None

    def publish(self, snapshot: ArmingSnapshot) -> None:
        self._snapshot = snapshot
        self._invalid = None

    def invalidate(self, why: str, *, reason_code: str = LIVE_ARMING_LEDGER_INVALID) -> None:
        self._snapshot = None
        self._invalid = (reason_code, why)

    @property
    def invalid_why(self) -> str | None:
        return None if self._invalid is None else self._invalid[1]

    @property
    def invalid_reason_code(self) -> str | None:
        return None if self._invalid is None else self._invalid[0]

    def latest_snapshot(self) -> ArmingSnapshot | None:
        return self._snapshot

    def fresh_snapshot(self, now_ms: int) -> ArmingSnapshot | None:
        """The published snapshot, if its age at ``now_ms`` is in range.

        Fresh means ``0 <= age <= max_age`` — the envelope's rule: a snapshot
        dated after the admission clock is not fresh either.
        """
        snapshot = self._snapshot
        if snapshot is None or not (0 <= now_ms - snapshot.observed_at_ms <= self._max_age_ms):
            return None
        return snapshot


__all__ = ["ArmingGate", "ArmingSnapshot"]
```

- [ ] **Step 6: The admission**

Create `app/broker/alpaca/clerk/sqlite/arming_admission.py`:

```python
"""Per-instance arming at ENTER — the third gate, between admission and the envelope (ADR 0059 D11, slice 7).

Order of refusals, each fail-closed: a ledger the last refresh could not
verify; no fresh snapshot; then the instance's own state — never armed,
lapsed, or disarmed under its reason code. EXIT is never subject to this
(Decisions 3 and 4). Nothing here touches a file, a broker or a clock: the
sync published the snapshot, and the caller's ``now_ms`` — the repository
clock — decides freshness and lapse.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
    ArmingStatus,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
)


def _refuse(reason_code: str, why: str) -> AdmissionBlockedError:
    return AdmissionBlockedError(
        CapabilityDecision(
            allowed=False,
            capability=Capability.NEW_EXPOSURE,
            reason_code=reason_code,
            why=why,
        )
    )


def require_arming_admission(gate: ArmingGate, *, strategy_instance_id: str, now_ms: int) -> ArmingStatus:
    """Admit one ENTER only for an instance the ledger says is armed right now."""
    reason_code = gate.invalid_reason_code
    if reason_code is not None:
        raise _refuse(
            reason_code,
            f"The live authority admits no ENTER while its arming evidence cannot be judged "
            f"({gate.invalid_why}); restore it and let the sync observe again.",
        )
    snapshot = gate.fresh_snapshot(now_ms)
    if snapshot is None:
        raise _refuse(
            LIVE_ARMING_UNOBSERVED,
            "No fresh arming snapshot exists; the live authority cannot tell whether this instance is armed.",
        )
    status = snapshot.status_for(strategy_instance_id, now_ms=now_ms)
    if status.state == "armed":
        return status
    if status.state == "unarmed":
        raise _refuse(
            LIVE_ARMING_REQUIRED,
            f"{strategy_instance_id} has never been armed on {snapshot.live_account_id}; "
            "run scripts.manage_alpaca_arming plan, then apply.",
        )
    raise _refuse(
        status.reason_code or LIVE_ARMING_REQUIRED,
        f"{strategy_instance_id} is {status.state} on {snapshot.live_account_id} "
        f"({status.reason_code}); re-arm before its next ENTER.",
    )


__all__ = ["require_arming_admission"]
```

- [ ] **Step 7: `accept_enter` runs the chain, and the codes are transient**

In `app/broker/alpaca/clerk/sqlite/enter.py`:
- After line 70 add `from app.broker.alpaca.clerk.live_arming_gate import ArmingGate` and after line 73 add `from app.broker.alpaca.clerk.sqlite.arming_admission import require_arming_admission`.
- In `accept_enter`'s signature add `arming: ArmingGate | None = None,` immediately before `envelope: LiveEnvelopeGate | None = None,`.
- In its docstring, before the ``envelope`` paragraph, add: ``arming`` is the live authority's per-instance arming gate (ADR 0059 D11). When supplied it runs after ``require_admission`` and **before** the envelope, so an unarmed instance never reserves cash; ``None`` means no arming check runs (paper, shadow).
- In `build_transition`, immediately after `require_admission(repo, strategy_instance_id=strategy_instance_id)` (line 202) insert:

```python
        if arming is not None:
            require_arming_admission(arming, strategy_instance_id=strategy_instance_id, now_ms=repo.clock())
```

In `app/broker/alpaca/clerk/sqlite/uncertainty.py`, at the `TRANSIENT_ADMISSION_REASON_CODES` definition (lines 766–773) append `| ARMING_ADMISSION_REASON_CODES` after `| ENVELOPE_ADMISSION_REASON_CODES`, import `ARMING_ADMISSION_REASON_CODES` from `app.broker.alpaca.clerk.live_arming` beside the `live_envelope` import, and extend the comment at lines 757–761: "Every arming refusal joins them too (slice 7): the reaction to a lost arming is the sync's pause, never a halt from admission."

- [ ] **Step 8: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming_gate.py tests/broker/alpaca/clerk/sqlite/test_arming_admission.py tests/broker/alpaca/clerk/test_live_arming.py tests/broker/alpaca/clerk/sqlite/test_envelope_admission.py tests/broker/alpaca/clerk/sqlite/test_uncertainty.py -q -p no:cacheprovider`
Expected: PASS (drop `test_uncertainty.py` from the list if no such file exists).

- [ ] **Step 9: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/live_arming.py PythonDataService/app/broker/alpaca/clerk/live_arming_gate.py PythonDataService/app/broker/alpaca/clerk/sqlite/arming_admission.py PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py PythonDataService/app/broker/alpaca/clerk/sqlite/uncertainty.py PythonDataService/tests/broker/alpaca/clerk/test_live_arming_gate.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_arming_admission.py PythonDataService/tests/broker/alpaca/clerk/test_live_arming.py
git commit -m "feat(alpaca): per-instance arming is the third ENTER admission (ADR 0059 slice 7, R5, R11)

ArmingGate caches one verified ledger read; require_arming_admission runs
after the holds and before the envelope so an unarmed instance never
reserves cash; an unreadable ledger refuses instead of unsealing.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The facade and the sync — one ledger read feeds the seal, the gate and the halt seam

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:201-247, 269-281, 834-843`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py:20-53, 152-192, 265-335`
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_runtime.py:52-66, 243-275, 295-306, 359-372`
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py` (new), `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime_live_invariant.py` (new)

**Interfaces:**
- Consumes: `ArmingGate`, `ArmingSnapshot`, `LIVE_MODE_DISAGREEMENT`, `LIVE_VERDICT_TRANSITION_HALT` (Task 2); `LiveArmingLedger.records()`, `latest_arming` (slice 6); `BrokerAccountModeDisagreement` (the exception `adapter.py:192` raises — import it from the module `adapter.py` imports it from).
- Produces (Task 4 relies on these):
  - `type InstanceSeals = Callable[[], Mapping[str, str]]` (in `live_envelope_sync.py`)
  - `LiveEnvelopeSync(..., arming_ledger=, arming_gate: ArmingGate | None = None, instance_seals: InstanceSeals | None = None)`; `_refresh_arming() -> ArmingSnapshot | None` replaces `_refresh_sealed_envelope`; `EnvelopeSyncAction` gains `"mode_disagreed"`
  - `compose_repository_runtime(..., arming_ledger=, arming_gate=, instance_seals=)`
  - `SqliteAlpacaClerkFacade(..., live_envelope=, live_arming: ArmingGate | None = None)`; properties `live_arming`, `account_mode`

- [ ] **Step 1: Write the failing sync tests**

`tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py`:

```python
"""One tick, one ledger read: the seal, the arming snapshot, the transition warning (slice 7, R2, R5, R10)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_REVOKED,
    LIVE_MODE_DISAGREEMENT,
    LIVE_VERDICT_TRANSITION_HALT,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerAccountModeDisagreement
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0

SID = "ema-live-1"
SEAL = "a" * 64


class _DisagreeingBroker(_LiveBroker):
    """The live account whose broker-reported mode can stop agreeing mid-session."""

    disagree = False

    async def get_account(self):
        if self.disagree:
            raise BrokerAccountModeDisagreement(
                "The configured Alpaca mode and the observed account disagree.",
                broker="alpaca",
                detail="ALPACA_MODE='live' but the account number begins with 'PA'",
            )
        return await super().get_account()


def _halts(caplog: pytest.LogCaptureFixture) -> list:
    return [r for r in caplog.records if getattr(r, "action", None) == "live_verdict_transition_halt"]


def _record() -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=SID,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=TEST_ENVELOPE_VALUES,
        armed_at_ms=T0 - 60_000,
        max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
    )


def _sync(
    repo: ClerkSqliteRepository,
    ledger: LiveArmingLedger,
    gate: ArmingGate,
    seals: Mapping[str, str],
    broker: _LiveBroker | None = None,
) -> LiveEnvelopeSync:
    return LiveEnvelopeSync(
        repo=repo,
        read=broker or _LiveBroker(now_ms=T0),
        envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False),
        arming_ledger=ledger,
        arming_gate=gate,
        instance_seals=lambda: seals,
    )


async def test_a_tick_publishes_the_snapshot_and_seals_the_envelope_from_one_read(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    await sync.tick()

    snapshot = gate.fresh_snapshot(T0)
    assert snapshot is not None
    assert snapshot.live_account_id == LIVE_ACCT
    assert snapshot.status_for(SID, now_ms=T0).state == "armed"
    assert sync.envelope.sealed == TEST_ENVELOPE_VALUES
    assert sync.envelope.agreement == "agreed"


async def test_an_empty_ledger_publishes_an_unarmed_snapshot(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    gate = ArmingGate()
    sync = _sync(envelope_repo, LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT), gate, {})

    await sync.tick()

    snapshot = gate.fresh_snapshot(T0)
    assert snapshot is not None
    assert snapshot.status_for(SID, now_ms=T0).state == "unarmed"
    assert sync.envelope.sealed is None


async def test_an_unreadable_ledger_invalidates_the_gate_and_logs_once(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    ledger.path.write_text(ledger.path.read_text(encoding="utf-8").replace('"kind":"armed"', '"kind":"armed ', 1), encoding="utf-8")
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("ERROR"):
        await sync.tick()
        await sync.tick()

    assert gate.invalid_reason_code == "LIVE_ARMING_LEDGER_INVALID"
    assert gate.fresh_snapshot(T0) is None
    assert sync.envelope.sealed is None
    assert sum(1 for r in caplog.records if getattr(r, "action", None) == "live_arming_ledger_invalid") == 1


async def test_an_instance_leaving_armed_is_warned_about_once_with_its_code(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR 0059 D8 as R10 builds it: the loud part of the halt is one warning per transition."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("WARNING"):
        await sync.tick()
        assert _halts(caplog) == []
        ledger.revoke_latest(SID, disarmed_at_ms=T0)
        await sync.tick()
        await sync.tick()

    halts = _halts(caplog)
    assert len(halts) == 1
    assert halts[0].reason_code == LIVE_ARMING_REVOKED
    assert halts[0].strategy_instance_id == SID
    assert halts[0].live_account_id == LIVE_ACCT
    assert LIVE_VERDICT_TRANSITION_HALT in halts[0].getMessage()


async def test_a_mid_session_mode_disagreement_invalidates_the_gate_until_a_read_agrees_again(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: the one verdict input that can move mid-session refuses under its own code."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    broker = _DisagreeingBroker(now_ms=T0)
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL}, broker=broker)
    await sync.tick()
    assert gate.fresh_snapshot(T0) is not None

    broker.disagree = True
    with caplog.at_level("WARNING"):
        assert await sync.tick() == "mode_disagreed"
        await sync.tick()

    assert gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT
    assert sync.envelope.fresh_observation(T0) is None
    assert sum(1 for r in caplog.records if getattr(r, "action", None) == "live_envelope_mode_disagreed") == 1

    broker.disagree = False
    await sync.tick()
    assert gate.invalid_reason_code is None
    assert gate.fresh_snapshot(T0) is not None


async def test_without_a_gate_the_sync_only_seals_as_in_slice_6(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    sync = LiveEnvelopeSync(
        repo=envelope_repo,
        read=_LiveBroker(now_ms=T0),
        envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True),
        arming_ledger=ledger,
    )
    await sync.tick()
    assert sync.envelope.sealed == TEST_ENVELOPE_VALUES
```

If the ledger's canonical JSON does not contain the literal `"kind":"armed"` (check with `cat` on the file), corrupt the row by appending one character to its `record_sha256` value instead — any change that makes the row's digest fail to verify.

`tests/broker/alpaca/clerk/sqlite/test_runtime_live_invariant.py`:

```python
"""A live sqlite facade cannot exist without its envelope and its arming gate (slice 7, R4)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0


def _facade(repo: ClerkSqliteRepository, **kwargs) -> SqliteAlpacaClerkFacade:
    broker = _LiveBroker(now_ms=T0)
    return SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, authority_kind="sqlite", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_mode": "live"},
        {"account_mode": "live", "live_envelope": LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False)},
        {"account_mode": "live", "live_arming": ArmingGate()},
    ],
    ids=["nothing", "envelope-only", "gate-only"],
)
def test_a_live_facade_refuses_to_exist_without_both_gates(envelope_repo: ClerkSqliteRepository, kwargs) -> None:
    with pytest.raises(AccountAuthorityIdentityError):
        _facade(envelope_repo, **kwargs)


def test_a_live_facade_with_both_gates_exposes_them_and_its_mode(envelope_repo: ClerkSqliteRepository) -> None:
    gate = ArmingGate()
    facade = _facade(
        envelope_repo,
        account_mode="live",
        live_envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False),
        live_arming=gate,
    )
    assert facade.live_arming is gate
    assert facade.account_mode == "live"


def test_a_paper_facade_is_untouched(envelope_repo: ClerkSqliteRepository) -> None:
    facade = _facade(envelope_repo, account_mode="paper")
    assert facade.live_arming is None
    assert facade.account_mode == "paper"
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py tests/broker/alpaca/clerk/sqlite/test_runtime_live_invariant.py -q -p no:cacheprovider`
Expected: FAIL — `unexpected keyword argument 'arming_gate'` / `'live_arming'`.

- [ ] **Step 3: The facade**

In `app/broker/alpaca/clerk/sqlite/runtime.py`:
- Add `from app.broker.alpaca.clerk.live_arming_gate import ArmingGate` beside the `LiveEnvelopeGate` import.
- `__init__` (line 219): after `live_envelope: LiveEnvelopeGate | None = None,` add `live_arming: ArmingGate | None = None,`.
- Replace the `else:` branch at lines 229–230 with:

```python
        else:
            require_real_account_id(repo.account_id)
            # ADR 0059 D11 (slice 7): a real-money facade is never a permissive
            # default. Both gates are composed by the live selector; a live
            # facade with either missing is a composition bug, refused here.
            if account_mode == "live" and (live_envelope is None or live_arming is None):
                raise AccountAuthorityIdentityError(
                    "a live sqlite authority requires a risk envelope and an arming gate"
                )
```

- After line 247 (`self._live_envelope = live_envelope`) add:

```python
        # ADR 0059 D11: per-instance arming, consulted at ENTER on the live
        # authority only; ``None`` on paper and under shadow.
        self._live_arming = live_arming
```

- After the `live_envelope` property (lines 278–281) add:

```python
    @property
    def live_arming(self) -> ArmingGate | None:
        """The per-instance arming gate this authority admits ENTERs against, if it has one."""
        return self._live_arming

    @property
    def account_mode(self) -> Literal["paper", "live"]:
        """The environment every custody answer names (ADR 0054), learned at activation."""
        return self._account_mode
```

- At the `accept_enter(...)` call (line 843), after `envelope=self._live_envelope,` add `arming=self._live_arming,`.

- [ ] **Step 4: The sync**

In `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`:
- Imports: extend the existing `collections.abc` import with `Mapping`; replace the bare `LiveArmingInvalid` import with `from app.broker.alpaca.clerk.live_arming import (LIVE_MODE_DISAGREEMENT, LIVE_VERDICT_TRANSITION_HALT, LiveArmingInvalid, latest_arming)`; add `from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot`; add `BrokerAccountModeDisagreement` to the `app.broker.contract.errors` import (the exception `adapter.py:192` raises).
- `EnvelopeSyncAction` (line 53) gains `"mode_disagreed"`; `_ACTION_MESSAGES` gains `"mode_disagreed": "live envelope read a broker mode that no longer agrees; every ENTER is refused"` and `_WARNING_ACTIONS` gains `"mode_disagreed"`.
- After `type Sleep = ...` (line 52) add:

```python
# The runner's sealed bindings, read per tick: ``strategy_instance_id`` -> the
# instance's current sealed-program hash. Injected as a callable so the clerk
# layer stays free of bot-registration imports (the ``roster_symbols`` pattern).
type InstanceSeals = Callable[[], Mapping[str, str]]
```

- `__init__`: after `arming_ledger: LiveArmingLedger | None = None,` add `arming_gate: ArmingGate | None = None,` and `instance_seals: InstanceSeals | None = None,`; after `self._arming_ledger = arming_ledger` add:

```python
        self._arming_gate = arming_gate
        self._instance_seals = instance_seals
        # The instances the previous tick judged armed, so an instance leaving
        # that set is warned about exactly once (ADR 0059 D8 as design R10).
        self._previously_armed: frozenset[str] = frozenset()
        # Set by a read the broker answered in the wrong mode (design R2); the
        # gate stays invalid under LIVE_MODE_DISAGREEMENT until a read agrees.
        self._mode_disagreed = False
```
- Replace `_refresh_sealed_envelope` (lines 265–324) with:

```python
    def _refresh_arming(self) -> ArmingSnapshot | None:
        """Re-read the account's arming ledger once; seal the envelope and refresh the gate.

        The envelope a live ENTER is admitted against is the one an operator
        sealed at arming (ADR 0059 D3/R10), and the instance it is admitted
        *for* must be armed right now (D11, slice 7). Both facts come from the
        same read, so one verdict can never describe two snapshots of a file an
        operator may be appending to.

        Read on every tick rather than once at composition because an arming
        is an out-of-process CLI act; one cadence is the whole latency between
        ``apply`` and the gate that enforces it.

        A ledger nobody can read seals nothing and admits nothing: the envelope
        returns to unsealed, the gate is invalidated with the fault, and the
        fault is logged at error level once per transition.
        """
        if self._arming_ledger is None:
            return None
        try:
            records = tuple(self._arming_ledger.records())
        except LiveArmingInvalid as exc:
            if not self._arming_ledger_invalid:
                logger.error(
                    "live arming ledger cannot be read; the envelope is unsealed and nothing is armed",
                    exc_info=True,
                    extra={
                        "action": "live_arming_ledger_invalid",
                        "account_id": self._repo.account_id,
                        "live_account_id": self._arming_ledger.live_account_id,
                        "path": str(self._arming_ledger.path),
                        "why": str(exc),
                    },
                )
            self._arming_ledger_invalid = True
            self._assign_sealed(None)
            if self._arming_gate is not None:
                self._arming_gate.invalidate(str(exc))
            return None
        self._arming_ledger_invalid = False
        newest = latest_arming(records)
        self._assign_sealed(None if newest is None else newest.envelope)
        if self._arming_gate is None:
            return None
        snapshot = ArmingSnapshot(
            observed_at_ms=self._repo.clock(),
            live_account_id=self._arming_ledger.live_account_id,
            records=records,
            seals=dict(self._instance_seals()) if self._instance_seals is not None else {},
            configured_envelope=self.envelope.values,
        )
        if self._mode_disagreed:
            # The ledger is fine; the account is not the one this authority
            # was composed for. Nothing is admitted until a read agrees (R2).
            self._arming_gate.invalidate(
                "the broker-reported mode no longer agrees with the configured mode",
                reason_code=LIVE_MODE_DISAGREEMENT,
            )
        else:
            self._arming_gate.publish(snapshot)
        self._note_transitions(snapshot)
        return snapshot

    def _note_transitions(self, snapshot: ArmingSnapshot) -> None:
        """Warn once, with the code, for every instance that was armed last tick and is not now (R10).

        This is the loud half of ADR 0059 D8. The quiet half — new submission
        stops — is the ENTER refusal every such instance now gets; no desired
        state is written, so the instance keeps managing its own position.
        """
        armed_now = snapshot.armed_instance_ids(snapshot.observed_at_ms)
        for strategy_instance_id in sorted(self._previously_armed - armed_now):
            status = snapshot.status_for(strategy_instance_id, now_ms=snapshot.observed_at_ms)
            logger.warning(
                "%s: a live instance is no longer armed; its ENTERs refuse until it is re-armed",
                LIVE_VERDICT_TRANSITION_HALT,
                extra={
                    "action": "live_verdict_transition_halt",
                    "reason_code": status.reason_code,
                    "state": status.state,
                    "strategy_instance_id": strategy_instance_id,
                    "live_account_id": snapshot.live_account_id,
                    "account_id": self._repo.account_id,
                },
            )
        self._previously_armed = armed_now

    def _assign_sealed(self, sealed: LiveEnvelopeValues | None) -> None:
        """Assign the sealed envelope, logging each transition once (slice 6 R10)."""
        if sealed == self.envelope.sealed:
            return
        self.envelope.sealed = sealed
        emit = logger.warning if self.envelope.agreement == "disagreed" else logger.info
        emit(
            "live envelope sealed by an arming record"
            if sealed is not None
            else "live envelope is no longer sealed by any arming record",
            extra={
                "action": "live_envelope_sealed" if sealed is not None else "live_envelope_unsealed",
                "account_id": self._repo.account_id,
                "live_account_id": self._arming_ledger.live_account_id if self._arming_ledger else None,
                "agreement": self.envelope.agreement,
            },
        )
```

(add `LiveEnvelopeValues` to the `live_envelope` import). In `tick()` replace the first statement `self._refresh_sealed_envelope()` with `self._refresh_arming()`, and replace the `try: reading = await self.observe() / except BrokerError as exc: return self._acted("read_failed", ...)` block with:

```python
        try:
            reading = await self.observe()
        except BrokerAccountModeDisagreement as exc:
            # Design R2: the account the read answered is not the one this
            # authority was composed for. Withdraw the observation (no ENTER
            # bounds against it) and hold the gate under the disagreement's
            # own code until a read agrees again; the next refresh does that.
            self._mode_disagreed = True
            self.envelope.withdraw()
            if self._arming_gate is not None:
                self._arming_gate.invalidate(exc.detail or str(exc), reason_code=LIVE_MODE_DISAGREEMENT)
            return self._acted("mode_disagreed", {"why": exc.detail or str(exc)})
        except BrokerError as exc:
            return self._acted("read_failed", {"why": str(exc)})
        self._mode_disagreed = False
```

Place the `BrokerAccountModeDisagreement` branch first whether or not it subclasses `BrokerError` — if it does, order decides which branch runs; if it does not, the branch is what stops a disagreement from becoming a swallowed tick failure. `LiveArmingLedger.records()` is the slice-6 reader `account_arming` already calls; `sealed_envelope()` stays for its other callers and is no longer called here.

- [ ] **Step 5: The composition seam**

In `app/broker/alpaca/clerk/active_runtime.py`:
- In the `TYPE_CHECKING` block add `from app.broker.alpaca.clerk.live_arming_gate import ArmingGate` and `from app.broker.alpaca.clerk.sqlite.live_envelope_sync import InstanceSeals` (the sync is already imported at runtime; the alias is type-only).
- `compose_repository_runtime` signature: after `arming_ledger: LiveArmingLedger | None = None,` add `arming_gate: ArmingGate | None = None,` and `instance_seals: InstanceSeals | None = None,`. Extend the docstring: "``arming_gate`` and ``instance_seals`` exist only on the live authority (ADR 0059 D11, slice 7): the gate the facade admits ENTERs against, and the runner's sealed bindings the sync reads beside the ledger every tick — injected as a callable, the ``roster_symbols`` pattern, so the clerk layer never learns the runner's root."
- Facade construction (line 305): after `live_envelope=live_envelope,` add `live_arming=arming_gate,`.
- `LiveEnvelopeSync(...)` (line 370): after `arming_ledger=arming_ledger,` add `arming_gate=arming_gate,` and `instance_seals=instance_seals,`.

- [ ] **Step 6: Run the tests and the slice-6 sync suite**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py tests/broker/alpaca/clerk/sqlite/test_runtime_live_invariant.py tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py tests/contracts/test_alpaca_active_authority_wiring.py -q -p no:cacheprovider`
Expected: PASS. If a slice-6 test calls `_refresh_sealed_envelope` by name, rename that call to `_refresh_arming` — same behaviour for a sync with no gate.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py PythonDataService/app/broker/alpaca/clerk/active_runtime.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime_live_invariant.py
git commit -m "feat(alpaca): the sync refreshes the arming gate each tick and says who left armed (ADR 0059 slice 7, R2, R4, R10)

One ledger read seals the envelope and publishes the per-instance snapshot;
an instance leaving armed is warned about once with its code; a broker mode
that stops agreeing holds the gate under LIVE_MODE_DISAGREEMENT; a live
sqlite facade refuses to exist without both gates.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The live authority — `select_live_clerk_runtime`, the selector fork, and the first admitted ENTER

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/live_authority.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py:44-47, 88-110, 136-148, 162, 547-570`
- Modify: `PythonDataService/app/main.py:230-260`
- Modify: `PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py` (append)
- Create: `PythonDataService/tests/broker/alpaca/clerk/live_authority_fixtures.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_live_authority_runtime.py` (new)

**Interfaces:**
- Consumes: `bind_real_alpaca_ports(account_mode=)` (Task 1); `ArmingGate`, `ArmingSnapshot` (Task 2); `compose_repository_runtime(arming_gate=, instance_seals=)`, facade `live_arming` (Task 3); `ActivationStore`, `ActivationRecord`, `ActivationRecordInvalid` (`sqlite/activation.py`); `DeveloperCleanSlateResetRegistry` (`sqlite/dev_reset.py`); `instance_seal_hashes` (`live_arming_ceremony.py`, slice 6); `SqliteTradeUpdateEvidenceSink` (`trade_evidence.py`).
- Produces (Tasks 6, 8, 10 rely on these):
  - `LIVE_CONTROL_UNAUTHENTICATED = "LIVE_CONTROL_UNAUTHENTICATED"` (in `live_authority.py`)
  - `select_live_clerk_runtime(*, account, activation, activation_store, read, trade, artifacts_root, repository_opener, startup_recovery_timeout_s, execution_lease_wait_timeout_s, execution_lease_retry_interval_s, stream_health_gate, roster_symbols, live_envelope_values, live_state_root: Callable[[], Path] | None, control_unauthenticated: bool) -> ActiveClerkRuntime`
  - `select_active_clerk_runtime(..., live_state_root: Callable[[], Path] | None = None, control_unauthenticated: bool = False)`
  - fixtures: `LIVE_SID = "ema-live-1"`, `live_activation(account_id=LIVE_ACCT) -> ActivationRecord`, `_RecordingLiveBroker` (a `_LiveBroker` whose `submit` records and answers an accepted order), `compose_live(tmp_path, broker, *, live_state_root, control_unauthenticated=False) -> ActiveClerkRuntime`

- [ ] **Step 1: The shared fixtures**

Create `tests/broker/alpaca/clerk/live_authority_fixtures.py`:

```python
"""One activated live account, one live-sealed binding, one recording trade port (ADR 0059 slice 7).

Not a conftest: imported by name from ``tests/broker/alpaca/clerk/``,
``tests/services/`` and ``tests/broker/v2panel/``, which share no conftest —
the ``live_arming_fixtures`` precedent. Extends those fixtures rather than
restating them, so every slice-7 test describes the same live account.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, select_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.test_active_authority import _ActivationStore

LIVE_SID = "ema-live-1"


def live_activation(*, account_id: str = LIVE_ACCT, authority_generation: int = 1, db_identity_token: str = "live-db") -> ActivationRecord:
    """The cutover's activation record for the live account — the second leg of Decision 1."""
    return ActivationRecord.create(
        account_id=account_id,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        broker_proof_reference="proof.json",
        broker_proof_sha256="0" * 64,
        legacy_quarantine_manifest="quarantine.json",
        legacy_quarantine_manifest_sha256="1" * 64,
        activated_at_ms=1,
    )


class _RecordingLiveBroker(_LiveBroker):
    """The live account whose trade port *is* reached: it records every submit and answers accepted."""

    def __init__(self, *, now_ms: int, **kwargs: Any) -> None:
        super().__init__(now_ms=now_ms, **kwargs)
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def submit(self, *args: Any, **kwargs: Any) -> BrokerOrder:
        self.submissions.append((args, kwargs))
        client_order_id = kwargs.get("client_order_id") or next(
            (value for value in args if isinstance(value, str)), "unknown"
        )
        leg = kwargs.get("leg") or next((value for value in args if hasattr(value, "quantity")), None)
        return BrokerOrder(
            broker="alpaca",
            order_id=f"live-order-{len(self.submissions)}",
            client_order_id=client_order_id,
            symbol=getattr(leg, "symbol", "SPY"),
            asset_class="us_equity",
            side=str(getattr(leg, "side", "buy")),
            order_type="market",
            time_in_force="day",
            quantity=float(getattr(leg, "quantity", 1)),
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=self.now_ms,
            created_at_ms=self.now_ms,
            updated_at_ms=self.now_ms,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=self.now_ms,
        )


def pinned_repository(now_ms: int):
    def _open(account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
        return ClerkSqliteRepository.open(account_id=account_id, artifacts_root=artifacts_root, clock=lambda: now_ms)

    return _open


async def compose_live(
    tmp_path: Path,
    broker: _LiveBroker,
    *,
    now_ms: int,
    live_state_root: Path,
    control_unauthenticated: bool = False,
    live_envelope_values=TEST_ENVELOPE_VALUES,
) -> ActiveClerkRuntime:
    """Initialize the live custody database, activate it, and run the real selector."""
    repository = ClerkSqliteRepository.initialize(account_id=LIVE_ACCT, artifacts_root=tmp_path, clock=lambda: now_ms)
    meta = repository.control_meta_snapshot()
    repository.close()
    activation = live_activation(
        authority_generation=meta.authority_generation, db_identity_token=meta.db_identity_token
    )
    return await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        activation_store=_ActivationStore(activation),
        repository_opener=pinned_repository(now_ms),
        live_envelope_values=live_envelope_values,
        live_state_root=lambda: live_state_root,
        control_unauthenticated=control_unauthenticated,
    )


__all__ = ["LIVE_SID", "_RecordingLiveBroker", "compose_live", "live_activation", "pinned_repository"]
```

If `BrokerOrder` requires a field this constructor omits, copy the field list from `tests/broker/alpaca/clerk/sqlite/conftest.py::_broker_order_fixture` (lines 90–124), which builds the same model.

- [ ] **Step 2: Write the failing composed-runtime tests**

`tests/broker/alpaca/clerk/test_live_authority_runtime.py`:

```python
"""The real-money Live Account Authority, composed by the real selector (ADR 0059 D1, D11 — slice 7).

Every test runs ``select_active_clerk_runtime`` against a live broker double:
the wiring under test is composition — which authority a live boot selects
once an activation exists, which ports it binds, which gates it installs, and
whether an armed instance's ENTER reaches the real trade port through all
three admissions. No wall clock: the repository is pinned to ``NOW_MS``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import live_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_shadow_clerk_authority,
    primary_custody_world,
    reset_alpaca_clerk_for_testing,
    select_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_authority import LIVE_CONTROL_UNAUTHENTICATED, select_live_clerk_runtime
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from tests.broker.alpaca.clerk.live_arming_fixtures import record_sealed_binding
from tests.broker.alpaca.clerk.live_authority_fixtures import (
    LIVE_SID,
    _RecordingLiveBroker,
    compose_live,
    live_activation,
    pinned_repository,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import RUN_ID, _binding
from tests.broker.alpaca.clerk.test_active_authority import _ActivationStore
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, _retain
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import BAR_CLOSE, DECISION_MINUTE, NOW_MS


@pytest.fixture(autouse=True)
def _no_primary() -> AsyncIterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


@pytest.fixture()
def live_state_root(tmp_path: Path) -> Path:
    return tmp_path / "runner"


@pytest.fixture()
async def live_runtime(tmp_path: Path, live_state_root: Path) -> AsyncIterator[tuple[ActiveClerkRuntime, _RecordingLiveBroker]]:
    broker = _RecordingLiveBroker(now_ms=NOW_MS)
    runtime = await compose_live(tmp_path, broker, now_ms=NOW_MS, live_state_root=live_state_root)
    assert runtime.authority_kind == "sqlite", runtime.startup_failure
    try:
        yield runtime, broker
    finally:
        await runtime.close()


@pytest.fixture()
async def registered_live_bot(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker], tmp_path: Path, live_state_root: Path
) -> AsyncIterator[tuple[RetainedSourceBar, str]]:
    """One instance registered with the live Clerk, sealed on disk on the live id, and its decision bar."""
    runtime, _broker = live_runtime
    assert runtime.clerk is not None
    seal = record_sealed_binding(live_state_root, strategy_instance_id=LIVE_SID, sealed_account_id=LIVE_ACCT)
    await runtime.clerk.register_strategy_run(
        _binding(use_rth=True).model_copy(update={"strategy_instance_id": LIVE_SID, "sealed_account_id": LIVE_ACCT})
    )
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=live_evidence_account_id_for_strategy(LIVE_SID))
    try:
        yield _retain(bars, minute=DECISION_MINUTE, close=BAR_CLOSE), seal.bot_configuration_hash
    finally:
        bars.close()


def _arm(tmp_path: Path, seal_hash: str, *, configured_signal_hash: str = "b" * 64) -> LiveArmingRecord:
    record = LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=LIVE_SID,
        seal_hash=seal_hash,
        configured_signal_hash=configured_signal_hash,
        shadow_receipt_sha256="e" * 64,
        envelope=TEST_ENVELOPE_VALUES,
        armed_at_ms=NOW_MS - 60_000,
        max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
    )
    LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).append(record)
    return record


async def _enter(runtime: ActiveClerkRuntime, bar: RetainedSourceBar, *, decision_id: str = "d1") -> Any:
    assert runtime.clerk is not None
    binding = _binding(use_rth=True)
    return await runtime.clerk.execute_for_instance(
        strategy_instance_id=LIVE_SID,
        run_id=RUN_ID,
        decision_id=decision_id,
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=1,
        use_rth=True,
        retained_source_bar=bar,
    )


async def test_a_live_account_with_no_activation_still_boots_the_shadow_authority(tmp_path: Path) -> None:
    """R1: absent the cutover's record, every live boot is exactly what slice 4 built."""
    await activate_shadow_clerk_authority(live_account_id=LIVE_ACCT, artifacts_root=tmp_path)
    runtime = await select_active_clerk_runtime(
        read=_LiveBroker(now_ms=NOW_MS),
        trade=_LiveBroker(now_ms=NOW_MS),
        artifacts_root=tmp_path,
        repository_opener=pinned_repository(NOW_MS),
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )
    try:
        assert runtime.authority_kind == "shadow", runtime.startup_failure
    finally:
        await runtime.close()


async def test_an_activated_live_account_boots_the_real_live_authority_with_both_gates(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
) -> None:
    """R1, R4: the same sqlite Clerk over a real account, in the real_live world, with the real trade port."""
    runtime, _broker = live_runtime
    assert runtime.selected_account_authority_kind == "real_live"
    assert runtime.selected_account_id == LIVE_ACCT
    assert runtime.clerk is not None and runtime.envelope_sync is not None
    assert runtime.clerk.account_mode == "live"
    assert runtime.clerk.live_envelope is runtime.envelope_sync.envelope
    assert runtime.clerk.live_envelope.custody_is_simulated is False
    assert runtime.clerk.live_arming is not None
    assert isinstance(runtime.evidence_sink, SqliteTradeUpdateEvidenceSink)
    set_active_clerk_runtime(runtime)
    assert primary_custody_world() == "real_live"


async def test_an_open_control_plane_installs_no_live_authority(tmp_path: Path, live_state_root: Path) -> None:
    """R14: DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=true and a live account never meet."""
    runtime = await compose_live(
        tmp_path, _LiveBroker(now_ms=NOW_MS), now_ms=NOW_MS, live_state_root=live_state_root, control_unauthenticated=True
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == LIVE_CONTROL_UNAUTHENTICATED
    assert "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL" in runtime.startup_failure.recovery


async def test_a_live_authority_without_envelope_values_is_unavailable(tmp_path: Path, live_state_root: Path) -> None:
    runtime = await compose_live(
        tmp_path, _LiveBroker(now_ms=NOW_MS), now_ms=NOW_MS, live_state_root=live_state_root, live_envelope_values=None
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "LIVE_ENVELOPE_MISSING"


async def test_an_activation_naming_another_account_is_a_mode_disagreement(tmp_path: Path) -> None:
    """R2: the activation leg is looked up by the observed id; it can never grant a different one."""
    broker = _LiveBroker(now_ms=NOW_MS)
    activation = live_activation(account_id="9OTHER0002")
    runtime = await select_live_clerk_runtime(
        account=await broker.get_account(),
        activation=activation,
        activation_store=_ActivationStore(activation),
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        repository_opener=pinned_repository(NOW_MS),
        startup_recovery_timeout_s=5.0,
        execution_lease_wait_timeout_s=0.0,
        execution_lease_retry_interval_s=0.1,
        stream_health_gate=None,
        roster_symbols=None,
        live_envelope_values=TEST_ENVELOPE_VALUES,
        live_state_root=None,
        control_unauthenticated=False,
    )
    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "LIVE_MODE_DISAGREEMENT"


async def test_before_the_first_tick_every_live_enter_is_unobserved(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker], registered_live_bot: tuple[RetainedSourceBar, str]
) -> None:
    runtime, broker = live_runtime
    bar, _seal = registered_live_bot
    receipt = await _enter(runtime, bar)
    assert receipt.state == "rejected"
    assert receipt.reason_code == LIVE_ARMING_UNOBSERVED
    assert broker.submissions == []


async def test_an_unarmed_live_instance_is_refused_required_after_the_tick(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker], registered_live_bot: tuple[RetainedSourceBar, str]
) -> None:
    runtime, broker = live_runtime
    bar, _seal = registered_live_bot
    assert runtime.envelope_sync is not None
    await runtime.envelope_sync.tick()
    receipt = await _enter(runtime, bar)
    assert receipt.state == "rejected"
    assert receipt.reason_code == LIVE_ARMING_REQUIRED
    assert broker.submissions == []


async def test_an_armed_live_instances_enter_passes_all_three_gates_and_reaches_the_real_trade_port(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    registered_live_bot: tuple[RetainedSourceBar, str],
    tmp_path: Path,
) -> None:
    """Consequence 7: the slice after which a real order is possible — and this is that order."""
    runtime, broker = live_runtime
    bar, seal_hash = registered_live_bot
    _arm(tmp_path, seal_hash)
    assert runtime.envelope_sync is not None and runtime.sqlite_repository is not None
    await runtime.envelope_sync.tick()

    receipt = await _enter(runtime, bar)

    assert receipt.state != "rejected", receipt
    assert runtime.sqlite_repository.reserved_cash_usd(observed_at_ms=NOW_MS) > 0
    assert len(broker.submissions) == 1


async def test_a_changed_seal_disarms_the_live_instance_at_admission(
    live_runtime: tuple[ActiveClerkRuntime, _RecordingLiveBroker],
    registered_live_bot: tuple[RetainedSourceBar, str],
    tmp_path: Path,
) -> None:
    runtime, broker = live_runtime
    bar, _seal = registered_live_bot
    _arm(tmp_path, "f" * 64)
    assert runtime.envelope_sync is not None
    await runtime.envelope_sync.tick()
    receipt = await _enter(runtime, bar)
    assert receipt.state == "rejected"
    assert receipt.reason_code == "LIVE_ARMING_SEAL_CHANGED"
    assert broker.submissions == []
```

If `_binding(...)` in `test_runtime_program_leg.py` fixes a `strategy_instance_id` that `validate_strategy_instance_id` accepts but `record_sealed_binding` does not, or vice versa, use `LIVE_SID` on both sides as written above — the two must agree, and `record_sealed_binding` already accepts any valid instance id.

- [ ] **Step 3: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_authority_runtime.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: app.broker.alpaca.clerk.live_authority`.

- [ ] **Step 4: The live authority's boot story**

Create `app/broker/alpaca/clerk/live_authority.py`:

```python
"""The real-money Live Account Authority's boot story (ADR 0059 D1, D11 — slice 7).

A live account gets a mutating Clerk only on three-way mode agreement: the
configured mode (which the adapter already derived the observed
``account_mode`` from), the broker-observed account, and the cutover's
activation record naming that exact account. It is the same ``sqlite`` Clerk
the paper account runs, composed with the two gates a real-money ENTER is
admitted against — the risk envelope (D4) and the per-instance arming gate
(D3/D11) — and the real trade port. Every refusal is a typed ``unavailable``
runtime; a live boot never aborts the data plane (#2014).

Cold start is the paper path's (design R18): the cutover's flat-and-order-
free evidence at graduation and ``recover()`` at every boot. The shadow
namespace scan is not applied here — after the first real order it would
refuse every boot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    bind_real_alpaca_ports,
)
from app.broker.alpaca.clerk.active_runtime import (
    ActiveClerkRuntime,
    compose_repository_runtime,
    unavailable_runtime,
)
from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_MISSING,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecord,
    ActivationRecordInvalid,
    ActivationStore,
)
from app.broker.alpaca.clerk.sqlite.dev_reset import DeveloperCleanSlateResetRegistry
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

logger = logging.getLogger(__name__)

# ADR 0059 D10 by extension (design R14): a real-money authority never
# installs behind an open data-plane control surface.
LIVE_CONTROL_UNAUTHENTICATED = "LIVE_CONTROL_UNAUTHENTICATED"


def _instance_seals_reader(
    *, live_account_id: str, live_state_root: Callable[[], Path] | None
) -> Callable[[], Mapping[str, str]]:
    """The runner's sealed bindings on this account, read per tick by the sync.

    Injected as a callable so the clerk layer never learns the runner's root
    (the ``roster_symbols`` pattern). No root means no seals, which means no
    instance is armed — fail closed, never a default.
    """

    def _seals() -> Mapping[str, str]:
        if live_state_root is None:
            return {}
        # Local import: the ceremony imports the runner's binding repository,
        # which this boot-time module must not pull in at import.
        from app.broker.alpaca.clerk.live_arming_ceremony import instance_seal_hashes

        return {
            sid: seal.seal_hash
            for sid, seal in instance_seal_hashes(
                live_account_id=live_account_id, live_state_root=live_state_root()
            ).items()
        }

    return _seals


async def select_live_clerk_runtime(
    *,
    account: BrokerAccountSnapshot,
    activation: ActivationRecord,
    activation_store: ActivationStore,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    artifacts_root: Path,
    repository_opener: Callable[[str, Path], ClerkSqliteRepository],
    startup_recovery_timeout_s: float,
    execution_lease_wait_timeout_s: float,
    execution_lease_retry_interval_s: float,
    stream_health_gate: StreamHealthGate | None,
    roster_symbols: Callable[[], Sequence[str]] | None,
    live_envelope_values: LiveEnvelopeValues | None,
    live_state_root: Callable[[], Path] | None,
    control_unauthenticated: bool,
) -> ActiveClerkRuntime:
    """Compose the real-money authority for an activated live account (ADR 0059 D1/D11)."""
    if control_unauthenticated:
        return unavailable_runtime(
            LIVE_CONTROL_UNAUTHENTICATED,
            account_id=account.account_id,
            recovery=(
                "Set DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=false; a real-money authority "
                "never installs behind an open control plane (ADR 0059 D10)."
            ),
        )
    if account.account_mode != "live" or activation.account_id != account.account_id:
        return unavailable_runtime(
            LIVE_MODE_DISAGREEMENT,
            account_id=account.account_id,
            recovery=(
                "The observed account, the configured mode and the live activation record "
                "must name one live account (ADR 0059 D1)."
            ),
        )
    if live_envelope_values is None:
        return unavailable_runtime(
            LIVE_ENVELOPE_MISSING,
            account_id=account.account_id,
            recovery="Set every ALPACA_LIVE_* value; a live authority admits nothing without its envelope (ADR 0059 D4).",
        )
    try:
        ports = bind_real_alpaca_ports(
            account_id=account.account_id, read=read, trade=trade, account_mode="live"
        )
    except AccountAuthorityIdentityError as exc:
        return unavailable_runtime(
            "REAL_PORT_REJECTED_SYNTHETIC_ACCOUNT", account_id=account.account_id, recovery=str(exc)
        )
    alpaca_accounts_root = artifacts_root / "accounts" / "alpaca"
    if DeveloperCleanSlateResetRegistry(alpaca_accounts_root).authorizes_reinitialize(
        account_id=account.account_id,
        prior_authority_generation=activation.authority_generation,
        artifacts_root=artifacts_root,
    ):
        return unavailable_runtime(
            "DEVELOPER_RESET_REACTIVATION_REQUIRED",
            account_id=account.account_id,
            recovery=(
                "This activated authority was moved aside by a developer clean-slate reset. "
                "Regenerate it, then complete a new live cutover before startup."
            ),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )

    def _verify_live_activation(meta: ControlMetaSnapshot) -> None:
        if (
            activation_store.resolve(
                account.account_id, meta.authority_generation, meta.db_identity_token, artifacts_root
            )
            is None
        ):
            raise ActivationRecordInvalid("activation record disappeared during SQLite startup")

    try:
        composed = await compose_repository_runtime(
            ports=ports,
            authority_kind="sqlite",
            account_mode="live",
            artifacts_root=artifacts_root,
            verify_activation=_verify_live_activation,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
            # Real custody: the broker's cash already reflects this Clerk's
            # own fills, so the envelope subtracts nothing (ADR 0059 D4).
            live_envelope=LiveEnvelopeGate(values=live_envelope_values, custody_is_simulated=False),
            arming_ledger=LiveArmingLedger(artifacts_root, live_account_id=account.account_id),
            arming_gate=ArmingGate(),
            instance_seals=_instance_seals_reader(
                live_account_id=account.account_id, live_state_root=live_state_root
            ),
        )
    except Exception as exc:
        logger.warning(
            "Live Alpaca Clerk failed startup; no authority installed",
            extra={"action": "live_active_clerk_startup_failed", "account_id": account.account_id},
            exc_info=True,
        )
        return unavailable_runtime(
            "ACTIVATION_RECORD_INVALID" if isinstance(exc, ActivationRecordInvalid) else "SQLITE_CLERK_STARTUP_FAILED",
            account_id=account.account_id,
            recovery=str(exc),
            activation_detected=True,
            authority_generation=activation.authority_generation,
            db_identity_token=activation.db_identity_token,
        )
    logger.warning(
        "REAL-MONEY Alpaca authority installed; ENTERs admit only for armed instances",
        extra={"action": "live_authority_installed", "account_id": account.account_id},
    )
    return ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=composed.facade,
        sweep=composed.sweep,
        hold_sync=composed.hold_sync,
        envelope_sync=composed.envelope_sync,
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=composed.repository,
            intake=composed.facade.intake,
            reconciler=composed.facade,
        ),
        _sqlite_repository=composed.repository,
        account_id=account.account_id,
        account_authority_kind="real_live",
    )


__all__ = ["LIVE_CONTROL_UNAUTHENTICATED", "select_live_clerk_runtime"]
```

- [ ] **Step 5: The selector fork**

In `app/broker/alpaca/clerk/active_authority.py`:
- Beside the `shadow_authority` import (lines 44–47) add `from app.broker.alpaca.clerk.live_authority import select_live_clerk_runtime`.
- `select_active_clerk_runtime`'s signature (after `live_envelope_values: LiveEnvelopeValues | None = None,` at line 100) gains:

```python
    live_state_root: Callable[[], Path] | None = None,
    control_unauthenticated: bool = False,
```

and its docstring gains: "``live_state_root`` resolves the runner's bindings root lazily — the live authority's arming gate reads sealed bindings beside the ledger every tick (slice 7). ``control_unauthenticated`` is the data plane's open-control flag; a live account refuses to install behind it (design R14). Both are inert on a paper boot."

- Replace lines 136–148 (the `if account.account_mode == "live":` block) with:

```python
    store = activation_store or ActivationStore(artifacts_root / "accounts" / "alpaca")
    if account.account_mode == "live":
        # ADR 0059 D1 / slice 7 R1: graduation is a boot-time selection. The
        # cutover's activation record for this exact account is the second
        # leg of mode agreement; present, the real-money authority is
        # composed; absent, the account is shadowed exactly as slice 4 built.
        try:
            live_activation = store.latest(account.account_id)
        except ActivationRecordInvalid as exc:
            return unavailable_runtime(
                "ACTIVATION_RECORD_INVALID",
                account_id=account.account_id,
                recovery=str(exc),
                activation_detected=True,
            )
        if live_activation is None:
            return await select_shadow_clerk_runtime(
                account=account,
                read=read,
                artifacts_root=artifacts_root,
                repository_opener=repository_opener,
                startup_recovery_timeout_s=startup_recovery_timeout_s,
                execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
                execution_lease_retry_interval_s=execution_lease_retry_interval_s,
                stream_health_gate=stream_health_gate,
                roster_symbols=roster_symbols,
                live_envelope_values=live_envelope_values,
            )
        return await select_live_clerk_runtime(
            account=account,
            activation=live_activation,
            activation_store=store,
            read=read,
            trade=trade,
            artifacts_root=artifacts_root,
            repository_opener=repository_opener,
            startup_recovery_timeout_s=startup_recovery_timeout_s,
            execution_lease_wait_timeout_s=execution_lease_wait_timeout_s,
            execution_lease_retry_interval_s=execution_lease_retry_interval_s,
            stream_health_gate=stream_health_gate,
            roster_symbols=roster_symbols,
            live_envelope_values=live_envelope_values,
            live_state_root=live_state_root,
            control_unauthenticated=control_unauthenticated,
        )
```

and delete the now-duplicate `store = activation_store or ActivationStore(...)` at line 162 (the paper path uses the `store` built above). Add `"select_live_clerk_runtime"` to `__all__`.

- [ ] **Step 6: Wire `main.py`**

In `app/main.py`, at the `select_active_clerk_runtime(...)` call (lines 252–259), after `live_envelope_values=live_envelope_values,` add:

```python
            # ADR 0059 slice 7: the live authority reads sealed bindings beside
            # the arming ledger every tick (lazily, through the runner's root),
            # and refuses to install behind an open control plane (R14).
            live_state_root=live_artifacts_root,
            control_unauthenticated=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL,
```

`live_artifacts_root` is already imported in that block (line 230); `settings` is `app.config.settings`, already used by `main.py` (line 750).

- [ ] **Step 7: The contract pins**

Append to `tests/contracts/test_alpaca_active_authority_wiring.py`:

```python
def test_the_live_composition_binds_the_real_trade_port_and_both_gates() -> None:
    """ADR 0059 D11 (slice 7), pinned structurally because the failures are missing calls.

    Without ``account_mode="live"`` the ports bind as ``real_paper``; without
    ``custody_is_simulated=False`` the envelope subtracts the Clerk's own fills
    twice; without the arming gate and the seals reader nothing is ever armed.
    """
    live_source = (APPLICATION_ROOT / "broker/alpaca/clerk/live_authority.py").read_text(encoding="utf-8")
    selector_source = (APPLICATION_ROOT / "broker/alpaca/clerk/active_authority.py").read_text(encoding="utf-8")
    runtime_source = (APPLICATION_ROOT / "broker/alpaca/clerk/active_runtime.py").read_text(encoding="utf-8")
    main_source = (APPLICATION_ROOT / "main.py").read_text(encoding="utf-8")

    assert 'account_mode="live"' in live_source and "bind_real_alpaca_ports(" in live_source
    assert "custody_is_simulated=False" in live_source
    assert "arming_gate=ArmingGate()," in live_source
    assert "arming_ledger=LiveArmingLedger(artifacts_root, live_account_id=account.account_id)," in live_source
    assert "verify_shadow_namespace_empty" not in live_source, "R18: the live authority inherits the paper path's cold start"
    assert "select_live_clerk_runtime(" in selector_source and "store.latest(account.account_id)" in selector_source
    assert "live_arming=arming_gate," in runtime_source and "instance_seals=instance_seals," in runtime_source
    assert "live_state_root=live_artifacts_root," in main_source
    assert "control_unauthenticated=settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL," in main_source
```

The two existing shadow pins (lines 405–441) read `shadow_authority.py` and `active_runtime.py` by source string; both strings they assert are unchanged by this task, so they keep passing — run them to prove it.

- [ ] **Step 8: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_authority_runtime.py tests/broker/alpaca/clerk/test_active_authority.py tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py tests/contracts/test_alpaca_active_authority_wiring.py tests/broker/v2panel/test_shadow_operator_surfaces.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main"`
Expected: PASS, and the import smoke prints nothing.

- [ ] **Step 9: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/live_authority.py PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/app/main.py PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py PythonDataService/tests/broker/alpaca/clerk/live_authority_fixtures.py PythonDataService/tests/broker/alpaca/clerk/test_live_authority_runtime.py
git commit -m "feat(alpaca): the real-money Live Account Authority (ADR 0059 slice 7, R1, R2, R4, R14, R18)

An activated live account boots the sqlite Clerk over the real trade port
with the envelope and the arming gate; without the activation it is shadowed
as before. An armed instance's ENTER passes admission, arming and the
envelope and reaches the real port — the first real order is possible.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The cutover admits live evidence — graduation (R3)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/cutover.py:128-135, 198-227, 255-299, 433-511, 514-580, 680-707`
- Modify: `PythonDataService/scripts/manage_alpaca_sqlite_clerk.py:255-276`
- Test: `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_live.py` (new); `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover.py` (one parametrized happy path); `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py` (one case)

**Interfaces:**
- Consumes: `ShadowReceiptStore.any_for_account` (slice 4); `LIVE_SHADOW_INCOMPLETE` (slice 6); `seal_receipt` (`live_arming_fixtures`).
- Produces: `BrokerCutoverEvidence.account_mode: Literal["paper", "live"]`; `_live_evidence_shadowed(*, artifacts_root, broker_evidence) -> bool` (cutover.py; refuses `CutoverRefused` prefixed `LIVE_SHADOW_INCOMPLETE:` when live and unshadowed, returns `False` for paper).

- [ ] **Step 1: Write the failing tests**

`tests/broker/alpaca/clerk/sqlite/test_cutover_live.py`:

```python
"""A live account graduates through the paper cutover ceremony, widened (ADR 0059 D1/D11, slice 7 R3).

The ceremony is offline and its evidence file is hand-authored, so the only
mode facts it can prove are the evidence's own word and — for ``live`` — that
the account already holds a shadow receipt, which stands in for the legacy
artifacts a never-legacy live account cannot have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverRefused,
    initialize_cutover_authority,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.live_arming_fixtures import seal_receipt
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

NOW = 1_800_000_000_000
MAX_AGE_MS = 600_000


def _evidence(*, account_id: str = LIVE_ACCT, account_mode: str = "live", positions=None, open_order_ids=()) -> BrokerCutoverEvidence:
    return BrokerCutoverEvidence(
        account_id=account_id,
        account_mode=account_mode,
        observed_at_ms=NOW - 1_000,
        proof_reference="alpaca-account-read:2026-09-09",
        positions=positions or {},
        open_order_ids=tuple(open_order_ids),
    )


def _initialize(tmp_path: Path, evidence: BrokerCutoverEvidence):
    return initialize_cutover_authority(
        account_id=evidence.account_id,
        artifacts_root=tmp_path,
        runner_artifacts_root=tmp_path / "runner",
        broker_evidence=evidence,
        max_broker_evidence_age_ms=MAX_AGE_MS,
        clock=lambda: NOW,
    )


def test_a_never_legacy_live_account_with_a_shadow_receipt_initializes(tmp_path: Path) -> None:
    seal_receipt(tmp_path, configured_signal_hash="b" * 64, live_account_id=LIVE_ACCT)
    receipt = _initialize(tmp_path, _evidence())
    assert receipt.account_id == LIVE_ACCT
    assert receipt.broker_evidence.account_mode == "live"
    assert receipt.legacy_artifacts == ()
    assert receipt.runner_roster == ()
    ClerkSqliteRepository.open(account_id=LIVE_ACCT, artifacts_root=tmp_path, clock=lambda: NOW).close()


def test_a_never_legacy_paper_account_still_refuses(tmp_path: Path) -> None:
    with pytest.raises(CutoverRefused, match="no legacy authority artifacts"):
        _initialize(tmp_path, _evidence(account_id="PA-NEVER", account_mode="paper"))


def test_a_live_account_nobody_shadowed_refuses_shadow_incomplete(tmp_path: Path) -> None:
    with pytest.raises(CutoverRefused, match="LIVE_SHADOW_INCOMPLETE"):
        _initialize(tmp_path, _evidence())


def test_a_receipt_for_another_account_does_not_graduate_this_one(tmp_path: Path) -> None:
    seal_receipt(tmp_path, configured_signal_hash="b" * 64, live_account_id="9OTHER0002")
    with pytest.raises(CutoverRefused, match="LIVE_SHADOW_INCOMPLETE"):
        _initialize(tmp_path, _evidence())


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"positions": {"SPY": 3.0}}, "broker-flat"),
        ({"open_order_ids": ("o-1",)}, "no open broker orders"),
    ],
)
def test_a_live_account_must_be_flat_and_order_free_to_graduate(tmp_path: Path, kwargs, match: str) -> None:
    """R3 / owner question E2: the shadow authority never submitted, so any position is a human's."""
    seal_receipt(tmp_path, configured_signal_hash="b" * 64, live_account_id=LIVE_ACCT)
    with pytest.raises(CutoverRefused, match=match):
        _initialize(tmp_path, _evidence(**kwargs))


def test_an_unknown_mode_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(CutoverRefused, match="paper or live"):
        _initialize(tmp_path, _evidence(account_mode="sandbox"))
```

In `tests/broker/alpaca/clerk/sqlite/test_cutover.py`, find the happy-path test that builds the legacy tree and runs `initialize_cutover_authority` → `plan_cutover` → `apply_cutover` for a paper account (it asserts the activation record is written), and parametrize it over `account_mode` in `("paper", "live")`: the `live` variant uses a live-shaped account id (`"9LIVE0001"`), calls `seal_receipt(tmp_path, configured_signal_hash="b" * 64, live_account_id="9LIVE0001")` first, passes `account_mode="live"` in every `BrokerCutoverEvidence` the test constructs, and asserts the same activation outcome. Do not change the paper variant's bytes; the parametrization must leave the existing assertions untouched.

In `tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py`, add one case beside the existing evidence-reading tests: an evidence file with `"account_mode": "live"` is refused with `ValueError` matching `ALPACA_MODE` when the CLI runs with paper settings (monkeypatch `scripts.manage_alpaca_sqlite_clerk.get_alpaca_settings` to return `AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")`), and is read when it returns the `live_settings()` fixture from `live_arming_fixtures`.

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_cutover_live.py -q -p no:cacheprovider`
Expected: FAIL — the live cases refuse `cutover requires broker evidence from a paper account`; the paper case passes already.

- [ ] **Step 3: Widen the evidence and add the live rule**

In `app/broker/alpaca/clerk/sqlite/cutover.py`:
- Line 131: `account_mode: Literal["paper", "live"]`.
- Lines 689–690: replace with

```python
    if evidence.account_mode not in ("paper", "live"):
        raise CutoverRefused("cutover requires broker evidence naming a paper or live account mode")
```

- After `_normalize_broker_evidence` add:

```python
def _live_evidence_shadowed(*, artifacts_root: Path, broker_evidence: BrokerCutoverEvidence) -> bool:
    """Whether live evidence may stand in for legacy artifacts (ADR 0059 slice 7, design R3).

    A live account has no legacy JSONL authority to quarantine and no prior
    activation to have reset; what it has, if anything, is the shadow gate's
    receipt. That receipt is the one proof this offline ceremony can ask
    for, so it is required — and for a paper account nothing changes.
    """
    if broker_evidence.account_mode != "live":
        return False
    # Local imports: the receipt store lives beside the arming ledger, above
    # the sqlite package; this module must not pull either in at import.
    from app.broker.alpaca.clerk.live_arming import LIVE_SHADOW_INCOMPLETE
    from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore

    if not ShadowReceiptStore(artifacts_root).any_for_account(broker_evidence.account_id):
        raise CutoverRefused(
            f"{LIVE_SHADOW_INCOMPLETE}: a live account may be activated only after at least "
            "one instance completed the shadow gate on it (ADR 0059 D2)"
        )
    return True
```

- In `initialize_cutover_authority` (line 222) compute `live_shadowed = _live_evidence_shadowed(artifacts_root=artifacts_root, broker_evidence=normalized)` right after normalizing and pass it into `_initialize_cutover_authority_locked` as a new keyword `live_shadowed: bool`; inside, replace both `allow_empty=reset_authorized` (lines 269 and 278) with `allow_empty=reset_authorized or live_shadowed`.
- In `plan_cutover` (line 468) and `apply_cutover` (line 550), right after `normalized_broker = _normalize_broker_evidence(broker_evidence)`, add `live_shadowed = _live_evidence_shadowed(artifacts_root=artifacts_root, broker_evidence=normalized_broker)`; then in each of the following `_legacy_artifact_evidence(...)` and `_runner_roster_evidence(...)` calls, change `allow_empty=_developer_reset_replaces_activation(...)` to `allow_empty=live_shadowed or _developer_reset_replaces_activation(...)` (four sites: two per function).

`_validate_cutover_broker_state` (lines 729–747) is deliberately unchanged: a live account graduates flat and order-free.

- [ ] **Step 4: The CLI's configured-mode check**

In `scripts/manage_alpaca_sqlite_clerk.py::_read_cutover_evidence`, after the account-id check (line 268) add:

```python
    if payload.get("account_mode") == "live" and get_alpaca_settings().mode != "live":
        raise ValueError(
            "broker evidence names a live account but ALPACA_MODE is not live; the ceremony "
            "runs only under the mode it activates (ADR 0059 D1)"
        )
```

A paper evidence file never reads settings, so every existing paper CLI test runs exactly as before.

- [ ] **Step 5: Run the cutover suites**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_cutover_live.py tests/broker/alpaca/clerk/sqlite/test_cutover.py tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py -q -p no:cacheprovider`
Expected: PASS; the paper tests that existed before this task are byte-for-byte unchanged apart from the one parametrization.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/sqlite/cutover.py PythonDataService/scripts/manage_alpaca_sqlite_clerk.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_live.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover.py PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py
git commit -m "feat(alpaca): the cutover graduates a shadowed live account (ADR 0059 slice 7, R3)

Live evidence is admitted; a shadow receipt stands in for the legacy
artifacts a never-legacy live account cannot have; the account must be flat
and order-free; the CLI refuses live evidence under a non-live ALPACA_MODE.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Start and Resume admission read arming — the fact, the `UNREADABLE` gate, the not-armed note (R6)

**Files:**
- Modify: `PythonDataService/app/schemas/run_admission.py:58-65, 234-256, 288-317`
- Modify: `PythonDataService/app/services/run_admission.py:9-40, 227-241, 473-491`
- Create: `PythonDataService/app/services/live_arming_admission.py`
- Modify: `PythonDataService/app/services/bot_start_admission.py:445-471, 495-577`
- Modify: `PythonDataService/app/services/bot_resume_admission.py:105-153` and its `_decision` (the `ResumeRunFacts(...)` construction)
- Modify: `PythonDataService/app/services/bot_runner.py` (the two constructor calls `BotStartAdmission(...)` / `BotResumeAdmission(...)` inside `BotTaskRegistry.__init__`, and one new constructor keyword)
- Test: `PythonDataService/tests/services/test_run_admission.py` (append), `PythonDataService/tests/services/test_live_arming_admission.py` (new)

**Interfaces:**
- Consumes: `account_arming` (slice 6); `LiveArmingInvalid`, `LiveEnvelopeIncomplete`; `primary_custody_world` (Task 1 widened it); `LIVE_ARMING_REQUIRED`, `LIVE_ARMING_LEDGER_INVALID` (Task 2).
- Produces:
  - `ArmingAdmissionFact(state: Literal["ARMED", "NOT_ARMED", "UNREADABLE"], reason_code: str | None, explanation: str, next_step: str | None, observed_at_ms: int)`; `StartRunFacts.arming` / `ResumeRunFacts.arming`: `ArmingAdmissionFact | None = None`
  - `ARMING_REQUIRED_ADMITTED_NOTE`, `ARMING_NEXT_STEP` (schema module, beside the corpus copy)
  - `ArmingFactResolver = Callable[[BrokerBotBinding, ClerkCustodySnapshot, int], ArmingAdmissionFact | None]`; `live_arming_admission_fact(binding, custody, observed_at_ms, *, custody_world=None, settings=None, artifacts_root=None, live_state_root=None)` (defaults resolve from `primary_custody_world()`, `get_alpaca_settings()`, `settings.clerk_dir`, `live_artifacts_root()`)
  - `BotStartAdmission(..., arming_fact: ArmingFactResolver = live_arming_admission_fact)`; `BotResumeAdmission(..., arming_fact=...)`; `BotTaskRegistry(..., arming_fact: ArmingFactResolver = live_arming_admission_fact)`

- [ ] **Step 1: Write the failing policy tests**

Append to `tests/services/test_run_admission.py` (its `_bot` builder gains an `arming: ArmingAdmissionFact | None = None` keyword that it passes straight into `StartRunFacts(...)`; add `ArmingAdmissionFact` and `ARMING_REQUIRED_ADMITTED_NOTE` to its `app.schemas.run_admission` import):

```python
def _arming(state: str, *, reason_code: str | None = None, observed_at_ms: int = _NOW - 500) -> ArmingAdmissionFact:
    return ArmingAdmissionFact(
        state=state,
        reason_code=reason_code,
        explanation={"ARMED": "The instance is armed.", "NOT_ARMED": "The instance has never been armed.", "UNREADABLE": "The arming ledger does not verify."}[state],
        next_step=None if state == "ARMED" else "Run scripts.manage_alpaca_arming plan, then apply.",
        observed_at_ms=observed_at_ms,
    )


def test_an_unreadable_arming_ledger_refuses_a_live_launch_after_the_corpus_gate() -> None:
    decision = evaluate_run_admission(
        _bot(arming=_arming("UNREADABLE", reason_code="LIVE_ARMING_LEDGER_INVALID")),
        _clerk(account_id="9LIVE0001", account_mode="live"),
        evaluated_at_ms=_NOW,
    )
    assert decision.allowed is False
    assert decision.reason_code == "LIVE_ARMING_LEDGER_INVALID"
    assert "does not verify" in decision.explanation


def test_the_corpus_gate_still_comes_first() -> None:
    bot = _bot(arming=_arming("UNREADABLE", reason_code="LIVE_ARMING_LEDGER_INVALID"))
    bot = bot.model_copy(update={"program_build": _program_build(_NOW - 1_000, state="UNPROVEN")})
    decision = evaluate_run_admission(bot, _clerk(account_id="9LIVE0001", account_mode="live"), evaluated_at_ms=_NOW)
    assert decision.reason_code == "PROGRAM_BUILD_UNPROVEN"


def test_a_not_armed_live_launch_is_admitted_and_says_every_enter_will_refuse() -> None:
    decision = evaluate_run_admission(
        _bot(arming=_arming("NOT_ARMED", reason_code="LIVE_ARMING_REQUIRED")),
        _clerk(account_id="9LIVE0001", account_mode="live"),
        evaluated_at_ms=_NOW,
    )
    assert decision.allowed is True
    assert decision.reason_code == "START_ADMITTED"
    assert ARMING_REQUIRED_ADMITTED_NOTE in decision.explanation
    assert decision.next_step is not None and "manage_alpaca_arming" in decision.next_step


def test_an_armed_or_not_applicable_launch_carries_no_arming_note() -> None:
    for arming in (None, _arming("ARMED")):
        decision = evaluate_run_admission(_bot(arming=arming), _clerk(), evaluated_at_ms=_NOW)
        assert decision.allowed is True
        assert ARMING_REQUIRED_ADMITTED_NOTE not in decision.explanation
```

- [ ] **Step 2: Write the failing resolver tests**

`tests/services/test_live_arming_admission.py`:

```python
"""The one resolver Start, Resume and the runner share for the arming fact (slice 7, R6)."""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.live_arming import LIVE_ARMING_LEDGER_INVALID, LIVE_ARMING_REQUIRED, LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.live_arming_admission import live_arming_admission_fact
from tests.broker.alpaca.clerk.live_arming_fixtures import ARMED_AT_MS, live_settings, record_sealed_binding
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES

SID = "ema-live-1"
NOW = ARMED_AT_MS + 60_000


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID,
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id=LIVE_ACCT,
        run_id=f"{SID}-run-1",
        created_at_ms=NOW,
    )


def _custody(account_id: str = LIVE_ACCT, account_mode: str = "live") -> ClerkCustodySnapshot:
    def count() -> CustodyCountFact:
        return CustodyCountFact(state="zero", count=0)

    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode=account_mode,
        strategy_instance_id=SID,
        clerk_generation="clerk-1",
        journal_sequence=1,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=NOW,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=count(),
        pending_orders=count(),
        terminal_orders=count(),
        unresolved_effects=count(),
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=(f"clerk:{account_id}:1",),
        observed_at_ms=NOW,
    )


def _fact(tmp_path: Path, live_state_root: Path, *, custody=None, custody_world: str = "real_live"):
    return live_arming_admission_fact(
        _binding(),
        custody or _custody(),
        NOW,
        custody_world=custody_world,
        settings=live_settings(),
        artifacts_root=tmp_path,
        live_state_root=live_state_root,
    )


def test_paper_shadow_and_dry_run_launches_get_no_fact(tmp_path: Path) -> None:
    assert _fact(tmp_path, tmp_path / "runner", custody=_custody("PA-TEST", "paper"), custody_world="real_paper") is None
    assert _fact(tmp_path, tmp_path / "runner", custody_world="shadow") is None
    dry = _binding().model_copy(update={"mode": "dry_run", "sealed_account_id": f"sim:{SID}"})
    assert live_arming_admission_fact(dry, _custody(), NOW, custody_world="real_live", settings=live_settings(), artifacts_root=tmp_path, live_state_root=tmp_path / "runner") is None


def test_a_never_armed_live_instance_is_not_armed_and_names_the_ceremony(tmp_path: Path) -> None:
    fact = _fact(tmp_path, tmp_path / "runner")
    assert fact is not None
    assert fact.state == "NOT_ARMED"
    assert fact.reason_code == LIVE_ARMING_REQUIRED
    assert fact.next_step is not None and "manage_alpaca_arming" in fact.next_step
    assert fact.observed_at_ms == NOW


def test_an_armed_live_instance_is_armed(tmp_path: Path) -> None:
    live_state_root = tmp_path / "runner"
    seal = record_sealed_binding(live_state_root, strategy_instance_id=SID, sealed_account_id=LIVE_ACCT)
    LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).append(
        LiveArmingRecord.create(
            live_account_id=LIVE_ACCT,
            strategy_instance_id=SID,
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
            shadow_receipt_sha256="e" * 64,
            envelope=TEST_ENVELOPE_VALUES,
            armed_at_ms=ARMED_AT_MS,
            max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
        )
    )
    fact = _fact(tmp_path, live_state_root)
    assert fact is not None
    assert (fact.state, fact.reason_code) == ("ARMED", None)


def test_an_unreadable_ledger_is_unreadable_not_unarmed(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text('{"kind":"armed","schema_version":1}\n', encoding="utf-8")
    fact = _fact(tmp_path, tmp_path / "runner")
    assert fact is not None
    assert (fact.state, fact.reason_code) == ("UNREADABLE", LIVE_ARMING_LEDGER_INVALID)
```

- [ ] **Step 3: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_run_admission.py tests/services/test_live_arming_admission.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: ArmingAdmissionFact`, `ModuleNotFoundError: app.services.live_arming_admission`.

- [ ] **Step 4: The fact and the policy**

In `app/schemas/run_admission.py`, after `ExtendedHoursAdmissionFact` add:

```python
class ArmingAdmissionFact(BaseModel):
    """Whether the sealed instance is armed on the live account it will trade (ADR 0059 D3/D11, slice 7).

    Present only on the real-live custody world; ``None`` on the facts model
    means the world has no arming to consult (paper, shadow, Dry Run).
    ``UNREADABLE`` refuses a launch; ``NOT_ARMED`` admits it and rides the
    decision's explanation — submission, not the launch, is what arming
    gates, and every ENTER of an unarmed instance refuses at the Clerk.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ARMED", "NOT_ARMED", "UNREADABLE"]
    reason_code: str | None = None
    explanation: str
    next_step: str | None = None
    observed_at_ms: int = Field(ge=0)
```

Beside the corpus copy (after `CORPUS_UNCOVERED_NEXT_STEP`) add:

```python
ARMING_NEXT_STEP = (
    "Arm this instance with scripts.manage_alpaca_arming plan, then apply; until then the "
    "Clerk refuses every ENTER it makes."
)
ARMING_REQUIRED_ADMITTED_NOTE = (
    "The instance is not armed: this launch may run and manage exposure, and every ENTER "
    "it makes is refused until an operator arms it (ADR 0059 D11)."
)
```

Add `arming: ArmingAdmissionFact | None = None` as the last field of both `StartRunFacts` and `ResumeRunFacts`.

In `app/services/run_admission.py`: import `ARMING_REQUIRED_ADMITTED_NOTE` and `ARMING_NEXT_STEP` from the schema module and `LIVE_ARMING_LEDGER_INVALID` from `app.broker.alpaca.clerk.live_arming`. Immediately after the `PROGRAM_CORPUS_UNCOVERED` block (line 240) insert:

```python
    # ADR 0059 D11 (slice 7): a launch under arming evidence nobody can verify
    # is refused, not parked. A merely unarmed instance is admitted — the
    # Clerk refuses its every ENTER — and told so below.
    if bot.arming is not None and bot.arming.state == "UNREADABLE":
        return decide(
            allowed=False,
            reason_code=bot.arming.reason_code or LIVE_ARMING_LEDGER_INVALID,
            explanation=bot.arming.explanation,
            next_step=bot.arming.next_step or ARMING_NEXT_STEP,
        )
```

In `_admitted_explanation`, after the corpus stamp, append the arming note when `bot.arming is not None and bot.arming.state == "NOT_ARMED"`, and make the admitted `decide(...)` at line 473 pass `next_step=ARMING_NEXT_STEP if bot.arming is not None and bot.arming.state == "NOT_ARMED" else None`. Written out:

```python
def _admitted_explanation(bot: RunAdmissionFacts) -> str:
    """The admitted sentence, carrying the corpus-coverage and not-armed stamps when they apply."""
    admitted = (
        "The process slot is absent, market data is ready, and the Clerk proves flat custody."
        if bot.operation == "START"
        else "The prior run is terminal, market data is ready, and the Clerk proves resumable custody."
    )
    if bot.program_build.corpus_coverage == "UNCOVERED":
        admitted = f"{admitted} {CORPUS_UNCOVERED_ADMITTED_NOTE}"
    if bot.arming is not None and bot.arming.state == "NOT_ARMED":
        admitted = f"{admitted} {ARMING_REQUIRED_ADMITTED_NOTE}"
    return admitted
```

- [ ] **Step 5: The resolver**

Create `app/services/live_arming_admission.py`:

```python
"""The arming fact Start, Resume and the runner share (ADR 0059 D11, slice 7 R6).

Read-only: one ``account_arming`` read of the ledger and the runner's sealed
bindings, judged at the caller's instant. It answers ``None`` for every world
that has no arming to consult, so paper, shadow and Dry Run launches are
byte-for-byte what they were.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from app.broker.alpaca.clerk.active_authority import primary_custody_world
from app.broker.alpaca.clerk.live_arming import LIVE_ARMING_LEDGER_INVALID, LIVE_ARMING_REQUIRED, LiveArmingInvalid
from app.broker.alpaca.clerk.live_arming_ceremony import account_arming
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.ibkr.config import live_artifacts_root
from app.schemas.account_authority import CustodyWorld
from app.schemas.run_admission import ARMING_NEXT_STEP, ArmingAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding

ArmingFactResolver = Callable[[BrokerBotBinding, ClerkCustodySnapshot, int], ArmingAdmissionFact | None]


def live_arming_admission_fact(
    binding: BrokerBotBinding,
    custody: ClerkCustodySnapshot,
    observed_at_ms: int,
    *,
    custody_world: CustodyWorld | None = None,
    settings: AlpacaSettings | None = None,
    artifacts_root: Path | None = None,
    live_state_root: Path | None = None,
) -> ArmingAdmissionFact | None:
    """The instance's arming state on the live account, or ``None`` where arming does not apply.

    The keyword seams exist for tests and for callers that already hold the
    values; production resolves each from its one owner.
    """
    world = primary_custody_world() if custody_world is None else custody_world
    if binding.mode == "dry_run" or custody.account_mode != "live" or world != "real_live":
        return None
    try:
        resolved = get_alpaca_settings() if settings is None else settings
        arming = account_arming(
            live_account_id=custody.account_id,
            artifacts_root=resolved.clerk_dir if artifacts_root is None else artifacts_root,
            live_state_root=live_artifacts_root() if live_state_root is None else live_state_root,
            configured_envelope=LiveEnvelopeValues.from_settings(resolved),
            now_ms=observed_at_ms,
            strategy_instance_ids=(binding.strategy_instance_id,),
        )
    except (LiveArmingInvalid, LiveEnvelopeIncomplete, ValidationError) as exc:
        return ArmingAdmissionFact(
            state="UNREADABLE",
            reason_code=LIVE_ARMING_LEDGER_INVALID,
            explanation=f"The arming evidence cannot be judged: {exc}",
            next_step="Restore the arming ledger and the ALPACA_LIVE_* environment, then retry.",
            observed_at_ms=observed_at_ms,
        )
    status = arming.statuses[binding.strategy_instance_id]
    if status.state == "armed":
        return ArmingAdmissionFact(
            state="ARMED",
            explanation=f"{binding.strategy_instance_id} is armed on {custody.account_id} with {status.sessions_remaining} session(s) remaining.",
            observed_at_ms=observed_at_ms,
        )
    return ArmingAdmissionFact(
        state="NOT_ARMED",
        reason_code=status.reason_code or LIVE_ARMING_REQUIRED,
        explanation=(
            f"{binding.strategy_instance_id} has never been armed on {custody.account_id}."
            if status.state == "unarmed"
            else f"{binding.strategy_instance_id} is {status.state} on {custody.account_id} ({status.reason_code})."
        ),
        next_step=ARMING_NEXT_STEP,
        observed_at_ms=observed_at_ms,
    )


__all__ = ["ArmingFactResolver", "live_arming_admission_fact"]
```

- [ ] **Step 6: Compose the fact in Start and Resume**

In `app/services/bot_start_admission.py`: import `ArmingFactResolver, live_arming_admission_fact` from `app.services.live_arming_admission`; `BotStartAdmission.__init__` gains `arming_fact: ArmingFactResolver = live_arming_admission_fact,` (stored as `self._arming_fact`); in `_decision`, in the `StartRunFacts(...)` construction after `extended_hours=...` add `arming=self._arming_fact(binding, custody, observed_at_ms),`.

In `app/services/bot_resume_admission.py`: the same import and constructor keyword; in its `_decision`, the `ResumeRunFacts(...)` construction gains `arming=self._arming_fact(proposed, custody, observed_at_ms),` (use the local names that function already has for the sealed proposed binding, the custody snapshot and the observation instant).

In `app/services/bot_runner.py`: `BotTaskRegistry.__init__` gains the keyword `arming_fact: ArmingFactResolver = live_arming_admission_fact,` and passes `arming_fact=arming_fact` into both the `BotStartAdmission(...)` and the `BotResumeAdmission(...)` it constructs. One import, one keyword, two arguments — nothing else in the file changes.

- [ ] **Step 7: Run the suites**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_run_admission.py tests/services/test_live_arming_admission.py tests/services/test_bot_start_admission.py tests/services/test_bot_resume_admission.py tests/services/test_boot_recovery.py -q -p no:cacheprovider`
Expected: PASS (drop a listed file only if it does not exist). The registry tests run with the default resolver, which answers `None` on their paper custody.

- [ ] **Step 8: Commit**

```bash
git add PythonDataService/app/schemas/run_admission.py PythonDataService/app/services/run_admission.py PythonDataService/app/services/live_arming_admission.py PythonDataService/app/services/bot_start_admission.py PythonDataService/app/services/bot_resume_admission.py PythonDataService/app/services/bot_runner.py PythonDataService/tests/services/test_run_admission.py PythonDataService/tests/services/test_live_arming_admission.py
git commit -m "feat(alpaca): Start and Resume read the instance's arming on the live authority (ADR 0059 slice 7, R6)

An unreadable ledger refuses the launch; an unarmed instance is admitted and
told every ENTER will refuse until it is armed; paper, shadow and Dry Run
launches are unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Deploy on the live world — the `live` mode, the receipt copy, and the rehearsal's receipt found by twin identity (R7, R12)

**Files:**
- Modify: `PythonDataService/app/schemas/broker_bots.py:154, 246, 279-303, 503`
- Modify: `PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py:152-159, 184-224, 241-260, 271-330, 721-726`
- Modify: `PythonDataService/app/services/broker_v2_panel/panel_deploy.py:72-82`
- Modify: `PythonDataService/app/services/broker_v2_panel/panel_projection_service.py:473-484`
- Modify: `PythonDataService/app/broker/alpaca/clerk/shadow_receipt.py:170-184`
- Modify: `PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py:68-74, 138-155, 178-227`
- Test: `PythonDataService/tests/broker/v2panel/test_panel_deploy_live.py` (new), `PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ceremony_twin.py` (new), `PythonDataService/tests/broker/v2panel/test_panel_deploy_shadow.py` (one assertion), `PythonDataService/tests/services/test_panel_projection_service.py` (one string, if pinned)

**Interfaces:**
- Consumes: `CustodyWorld` with `real_live` (Task 1); `twins_agree` (`alpaca_shadow_reconciliation.py`, slice 4); `custody_account_ids_for`.
- Produces:
  - wire: `AlpacaPaperDeployRequest.execution_mode`, `AlpacaPaperDeployReceipt.execution_mode`: `Literal["paper", "dry_run", "shadow", "live"]`; `AlpacaPaperDeployStrategy.admissible_modes: tuple[Literal["dry_run", "paper", "shadow", "live"], ...]`
  - `BrokerExecutionMode = Literal["paper", "shadow", "live"]`; `_broker_mode_for(custody_world)` maps `real_live → "live"`
  - `ShadowReceiptStore.current_for_account(live_account_id, *, required_sessions) -> tuple[ShadowReceipt, ...]`
  - `InstanceSeal(seal_hash, configured_signal_hash, program: SealedBotProgram)`; `twin_receipt(*, store, live_account_id, program, required_sessions, seals: Mapping[str, InstanceSeal]) -> ShadowReceipt | None`

- [ ] **Step 1: Write the failing deploy-view tests**

`tests/broker/v2panel/test_panel_deploy_live.py` (mirrors `test_panel_deploy_shadow.py`; reuse its `_entries`, `_clerk_status`, `_live_account` helpers by import):

```python
"""On the real-live authority the deploy path offers `live`, never `paper` or `shadow` (ADR 0059 D11, slice 7 R7)."""

from __future__ import annotations

import pytest

from app.schemas.broker_bots import AlpacaPaperDeployRequest, AlpacaPaperDeployView, AlpacaPaperSizingSelection
from app.services.broker_v2_panel.panel_deploy import _require_alpaca_deploy_request
from app.services.broker_v2_panel.panel_errors import PanelRunnerError
from app.services.broker_v2_panel.paper_deploy_service import build_alpaca_paper_deploy_view
from tests._helpers.canary_admission import admit_canary_pairing
from tests.broker.v2panel.test_panel_deploy_shadow import LIVE_ACCT, _STRATEGY_KEY, _clerk_status, _entries, _live_account


def _live_view(monkeypatch: pytest.MonkeyPatch) -> AlpacaPaperDeployView:
    admit_canary_pairing(monkeypatch, _STRATEGY_KEY, LIVE_ACCT)
    return build_alpaca_paper_deploy_view(
        _live_account(), _clerk_status(LIVE_ACCT), _entries(), symbol="SPY", custody_world="real_live"
    )


def test_the_live_world_offers_live_and_dry_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _live_view(monkeypatch)
    assert view.account_mode == "live"
    assert view.account_label == f"Alpaca live · {LIVE_ACCT}"
    assert {mode.mode: mode.availability for mode in view.execution_modes} == {"dry_run": "available", "live": "available"}
    assert all("paper" not in strategy.admissible_modes and "shadow" not in strategy.admissible_modes for strategy in view.strategies)
    assert any("live" in strategy.admissible_modes for strategy in view.strategies)
    assert view.eligibility.eligible is True


def test_the_live_view_names_real_money_and_the_arming_step(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _live_view(monkeypatch)
    live = next(mode for mode in view.execution_modes if mode.mode == "live")
    assert "real-money" in live.explanation and "arm" in live.explanation
    row = next(check for check in view.readiness_checks if check.gate_id == "broker.account_posture")
    assert row.label == "Live account posture"
    assert "paper" not in row.headline.lower()


def test_a_live_request_passes_the_mode_offer_check(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _live_view(monkeypatch)
    strategy = next(s for s in view.strategies if s.selectable)
    request = AlpacaPaperDeployRequest(
        strategy_instance_id="ema-live-1",
        strategy_key=strategy.strategy_key,
        symbol="SPY",
        execution_mode="live",
        sizing=AlpacaPaperSizingSelection(preset="safe_canary", quantity=1),
        carryover_policy="FORBID",
        evidence_override=None,
    )
    assert _require_alpaca_deploy_request(view, request) is not None
    with pytest.raises(PanelRunnerError, match="not available on this account"):
        _require_alpaca_deploy_request(view, request.model_copy(update={"execution_mode": "shadow"}))
```

If `AlpacaPaperSizingSelection` is not the sizing model's name, use the class `AlpacaPaperDeployRequest.sizing` is annotated with (read it at `broker_bots.py:150-160`); if the request has a required field this constructor omits, copy the construction from `test_panel_deploy_shadow.py`'s request fixture.

In `tests/broker/v2panel/test_panel_deploy_shadow.py::test_real_paper_world_is_unchanged` (line 128) the expected mode set `{"dry_run", "paper", "live"}` stays true (the live card is still `planned` on a paper world) — run it to prove it.

- [ ] **Step 2: Write the failing twin-receipt tests**

`tests/broker/alpaca/clerk/test_live_arming_ceremony_twin.py`:

```python
"""A live-sealed instance finds its rehearsal's receipt by twin identity (slice 7, R7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import LIVE_SHADOW_INCOMPLETE, LiveArmingRefused
from app.broker.alpaca.clerk.live_arming_ceremony import observe_arming_inputs
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMING_SID,
    activate_shadow_fence,
    live_settings,
    record_sealed_binding,
    seal_receipt,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, SHADOW_ACCT, TEST_ENVELOPE_VALUES

LIVE_SID = "ema-live-1"


def _rehearsed(tmp_path: Path, live_state_root: Path, *, quantity: int = 1):
    """The shadow instance, its receipt, and the fence — the slice-6 world as it stands after a gate."""
    activate_shadow_fence(tmp_path)
    shadow_seal = record_sealed_binding(live_state_root, strategy_instance_id=ARMING_SID, sealed_account_id=SHADOW_ACCT, quantity=quantity)
    seal_receipt(
        tmp_path,
        configured_signal_hash=shadow_seal.configured_signal_hash,
        strategy_instance_id=ARMING_SID,
        sessions=TEST_ENVELOPE_VALUES.shadow_sessions,
    )
    return shadow_seal


def test_the_live_instance_arms_on_its_twins_receipt(tmp_path: Path) -> None:
    live_state_root = tmp_path / "runner"
    _rehearsed(tmp_path, live_state_root)
    live_seal = record_sealed_binding(live_state_root, strategy_instance_id=LIVE_SID, sealed_account_id=LIVE_ACCT)

    inputs = observe_arming_inputs(
        strategy_instance_id=LIVE_SID, artifacts_root=tmp_path, live_state_root=live_state_root, settings=live_settings()
    )

    assert inputs.strategy_instance_id == LIVE_SID
    assert inputs.seal_hash == live_seal.bot_configuration_hash
    assert inputs.live_account_id == LIVE_ACCT
    # The receipt is the rehearsal's; the record will name it beside the live seal.
    assert inputs.configured_signal_hash == live_seal.configured_signal_hash


def test_a_size_change_between_rehearsal_and_live_is_shadow_incomplete(tmp_path: Path) -> None:
    live_state_root = tmp_path / "runner"
    _rehearsed(tmp_path, live_state_root, quantity=1)
    record_sealed_binding(live_state_root, strategy_instance_id=LIVE_SID, sealed_account_id=LIVE_ACCT, quantity=2)

    with pytest.raises(LiveArmingRefused) as exc_info:
        observe_arming_inputs(
            strategy_instance_id=LIVE_SID, artifacts_root=tmp_path, live_state_root=live_state_root, settings=live_settings()
        )
    assert exc_info.value.reason_code == LIVE_SHADOW_INCOMPLETE


def test_the_shadow_instance_itself_still_arms_on_its_own_receipt(tmp_path: Path) -> None:
    """Slice 6 unchanged: an instance with its own current receipt never needs a twin."""
    live_state_root = tmp_path / "runner"
    _rehearsed(tmp_path, live_state_root)
    inputs = observe_arming_inputs(
        strategy_instance_id=ARMING_SID, artifacts_root=tmp_path, live_state_root=live_state_root, settings=live_settings()
    )
    assert inputs.strategy_instance_id == ARMING_SID
```

- [ ] **Step 3: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/v2panel/test_panel_deploy_live.py tests/broker/alpaca/clerk/test_live_arming_ceremony_twin.py -q -p no:cacheprovider`
Expected: FAIL — a `ValidationError` on `custody_world="real_live"` / `execution_mode="live"`, and `LIVE_SHADOW_INCOMPLETE` on the twin test.

- [ ] **Step 4: The wire and the view**

In `app/schemas/broker_bots.py`: lines 154 and 503 become `execution_mode: Literal["paper", "dry_run", "shadow", "live"] = "paper"`; line 246 becomes `admissible_modes: tuple[Literal["dry_run", "paper", "shadow", "live"], ...]`; in `_admissible_modes_invariants` (line 295) the admitted tuples become `(("dry_run", "paper"), ("dry_run", "shadow"), ("dry_run", "live"))` and the message `"... its account's one broker-contacting mode (paper, shadow or live)."`; extend the comment above it with "`live` on the real-live authority (ADR 0059 D11, slice 7)".

In `app/services/broker_v2_panel/paper_deploy_service.py`:
- Lines 152–159:

```python
BrokerExecutionMode = Literal["paper", "shadow", "live"]

DeployExecutionMode = Literal["dry_run", "paper", "shadow", "live"]

_BROKER_MODE_BY_WORLD: dict[str, BrokerExecutionMode] = {
    "real_paper": "paper",
    "shadow": "shadow",
    "real_live": "live",
}


def _broker_mode_for(custody_world: CustodyWorld) -> BrokerExecutionMode:
    """The one broker-contacting mode this custody world can offer (ADR 0059 D2/D11)."""
    return _BROKER_MODE_BY_WORLD[custody_world]
```

- In `_deploy_view_copy`, before the paper `return` add a `real_live` branch:

```python
    if custody_world == "real_live":
        return _DeployViewCopy(
            posture_label="Live account posture",
            posture_ready_headline="The Alpaca live account is active and tradable.",
            posture_blocked_headline="Deployment is blocked by the Alpaca live account posture.",
            posture_ready_explanation=(
                "The server resolved the real-money account this live authority custodies and "
                "found no broker trading block."
            ),
            posture_evidence_summary=(
                f"Alpaca live account {account.account_id}, custodied by its live authority, "
                f"reports status {account.account_status}."
            ),
            posture_recovery=(
                f"Restore live account {account.account_id} to ACTIVE and unblocked, then refresh."
            ),
            eligible_headline="This Alpaca live account is eligible for a Clerk-governed live deployment.",
            eligible_explanation=(
                "The operator may choose Clerk-governed live execution — real-money submission for an "
                "armed instance only; every ENTER of an unarmed instance is refused — or a "
                "zero-broker-write Dry Run before launch."
            ),
        )
```

- `_RECEIPT_COPY` gains:

```python
    "live": _ReceiptCopy(
        duty="Alpaca live",
        explanation=(
            "The deployment binding is durable and sealed on the real-money account; every ENTER "
            "is refused until an operator arms this instance (ADR 0059 D11)."
        ),
        next_action=(
            "Arm the instance with scripts.manage_alpaca_arming plan, then apply; then open the bot "
            "panel and verify the first Clerk receipt."
        ),
    ),
```

- `_execution_modes` becomes:

```python
def _execution_modes(broker_mode: BrokerExecutionMode) -> tuple[AlpacaPaperExecutionMode, ...]:
    """Author the mode cards this custody world offers.

    Dry Run is offered in every world. The one broker-contacting card is the
    world's own — ``paper``, ``shadow``, or ``live`` on the real-live
    authority (ADR 0059 D2/D11). The paper and shadow worlds also show the
    ``live`` card as `planned`, because graduation is the step that follows
    them; on the live world that card *is* the broker card.
    """
    dry_run = AlpacaPaperExecutionMode(
        mode="dry_run",
        label="Dry Run",
        availability="available",
        explanation=(
            "Real market data and strategy decisions produce clearly simulated fills; "
            "the runner never calls the Clerk's broker-effect boundary."
        ),
    )
    broker_cards: dict[BrokerExecutionMode, AlpacaPaperExecutionMode] = {
        "paper": AlpacaPaperExecutionMode(
            mode="paper",
            label="Paper",
            availability="available",
            explanation="Orders route only to the selected Alpaca paper account through the Clerk.",
        ),
        "shadow": AlpacaPaperExecutionMode(
            mode="shadow",
            label="Shadow",
            availability="available",
            explanation=(
                "Decisions run against this live account's real reads; every fill is "
                "synthesized by the shadow Clerk and nothing is submitted (ADR 0059 D2)."
            ),
        ),
        "live": AlpacaPaperExecutionMode(
            mode="live",
            label="Live",
            availability="available",
            explanation=(
                "Real-money submission through the live Clerk for an armed instance only; "
                "every ENTER of an unarmed instance is refused until an operator arms it (ADR 0059 D11)."
            ),
        ),
    }
    if broker_mode == "live":
        return (dry_run, broker_cards["live"])
    planned_live = AlpacaPaperExecutionMode(
        mode="live",
        label="Live",
        availability="planned",
        explanation=(
            "Real-money submission requires a completed shadow receipt, the live cutover and the "
            "arming ceremony (ADR 0059)."
            if broker_mode == "shadow"
            else "Live Alpaca execution is planned but is not connected to an admission or execution path."
        ),
    )
    return (dry_run, broker_cards[broker_mode], planned_live)
```

- `_admissible_modes` return type gains `"live"`.
- Line 726: `account_label=f"Alpaca {broker_mode} · {account.account_id}",` — one line for three worlds (`paper`, `shadow`, `live`).

In `app/services/broker_v2_panel/panel_deploy.py` (lines 72–82) reword the refusal so it no longer claims the live world is unconstructible:

```python
        raise PanelUnavailableError(
            "Alpaca account deployment is refused.",
            detail=(
                f"The primary authority custodies the {custody_world} world, which does not admit "
                f"an account Alpaca reports as {account.account_mode!r} (ADR 0059 D1)."
            ),
            next_action=(
                "Reconnect with credentials for the account this authority custodies, or activate "
                "the authority for this account, then refresh."
            ),
        )
```

In `app/services/broker_v2_panel/panel_projection_service.py` (line 484) the `"trade"` sentence becomes world-neutral and true on every world: `"trade": "Broker execution through the Clerk. Only the Clerk may submit, cancel, or reduce broker orders.",`. If `tests/services/test_panel_projection_service.py` pins the old "Paper execution." string, update that one string.

- [ ] **Step 5: The twin-identity receipt lookup**

In `app/broker/alpaca/clerk/shadow_receipt.py`, after `current(...)` add:

```python
    def current_for_account(self, live_account_id: str, *, required_sessions: int) -> tuple[ShadowReceipt, ...]:
        """Every instance's latest receipt on this account that still proves the count, newest first.

        The read slice 7's twin lookup makes: a live-sealed instance has no
        receipt of its own, so the ceremony asks which rehearsals on this
        account are current and then matches by program identity.
        """
        latest_by_instance: dict[str, ShadowReceipt] = {}
        for receipt in self._read_all():
            if receipt.live_account_id == live_account_id:
                latest_by_instance[receipt.strategy_instance_id] = receipt
        return tuple(
            receipt
            for receipt in reversed(list(latest_by_instance.values()))
            if len(receipt.sessions) >= required_sessions
        )
```

In `app/broker/alpaca/clerk/live_arming_ceremony.py`:
- `InstanceSeal` gains `program: SealedBotProgram` (import `SealedBotProgram` from `app.schemas.signal_program_seal`); `instance_seal_hashes` sets `program=seal`.
- After `instance_seal_hashes` add:

```python
def twin_receipt(
    *,
    store: ShadowReceiptStore,
    live_account_id: str,
    program: SealedBotProgram,
    required_sessions: int,
    seals: Mapping[str, InstanceSeal],
) -> ShadowReceipt | None:
    """The receipt of a rehearsal on this account whose sealed program is ``program``'s twin (slice 7, R7).

    A live-sealed instance is a new instance (its account is inside its
    seal), so the shadow receipt that proved its program names a different
    instance id. Identity is the one the shadow gate itself used —
    ``twins_agree``: configured signal, action plan, size and carryover
    policy, a ``shadow:``-sealed rehearsal and a side that is not.
    """
    # Local import: the reconciliation module imports the sqlite package; the
    # ceremony is imported at boot by the live selector and must not pull it in.
    from app.services.alpaca_shadow_reconciliation import twins_agree

    for receipt in store.current_for_account(live_account_id, required_sessions=required_sessions):
        rehearsed = seals.get(receipt.strategy_instance_id)
        if rehearsed is None or receipt.configured_signal_hash != rehearsed.configured_signal_hash:
            continue
        if twins_agree(rehearsed.program, program) is None:
            return receipt
    return None
```

- In `observe_arming_inputs`, replace the receipt lookup (lines 196–206) with:

```python
    seals = instance_seal_hashes(live_account_id=live_account_id, live_state_root=live_state_root)
    seal = seals.get(strategy_instance_id)
    if seal is None:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"{strategy_instance_id} has no sealed alpaca binding on {live_account_id}",
        )
    store = ShadowReceiptStore(artifacts_root)
    receipt = store.current(
        strategy_instance_id,
        configured_signal_hash=seal.configured_signal_hash,
        required_sessions=envelope.shadow_sessions,
    )
    if receipt is None:
        # A live-sealed instance is a new instance: its proof is its twin's (R7).
        receipt = twin_receipt(
            store=store,
            live_account_id=live_account_id,
            program=seal.program,
            required_sessions=envelope.shadow_sessions,
            seals=seals,
        )
    if receipt is None:
        raise LiveArmingRefused(
            LIVE_SHADOW_INCOMPLETE,
            f"{strategy_instance_id} has no current shadow receipt for this program over "
            f"{envelope.shadow_sessions} session(s) — its own, or a rehearsal's with the same "
            "configured signal, action plan, size and carryover policy",
        )
```

(the existing `seal = instance_seal_hashes(...).get(...)` lines 188–195 are replaced by the first six lines above; the account check that follows the lookup stays as it is). Add `"twin_receipt"` to `__all__`.

- [ ] **Step 6: Run the suites**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/v2panel/test_panel_deploy_live.py tests/broker/v2panel/test_panel_deploy_shadow.py tests/broker/v2panel/test_panel_deploy.py tests/broker/alpaca/clerk/test_live_arming_ceremony_twin.py tests/broker/alpaca/clerk/test_live_arming_ceremony.py tests/broker/alpaca/clerk/test_shadow_receipt.py tests/scripts/test_manage_alpaca_arming.py tests/services/test_panel_projection_service.py -q -p no:cacheprovider`
Expected: PASS (drop a listed file only if it does not exist). `export_openapi_contract.py --check` now reports drift by design — Task 9 regenerates.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/schemas/broker_bots.py PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py PythonDataService/app/services/broker_v2_panel/panel_deploy.py PythonDataService/app/services/broker_v2_panel/panel_projection_service.py PythonDataService/app/broker/alpaca/clerk/shadow_receipt.py PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py PythonDataService/tests/broker/v2panel/test_panel_deploy_live.py PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ceremony_twin.py PythonDataService/tests/broker/v2panel/test_panel_deploy_shadow.py PythonDataService/tests/services/test_panel_projection_service.py
git commit -m "feat(alpaca): the live world deploys live, and a live instance arms on its twin's receipt (ADR 0059 slice 7, R7, R12)

The deploy wire gains live; the real-live world offers dry_run and live only;
the ceremony finds a live-sealed instance's rehearsal by twin identity.
OpenAPI --check drifts until Task 9 regenerates.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: The verdict reads every facade authority and says what is true (R9)

**Files:**
- Modify: `PythonDataService/app/services/alpaca_live_verdict.py:46-71, 129-139, 163-237, 331-383`
- Test: `PythonDataService/tests/services/test_alpaca_live_verdict.py` (append; two pinned strings)

**Interfaces:**
- Consumes: `SQLITE_FACADE_AUTHORITIES`; `ActiveClerkRuntime.selected_account_authority_kind` (`real_live` from Task 4); `live_account_id_for_shadow_account` (identity on a real id).
- Produces: `LiveSituation = Literal["live_armed", "live_unarmed", "armed", "shadow", "bare"]`; `observe_arming` / `observe_loss_hold` answer on any `SQLITE_FACADE_AUTHORITIES` runtime.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_alpaca_live_verdict.py`:

```python
class _LiveClerk:
    """The two facts the verdict reads off a live facade: its envelope and its kind."""

    authority_kind = "sqlite"
    account_id = LIVE_ACCT

    def __init__(self) -> None:
        from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate

        self.live_envelope = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False)


def _live_runtime() -> ActiveClerkRuntime:
    return ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=_LiveClerk(),
        account_id=LIVE_ACCT,
        account_authority_kind="real_live",
    )


def test_the_verdict_counts_arming_on_the_real_live_authority(tmp_path: Path) -> None:
    """Slice 6 counted only under shadow; the live authority is the widening point it named."""
    live_state_root = tmp_path / "runner"
    seal = record_sealed_binding(live_state_root, strategy_instance_id="ema-live-1", sealed_account_id=LIVE_ACCT)
    LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).append(
        LiveArmingRecord.create(
            live_account_id=LIVE_ACCT,
            strategy_instance_id="ema-live-1",
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
            shadow_receipt_sha256="e" * 64,
            envelope=TEST_ENVELOPE_VALUES,
            armed_at_ms=ARMED_AT_MS,
            max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
        )
    )
    observation = observe_arming(
        _live_runtime(), tmp_path, lambda: live_state_root, settings=live_settings(), now_ms=ARMED_AT_MS + 60_000
    )
    assert observation.armed_instance_count == 1
    assert observation.envelope_state == "sealed"


def test_a_live_armed_real_live_account_says_submission_is_open() -> None:
    verdict = alpaca_live_verdict(
        settings=live_settings(),
        runtime=_live_runtime(),
        now_ms=_NOW,
        arming=ArmingObservation(armed_instance_count=2, envelope_state="sealed", detail=""),
        loss_hold="clear",
    )
    assert verdict.final_verdict == "live-armed"
    assert verdict.clerk_authority == "sqlite"
    assert verdict.headline == f"LIVE account {LIVE_ACCT} — 2 instances armed, real-money submission open"
    assert "submitted to Alpaca" in verdict.detail
    assert "nothing submitted" not in verdict.detail.lower()


def test_a_real_live_account_with_nothing_armed_says_every_enter_refuses() -> None:
    verdict = alpaca_live_verdict(settings=live_settings(), runtime=_live_runtime(), now_ms=_NOW)
    assert verdict.final_verdict == "live-unarmed"
    assert verdict.headline == f"LIVE account {LIVE_ACCT} — real-money authority installed, no instance armed"
    assert "every ENTER" in verdict.detail


def test_the_shadow_rows_no_longer_promise_a_future_slice(shadow_runtime) -> None:
    runtime, _broker = shadow_runtime
    verdict = alpaca_live_verdict(
        settings=live_settings(),
        runtime=runtime,
        now_ms=_NOW,
        arming=ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail=""),
    )
    assert "slice 7" not in verdict.detail
    assert "under the shadow authority" in verdict.detail
```

(`record_sealed_binding`, `live_settings`, `ARMED_AT_MS` come from `live_arming_fixtures`; `LIVE_ACCT`, `TEST_ENVELOPE_VALUES` from `live_envelope_fixtures`; `shadow_runtime` is the slice-6 fixture this file already imports.) The slice-6 test that asserts `"slice 7" in verdict.detail` now asserts `"under the shadow authority" in verdict.detail`; the slice-6 test that asserts the `armed` headline `"... armed, nothing submitted yet"` now asserts `"... armed under the shadow authority, nothing submitted"`. Change those two strings and nothing else in existing tests.

- [ ] **Step 2: Run to verify they fail**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py -q -p no:cacheprovider`
Expected: FAIL on the four new tests (count 0, old headlines) and the two re-pinned strings.

- [ ] **Step 3: Widen the observations and re-author the copy**

In `app/services/alpaca_live_verdict.py`:
- `observe_loss_hold` (line 131): `if runtime is None or runtime.authority_kind not in SQLITE_FACADE_AUTHORITIES:`; `observe_arming` (line 192): `or runtime.authority_kind not in SQLITE_FACADE_AUTHORITIES` and delete the three-line comment above it; both read the live id through `live_account_id_for_shadow_account(...)`, which is the identity on a real id. Update `observe_arming`'s docstring: "reads on the shadow authority and on the real-live authority (slice 7); paper and unavailable runtimes report nothing".
- Replace `LiveSituation` and `_LIVE_COPY` (lines 49–71) with:

```python
LiveSituation = Literal["live_armed", "live_unarmed", "armed", "shadow", "bare"]

_LIVE_COPY: dict[LiveSituation, tuple[str, str]] = {
    "live_armed": (
        "LIVE account {account} — {instances} armed, real-money submission open",
        "This is a real-money Alpaca account custodied by its live authority, and an operator has "
        "armed {instances} on it. An armed instance's ENTER is admitted through the Clerk, the "
        "arming gate and the risk envelope and is submitted to Alpaca; every other instance's "
        "ENTER is refused.",
    ),
    "live_unarmed": (
        "LIVE account {account} — real-money authority installed, no instance armed",
        "This is a real-money Alpaca account custodied by its live authority. No sealed instance "
        "is armed, so every ENTER is refused; EXITs and operator reduce-only actions still run. "
        "Arming requires a completed shadow receipt and the supervised ceremony (ADR 0059).",
    ),
    "armed": (
        "LIVE account {account} — {instances} armed under the shadow authority, nothing submitted",
        "This is a real-money Alpaca account read by its shadow authority, and an operator has "
        "armed {instances} on it. Nothing is submitted under the shadow authority: every fill is "
        "synthesized. The live cutover (graduation) is what installs the authority that submits.",
    ),
    "shadow": (
        "LIVE account {account} — shadow authority active, no instance armed",
        "This is a real-money Alpaca account. Its shadow authority reads it and synthesizes "
        "every fill; nothing is submitted under the shadow authority. Arming requires a completed "
        "shadow receipt and the supervised ceremony (ADR 0059).",
    ),
    "bare": (
        "LIVE account {account} — real money, no instance armed",
        "This is a real-money Alpaca account. No sealed instance is armed, so every order path "
        "refuses. Arming requires a completed shadow receipt and the supervised ceremony (ADR 0059).",
    ),
}
```

- In `alpaca_live_verdict` (lines 331–358), derive the situation from the world:

```python
    observed_shadow: ShadowState = shadow_state if shadow_state is not None else "none"
    shadow_active = authority == "shadow"
    live_custody = runtime is not None and runtime.selected_account_authority_kind == "real_live"
    ...
    live_armed = armed >= 1 and authority in SQLITE_FACADE_AUTHORITIES
    instances = f"{armed} instance{'' if armed == 1 else 's'}"
    if live_custody:
        situation: LiveSituation = "live_armed" if live_armed else "live_unarmed"
    else:
        situation = "armed" if live_armed else "shadow" if shadow_active else "bare"
```

(keep every other line of that function as it is).

- [ ] **Step 4: Run the suites**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py tests/routers/test_alpaca_live_verdict_endpoint.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/services/alpaca_live_verdict.py PythonDataService/tests/services/test_alpaca_live_verdict.py
git commit -m "feat(alpaca): the live verdict counts arming on the live authority and says submission is open (ADR 0059 slice 7, R9)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: The contract is regenerated once, and the Frontend stops calling a live world "Paper" (R12)

**Files:**
- Regenerate: `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts`
- Modify: `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts:66, 251-266, 444-459, 593-607`
- Modify: `Frontend/src/app/components/broker/broker-deploy-page/deploy-execution-section.component.ts:61-82`
- Modify: `Frontend/src/app/components/broker/broker-deploy-page/deploy-paper-access.component.ts:48-55`
- Modify: `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-drawer.component.ts:8-20`
- Modify: `Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-lens-data.service.ts:29`
- Modify: `Frontend/src/app/components/broker/v2-panel/trader-lens/trader-lens.component.ts:73` and `.html:33`
- Test: `Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.fixtures.ts` (one fixture), `alpaca-deploy-workflow.component.spec.ts` (one case), `alpaca-deploy-drawer.component.spec.ts:70` (one expectation), `Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-lens-data.service.spec.ts` (one case), `Frontend/src/app/components/broker/v2-panel/trader-lens/trader-lens.component.spec.ts` (rename only)

**Interfaces:**
- Consumes: the widened `execution_mode` / `admissible_modes` enums (Task 7) via the regenerated `components['schemas']`.
- Produces: `brokerMode: Signal<'paper' | 'shadow' | 'live'>`, `brokerModeLabel: Signal<'Paper' | 'Shadow' | 'Live'>`; `LIVE_DEPLOY_VIEW` fixture.

- [ ] **Step 1: Regenerate the contract — once**

Run, from `PythonDataService/`: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py` then, from `Frontend/`: `npm run codegen:openapi`. Then `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check` (from `PythonDataService/`) and `npm run codegen:check` (from `Frontend/`).
Expected: both checks exit 0. `git diff --stat -- contracts/openapi/python-data-service.openapi.json Frontend/src/app/api/broker.types.ts` shows exactly the three widened enums (`AlpacaPaperDeployRequest.execution_mode`, `AlpacaPaperDeployReceipt.execution_mode`, `AlpacaPaperDeployStrategy.admissible_modes.items`) and nothing else; if anything else moved, a schema edit escaped Tasks 6–8 — find it, do not regenerate again to hide it.

- [ ] **Step 2: Write the failing spec case**

In `alpaca-deploy-workflow.fixtures.ts`, after `SHADOW_DEPLOY_VIEW` add:

```ts
/** The real-live authority's view: Dry Run and Live, and nothing that lies (ADR 0059 D11, slice 7). */
export const LIVE_DEPLOY_VIEW: DeployBotView = {
  ...SHADOW_DEPLOY_VIEW,
  account_label: 'Alpaca live · 9LIVE0001',
  strategies: SHADOW_DEPLOY_VIEW.strategies.map((strategy) => ({
    ...strategy,
    admissible_modes: strategy.admissible_modes.map((mode) => (mode === 'shadow' ? 'live' : mode)),
  })),
  execution_modes: [
    DRY_RUN_EXECUTION_MODE,
    {
      mode: 'live',
      label: 'Live',
      availability: 'available',
      explanation: 'Real-money submission through the live Clerk for an armed instance only.',
    },
  ],
};
```

In `alpaca-deploy-workflow.component.spec.ts`, beside the shadow case (`'on a shadow view the Shadow mode is offered, selected by default, and submitted'`, lines 584–608) add, importing `LIVE_DEPLOY_VIEW`:

```ts
  it('on a live view the Live mode is offered, selected by default, and submitted', async () => {
    const service = mockService(RECEIPT, LIVE_DEPLOY_VIEW);
    await renderWorkflow(service);

    expect(screen.queryByRole('radio', { name: /Paper/ })).toBeNull();
    expect(screen.queryByRole('radio', { name: /Shadow/ })).toBeNull();
    const live = screen.getByRole<HTMLInputElement>('radio', { name: /Live/ });
    expect(live.checked).toBe(true);
    expect(screen.getByRole('button', { name: 'Deploy live bot' })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/paper account/i);
    expect(document.body.textContent).not.toMatch(/shadow/i);

    await completeTicketAndSubmit();

    const [, body] = service.deployBot.mock.calls[0] as [string, DeployBotBody];
    expect(body.execution_mode).toBe('live');
  });
```

`completeTicketAndSubmit()` is whatever helper the shadow case uses to fill the ticket and click deploy — reuse it under its real name (read lines 584–608); the assertions above are the contract.

`alpaca-deploy-drawer.component.spec.ts:70`: the live-account case now expects the header `Deploy · 9LIVE0001` with no world suffix and the noun `Alpaca account` — a live account is shadowed or live-custodied, and the drawer does not know which until the view says.

`alpaca-operator-lens-data.service.spec.ts`: add a case beside the `shadow` one (line 68): `clerkStatus({ account_id: '9LIVE0001', authority_kind: 'real_live' })` resolves the account id (the Operator lens lights on the live authority).

- [ ] **Step 3: Run to verify they fail**

Run from `Frontend/`: `npx ng test --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts'`
Expected: FAIL — the Live radio is disabled/absent and the button reads "Deploy paper bot".

- [ ] **Step 4: The deploy form and its neighbours**

`alpaca-deploy-workflow.component.ts`:
- Line 66: `executionMode: Extract<DeployExecutionMode['mode'], 'dry_run' | 'paper' | 'shadow' | 'live'>;`
- Lines 251–266:

```ts
  /**
   * Which single broker-contacting mode this account's view offers: Paper on
   * a paper account, Shadow on a live one held by the Shadow Account
   * Authority, Live on one custodied by its live authority (ADR 0059 D2/D11).
   * Never two — the view's own `execution_modes` is the sole authority, and
   * there is no `'paper'` fallback once a live-world card is offered.
   */
  protected readonly brokerMode = computed<'paper' | 'shadow' | 'live'>(() => {
    const offered = (mode: 'shadow' | 'live') =>
      this.currentView()?.execution_modes.some(
        (candidate) => candidate.mode === mode && candidate.availability === 'available',
      ) ?? false;
    if (offered('live')) return 'live';
    if (offered('shadow')) return 'shadow';
    return 'paper';
  });

  /** The account's one broker world, as the access-grant copy words it. */
  protected readonly brokerModeLabel = computed<'Paper' | 'Shadow' | 'Live'>(() => {
    const mode = this.brokerMode();
    return mode === 'live' ? 'Live' : mode === 'shadow' ? 'Shadow' : 'Paper';
  });
```

- Lines 444–459 (the seeding effect): replace the `if (this.brokerMode() === 'shadow') {...}` body so a ticket opened on `'paper'` is moved to whichever broker mode the view offers: `const mode = this.brokerMode(); if (mode !== 'paper') { this.ticket.update((ticket) => ticket.executionMode === 'paper' ? { ...ticket, executionMode: mode } : ticket); }`.
- Line 594: `if (mode !== 'dry_run' && mode !== 'paper' && mode !== 'shadow' && mode !== 'live') return;`

`deploy-execution-section.component.ts:61-82`: in `strategyUnavailableReason`, the broker-mode branch becomes `if (mode.mode === 'paper' || mode.mode === 'shadow' || mode.mode === 'live') return this.brokerModeUnavailableReason();`; the comment at lines 30–39 loses its "Live stays governed by `availability` alone" sentence.

`deploy-paper-access.component.ts:48-55`: `readonly modeLabel = input.required<"Paper" | "Shadow" | "Live">();` and the docstring adds "`Live` on one custodied by its live authority".

`alpaca-deploy-drawer.component.ts:8-20`: the map becomes `Readonly<Record<'paper' | 'live', 'paper' | null>> = { paper: 'paper', live: null }` with the comment "A live account is shadowed or live-custodied; the drawer does not know which until the deploy view says, so it names no world rather than a wrong one (ADR 0059 D2/D11)." `brokerWorld`'s type stays `'paper' | 'shadow' | null`-compatible (`'paper' | null` is enough now); `headerLabel` and `accountNoun` already handle `null`.

`alpaca-operator-lens-data.service.ts:29`: `new Set(['real_paper', 'shadow', 'real_live'])`, and the comment at 24–27 gains "and `real_live` once the live authority is installed (slice 7)".

`trader-lens.component.ts:73`: rename `isPaperExecution` to `isBrokerExecution` (the predicate is `mode === 'trade'`, which is broker execution in any world); update `trader-lens.component.html:33` and every reference in `trader-lens.component.spec.ts`.

- [ ] **Step 5: Run the Frontend gates**

Run from `Frontend/`, one at a time:
- `npx ng test --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts'`
- `npx ng test --include='src/app/components/broker/broker-deploy-page/alpaca-deploy-drawer.component.spec.ts'`
- `npx ng test --include='src/app/components/broker/broker-deploy-page/deploy-execution-section.component.spec.ts'` (if the file exists)
- `npx ng test --include='src/app/components/brokers/alpaca-desk/alpaca-operator-lens-data.service.spec.ts'`
- `npx ng test --include='src/app/components/broker/v2-panel/trader-lens/trader-lens.component.spec.ts'`
- `npx eslint src/ --max-warnings 0`
- `npx tsc --noEmit`
- `npm run codegen:check`
Expected: every command exits 0.

- [ ] **Step 6: Commit**

```bash
git add contracts/openapi/python-data-service.openapi.json Frontend/src/app/api/broker.types.ts Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.ts Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.fixtures.ts Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-workflow.component.spec.ts Frontend/src/app/components/broker/broker-deploy-page/deploy-execution-section.component.ts Frontend/src/app/components/broker/broker-deploy-page/deploy-paper-access.component.ts Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-drawer.component.ts Frontend/src/app/components/broker/broker-deploy-page/alpaca-deploy-drawer.component.spec.ts Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-lens-data.service.ts Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-lens-data.service.spec.ts Frontend/src/app/components/broker/v2-panel/trader-lens/trader-lens.component.ts Frontend/src/app/components/broker/v2-panel/trader-lens/trader-lens.component.html Frontend/src/app/components/broker/v2-panel/trader-lens/trader-lens.component.spec.ts
git commit -m "feat(frontend): the deploy form offers Live, and no live surface says Paper (ADR 0059 slice 7, R12)

OpenAPI regenerated once for the three widened enums; the deploy form
derives its broker mode from the offered card with no paper fallback; the
drawer names no world it cannot know; the Operator lens lights on real_live.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Docs, the three pins the design promised, and the operator's environment truth (R14, R15, R16, R17)

**Files:**
- Create: `docs/references/alpaca-live-authority.md`
- Modify: `docs/references/alpaca-live-arming.md:21-25, 62-63, 72-74, 195-197, 237-238, 244-283`; `docs/references/alpaca-live-envelope.md:48-60, 80-84, 118, 186, 195-197`; `docs/references/alpaca-shadow-authority.md:22-27, 285-291`; `docs/architecture/engine-authority-map.md` (line 4 and the three ADR 0059 rows; one new row); `CONTEXT.md:464-467` and after line 505
- Modify: `PythonDataService/.env.example` (append), `compose.yaml:76-78`
- Test: `PythonDataService/tests/services/test_boot_recovery.py` (append, R15), `PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py` (append, R17), `PythonDataService/tests/broker/alpaca/test_fault_injection_live.py` (new, R14)

**Interfaces:** none new; this task writes prose and pins.

- [ ] **Step 1: The three pins**

Append to `tests/services/test_boot_recovery.py` (R15):

```python
async def test_a_live_primary_boots_with_shadow_sealed_bindings_present(tmp_path: Path) -> None:
    """R15: after graduation the rehearsal's bindings are foreign, not fatal."""
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.run_boot_recovery()
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    shadow_sealed = registry._bots[_SID].binding.sealed_account_id
    await registry.stop("alpaca", _SID, updated_by="test")

    proof = _custody_proof(exposure={}).model_copy(update={"account_id": f"live-of-{shadow_sealed}"})
    set_alpaca_clerk(_CustodyClerk(proof))
    rebooted = BotTaskRegistry(
        _artifacts_root(tmp_path),
        feed_resolver=lambda: feed,
        supported_broker_ids=frozenset({"alpaca"}),
        start_custody_guard=_flat_start_guard,
    )

    report = await rebooted.run_boot_recovery()

    assert report.authority_unavailable_instances == ()
    assert rebooted.status("alpaca", _SID).running is False
```

(`registry.stop`'s exact signature is the one `_stop_locked` wraps — read `bot_runner.py:930-960` and call it as the existing stop tests do.) If `_custody_proof` returns a `ClerkCustodySnapshot`, `model_copy` is available; if it returns a builder, apply the account id through its own keyword.

Append to `tests/contracts/test_alpaca_active_authority_wiring.py` (R17):

```python
def test_operator_reduce_only_paths_carry_no_mode_or_arming_gate() -> None:
    """ADR 0059 D3/D4 and slice 7 R17: a flatten is EXIT-shaped and is how a halted position closes."""
    for relative in ("broker/alpaca/clerk/sqlite/safe_flatten_execution.py", "broker/alpaca/clerk/sqlite/cohort_flatten.py"):
        source = (APPLICATION_ROOT / relative).read_text(encoding="utf-8")
        assert "account_mode" not in source, f"{relative} gates on the account mode; reduce-only actions never do"
        assert "arming" not in source, f"{relative} consults arming; reduce-only actions never do"
```

If either file lives elsewhere, find it with `grep -rl "def execute_safe_flatten\|cohort_flatten" app/` and use that path.

Create `tests/broker/alpaca/test_fault_injection_live.py` (R14):

```python
"""Fault injection never arms under live settings (ADR 0059 D10; slice 7 R14 verifies, changes nothing)."""

from __future__ import annotations

import pytest

from app.broker.alpaca import fault_injection
from tests.broker.alpaca.clerk.live_arming_fixtures import live_settings


def test_injection_is_refused_under_live_settings_even_with_the_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fault_injection.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fault_injection, "get_alpaca_settings", live_settings)
    assert fault_injection.injection_permitted() is False
```

- [ ] **Step 2: Run the pins**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_boot_recovery.py tests/contracts/test_alpaca_active_authority_wiring.py tests/broker/alpaca/test_fault_injection_live.py -q -p no:cacheprovider`
Expected: PASS (these pin behaviour that already holds; a failure is a finding, not a task).

- [ ] **Step 3: The reference note**

Create `docs/references/alpaca-live-authority.md`:

```markdown
# Alpaca live authority — graduation, the admission chain, and the first real order

**Status:** canonical for ADR 0059 slice 7 (2026-09-09). Lineage: live.

## What it is

The **Live Account Authority** is the same `sqlite` Clerk the paper account
runs, composed over a real-money account with the real trade port and the two
gates a real-money ENTER is admitted against: the risk envelope (ADR 0059 D4,
[alpaca-live-envelope](alpaca-live-envelope.md)) and the per-instance arming
gate (D3/D11, [alpaca-live-arming](alpaca-live-arming.md)). It exists only on
three-way mode agreement (D1): the configured mode, the broker-observed
account, and the cutover's activation record naming that exact account. This
is the slice after which a real order is possible, and this note says exactly
when one is.

## Graduation

A live account is **shadowed or live-custodied, never both at once** (design
R1): one primary authority per process. On a live-mode boot,
`active_authority.select_active_clerk_runtime` asks the cutover's
`ActivationStore` (`accounts/alpaca/`) for a record naming the observed
account. Present, `live_authority.select_live_clerk_runtime` composes the live
authority; absent, the Shadow Account Authority is composed exactly as slice 4
built it. Graduation is therefore the live cutover:

```bash
cd PythonDataService
# The evidence file names the account, `"account_mode": "live"`, flat
# positions and no open orders; the CLI refuses live evidence unless
# ALPACA_MODE=live, and the ceremony refuses unless at least one shadow
# receipt exists for the account (LIVE_SHADOW_INCOMPLETE).
python -m scripts.manage_alpaca_sqlite_clerk --account-id <LIVE> --artifacts-root <CLERK_DIR> \
    cutover-initialize --broker-evidence live-evidence.json --max-evidence-age-ms 600000 --runner-artifacts-root <RUNNER_ROOT>
python -m scripts.manage_alpaca_sqlite_clerk ... cutover-plan  --output plan.json ...
python -m scripts.manage_alpaca_sqlite_clerk ... cutover-apply --plan plan.json --confirmation-token <token> ...
```

A never-legacy live account has no legacy JSONL authority to quarantine; its
shadow receipt stands in for those artifacts (R3). The account must be **flat
and order-free** to graduate — the shadow authority never submitted, so any
position is a human's; flatten it by hand first. Stop the shadow instances
before the cutover: after it they are sealed on `shadow:<id>` while the
authority custodies `<id>`, so they are foreign to it — repaired from their own
lifecycle files at boot, refused `SEALED_ACCOUNT_MISMATCH` on any Start (R15).
Graduation has no reversal in this slice (`dev_reset` refuses live by D10); a
`LiveDeactivationRecord` mirroring disarm is the follow-up.

Cold start on the live authority is the paper path's (R18): the cutover's
flat-and-order-free evidence and `recover()`'s reconciliation of open orders
at every boot. The shadow namespace scan is not applied — after the first real
order it would refuse every boot.

## Where it runs

- `PythonDataService/app/broker/alpaca/clerk/live_authority.py::select_live_clerk_runtime`
  — the boot story. Refuses, as a typed `unavailable` runtime and never an
  aborted data plane: `LIVE_CONTROL_UNAUTHENTICATED`
  (`DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=true`, R14), `LIVE_MODE_DISAGREEMENT`,
  `LIVE_ENVELOPE_MISSING`, `ACTIVATION_RECORD_INVALID`, `SQLITE_CLERK_STARTUP_FAILED`.
- `app/broker/alpaca/clerk/live_arming_gate.py` — `ArmingSnapshot` and
  `ArmingGate`, the per-instance cache one ledger read fills.
- `app/broker/alpaca/clerk/sqlite/arming_admission.py::require_arming_admission`
  — the third ENTER admission.
- `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py` — every 15 s, one
  ledger read seals the envelope and publishes the snapshot; the runner's
  sealed bindings arrive through an injected callable (`instance_seals`).
- `app/services/live_arming_admission.py` — the arming fact Start and Resume read.

## The ENTER admission chain

`accept_enter` runs, inside the custody fence and before `ENTER_ACCEPTED`:

1. `require_admission` — holds and uncertainties, the loss hold included;
2. `require_arming_admission` — the instance must be `armed` **right now**
   (the snapshot is at most 45 s old; the status is derived at the admission
   instant, so a lapse at the ET-date boundary is enforced at the instant);
3. `require_envelope_admission` — the cash bound.

Arming runs before the envelope so an unarmed instance never reserves cash.
EXIT is never subject to 2 or 3. Refusals from 2, each a rejected receipt on
the decision:

| Code | When |
|---|---|
| `LIVE_ARMING_LEDGER_INVALID` | the last refresh could not verify the ledger |
| `LIVE_MODE_DISAGREEMENT` | the broker's mode stopped agreeing mid-session (R2) |
| `LIVE_ARMING_UNOBSERVED` | no snapshot, or one older than 45 s |
| `LIVE_ARMING_REQUIRED` | the ledger has never named this instance |
| `LIVE_ARMING_LAPSED`, `LIVE_ARMING_REVOKED`, `LIVE_ARMING_SEAL_CHANGED`, `LIVE_ARMING_FUTURE_DATED`, `LIVE_ENVELOPE_DISAGREEMENT` | the instance's own state (slice 6) |

All of them are transient (retry on the next decision clock); the reaction to
a lost arming is below, never a fatal halt.

## Deploy, arm, trade

A strategy instance is immutable per account and its account is inside its
seal, so the instance that rehearsed as `shadow:<id>` cannot be the instance
that trades on `<id>`: the live run is a **new instance** (R6).

1. Deploy it on the live world (`execution_mode: "live"`, offered only there).
   The launch is admitted running; every ENTER it makes refuses
   `LIVE_ARMING_REQUIRED` until it is armed, and the admitted decision and the
   receipt say so.
2. Arm it: `manage_alpaca_arming plan` / `apply`. The ceremony reads the
   live-sealed binding and finds the rehearsal's receipt **by twin identity**
   (`twins_agree`: configured signal, action plan, size, carryover policy) —
   a size change since the rehearsal is `LIVE_SHADOW_INCOMPLETE` (R7).
3. Within one sync tick the gate holds the instance `armed`; its next ENTER
   passes all three admissions and `submit_enter` hands the leg to the real
   trade port.

## The halt

Decision 8's `desired_state = PAUSED` is deliberately **not** written (R10):
`PAUSED` is observe-only for EXIT too, and would strand a real position every
morning under `ALPACA_LIVE_ARMING_MAX_SESSIONS=1`. What is built: new
submission stops (every ENTER of an instance that is no longer armed refuses,
as a rejected receipt); it is loud (the sync logs
`live_verdict_transition_halt` at warning level once per instance per
transition, naming the code, and the verdict names the instance); and
resumption is guarded by the arming ceremony itself, after which the next tick
admits. EXITs keep running; the operator's reduce-only actions
(`execute_safe_flatten`, the cohort flatten) are EXIT-shaped and never gated
(R17). The manual order ticket stays paper-only (D11).

## The thirteen gates, re-meant

See the table in design R8: `manual_order_runtime` keeps `LIVE_ACCOUNT_REFUSED`;
`historical_execution_recovery` admits live; `cutover` admits `paper | live`;
`dev_reset` still refuses non-paper on the configured mode; `panel_deploy`
offers `live` on the live world; the `run_admission` corpus gate is unchanged
and the arming fact sits beside it; `CustodyWorld` gains `real_live` and every
world admits exactly one account mode.

## In the live verdict

`observe_arming` and `observe_loss_hold` read on any facade authority. On the
live authority the headline is `LIVE account <id> — N instances armed,
real-money submission open` or `… — real-money authority installed, no
instance armed`; `clerk_authority` stays `sqlite` (`configured_mode` names the
world).

## Operator environment

The data plane reads **only** `PythonDataService/.env` (compose `env_file`);
the repo-root `.env` feeds compose interpolation only. One `ALPACA_MODE` line
and six `ALPACA_LIVE_*` lines, all in `PythonDataService/.env`;
`DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=false` and
`ALPACA_FAULT_INJECTION_ENABLED=false` on a live boot (the first refuses to
install the authority; the second is already refused by `injection_permitted`).

## Residuals

- One primary authority per process (R1): a graduated account cannot shadow
  a new instance until a secondary-authority seam exists.
- Graduation has no reversal (R1).
- The arming gate is a 15 s cache of local evidence (R5).
- The halt writes no desired state (R10) — owner question E1.
- A refused live boot labels its compat surfaces "paper" (`custody_world_or_paper`).
- A human trading the live account withdraws day P&L to unknown for the day
  (slice 5 R5), so every program ENTER refuses `LIVE_ENVELOPE_UNOBSERVED` — E7.
- `StrategySpec.submit_mode` is not stamped at deploy — E8.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decisions 1, 3, 8, 10, 11 and Consequence 7. Controller rulings R1–R18 are in
`docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md`.
Predecessors: [alpaca-shadow-authority](alpaca-shadow-authority.md),
[alpaca-live-envelope](alpaca-live-envelope.md), [alpaca-live-arming](alpaca-live-arming.md).
```

- [ ] **Step 4: The predecessors' sentences that go false**

`docs/references/alpaca-live-arming.md`:
- Lines 21–25 become: `**Since ADR 0059 slice 7 an arming record is what admits a real-money ENTER** on the live authority: ``require_arming_admission`` reads it at every ENTER, between the holds and the envelope, through the ``ArmingGate`` the sync refreshes each tick. The CLI's ``submission_admitted`` field now reports whether a live authority exists for the account; its ``note`` says under which authority the record is read. See [alpaca-live-authority](alpaca-live-authority.md).`
- Lines 62–63: replace "slice 7's at admission" with "the live authority's at admission (`LIVE_MODE_DISAGREEMENT` from the sync's read)".
- Lines 72–74: replace "slice 7's `real_live` custody will seal the live id itself" with "the live authority seals the live id itself".
- Lines 195–197 become: `Per-instance arming **is** consulted at ENTER admission on the live authority (slice 7); under the shadow authority the sealed envelope stays account-level and rehearsal ENTERs are not gated on arming.`
- Lines 237–238: replace "states that no path submits a real-money order yet" with "states whether real-money submission is open on this authority".
- Residuals 244–253 and 272–283 are resolved — replace each with one line naming the slice-7 unit that resolved it (`arming_admission.py`; the sync's `LIVE_MODE_DISAGREEMENT` path; `require_arming_admission`; `observe_arming` on every facade authority; `ArmingGate.invalidate` — an unreadable ledger is a refusal at admission).
- In `scripts/manage_alpaca_arming.py`, the `submission_admitted: false` literal and its `note` become a computed pair: `submission_admitted` is `True` iff the cutover's `ActivationStore(artifacts_root / "accounts" / "alpaca").latest(live_account_id)` is not `None`, and the `note` reads `"a live authority is activated for this account; an armed instance's ENTER is submitted"` or `"no live authority is activated for this account; nothing submits until the live cutover"`. Update `tests/scripts/test_manage_alpaca_arming.py`'s assertion on `report["note"]` (it pins `"Slice 7"`) to the new sentence.

`docs/references/alpaca-live-envelope.md`: lines 48–60 — replace "`real_live` custody stays unconstructible until slice 7, so every live-mode boot rehearses the envelope under the Shadow Account Authority instead" with "an activated live account composes the envelope on its live authority with `custody_is_simulated=False` (slice 7); an unactivated one rehearses it under the Shadow Account Authority"; lines 80–84 — "always true today" becomes "true under shadow; false on the live authority, whose broker cash already reflects the Clerk's own fills"; line 118 — "startup refusal of the Shadow Account Authority" becomes "startup refusal of the shadow and live authorities"; line 186 — "an authority that is not the shadow one (paper, synthetic, unavailable)" becomes "an authority with no envelope (paper, synthetic, unavailable)"; lines 195–197 — "slice 7 widens that one line" becomes "slice 7 widened it to every facade authority".

`docs/references/alpaca-shadow-authority.md`: lines 22–27 — after "when the broker-observed account resolves `account_mode == "live"`" add "and no live activation record exists for it; with one, the live authority is composed instead (graduation, [alpaca-live-authority](alpaca-live-authority.md))", and replace "`real_live` custody stays unconstructible until slice 7 (ruling R1)" with "`real_live` custody is the live authority's (slice 7)"; lines 285–291 — add a closing sentence to the operator recipe: "Graduation — the live cutover — is the step after the receipt; stop the shadow instances first, because after it they are foreign to the live authority."

`docs/architecture/engine-authority-map.md`: prepend to line 4's list `2026-09-09 (the live authority, graduation and the ENTER admission chain, ADR 0059 slice 7);`. In the shadow row (22) replace "routes every boot whose broker-observed `account_mode` is `live` to it" with "routes every live boot with no live activation record to it" and "`real_live` custody stays unconstructible until slice 7" with "an activated account boots its live authority instead (slice 7)"; in the arming row (65) replace "**An arming record admits nothing in this slice**: no path submits a real-money order until slice 7 reads it at ENTER admission." with "**An arming record is what admits a real-money ENTER** on the live authority (slice 7)."; in the envelope row (64) append "On the live authority the envelope bounds real custody (`custody_is_simulated=False`, slice 7)." Add one row after the arming row:

```
| Real-money live custody (ADR 0059 D1/D11) | **The Live Account Authority — the sqlite Clerk over a real account, admitting ENTER through holds → arming → envelope** | `PythonDataService/app/broker/alpaca/clerk/live_authority.py` (selection on three-way agreement), `app/broker/alpaca/clerk/live_arming_gate.py` (the snapshot the sync refreshes), `app/broker/alpaca/clerk/sqlite/arming_admission.py` (the third ENTER admission), `app/services/live_arming_admission.py` (the Start/Resume fact); consumers `sqlite/enter.py::accept_enter`, `sqlite/live_envelope_sync.py`, `services/alpaca_live_verdict.py`. | Graduation is the live cutover; a live instance is a new instance armed on its twin's receipt; a lost arming refuses ENTER and is logged once, and no desired state is written (R10). Cold start is the paper path's. | **canonical for ADR 0059 slice 7** — see [alpaca-live-authority](../references/alpaca-live-authority.md); validated by `tests/broker/alpaca/clerk/test_live_authority_runtime.py`, `test_live_arming_gate.py`, `sqlite/test_arming_admission.py`, `sqlite/test_live_envelope_sync_arming.py`, `sqlite/test_cutover_live.py`, `tests/services/test_live_arming_admission.py`, `tests/broker/v2panel/test_panel_deploy_live.py`, `tests/contracts/test_alpaca_active_authority_wiring.py`. |
```

`CONTEXT.md`: amend lines 464–467 to end with "Selected at boot when the account's live activation record exists — its **graduation** — and otherwise shadowed. _Avoid_: live mode, production account, real account, real_live custody (say *live authority*)". After the **Arming lapse** entry (line 505) add:

```
- **Graduation** — the live cutover: the supervised ceremony that writes a
  real-money account's activation record, after which the account boots its
  live authority instead of its shadow authority. It requires a shadow receipt
  for the account and a flat, order-free account. _Avoid_: going live, flipping
  to live, promotion
- **Arming gate** — the live authority's per-instance check at ENTER, between
  the holds and the risk envelope, that the instance is armed right now. Its
  evidence is a snapshot of the arming ledger and the sealed bindings the
  sync refreshes every tick. _Avoid_: arming check, arming lock
- **Live verdict transition halt** — what happens when an instance stops being
  armed while it runs: its ENTERs refuse under its arming code, the event is
  logged once, and the verdict names it. No desired state is written; its EXITs
  keep running. _Avoid_: kill switch, auto-pause, emergency stop
- **Live evidence namespace** — `live-evidence:<strategy_instance_id>`, the
  retained-bar ledger of one instance on the live authority; the same shape as
  `paper:` and `shadow-evidence:`, read under the same rule it is written under.
```

- [ ] **Step 5: The operator's environment truth**

Append to `PythonDataService/.env.example`:

```
# Alpaca (ADR 0059). The data plane reads THIS file (compose `env_file`), not the
# repo-root .env. Exactly one ALPACA_MODE line; every ALPACA_LIVE_* value is
# required when ALPACA_MODE=live and deliberately absent by default.
# ALPACA_API_KEY_ID=
# ALPACA_API_SECRET_KEY=
# ALPACA_MODE=paper
# ALPACA_LIVE_LOSS_FRACTION=
# ALPACA_LIVE_LOSS_USD=
# ALPACA_LIVE_SHADOW_SESSIONS=
# ALPACA_LIVE_ARMING_MAX_SESSIONS=
# ALPACA_LIVE_XH_ENTRY_BPS=
# ALPACA_LIVE_XH_EXIT_BPS=
# A live boot refuses to install its authority behind an open control plane.
# DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=false
```

In `compose.yaml` lines 76–78 the comment becomes: `# Alpaca's Pydantic settings file is not mounted into /app; its values pass through this env_file, so the running data plane reads ONLY PythonDataService/.env (ALPACA_MODE and every ALPACA_LIVE_* value belong there — the repo-root .env feeds compose interpolation only).`

- [ ] **Step 6: The docs contract and the arming CLI test**

Run: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts tests/scripts/test_manage_alpaca_arming.py -q -p no:cacheprovider`
Expected: PASS (`tests/contracts` guards every local docs link and the ADR index; the CLI test's `note` string was re-pinned in Step 4).

- [ ] **Step 7: Commit**

```bash
git add docs/references/alpaca-live-authority.md docs/references/alpaca-live-arming.md docs/references/alpaca-live-envelope.md docs/references/alpaca-shadow-authority.md docs/architecture/engine-authority-map.md CONTEXT.md PythonDataService/.env.example compose.yaml PythonDataService/scripts/manage_alpaca_arming.py PythonDataService/tests/scripts/test_manage_alpaca_arming.py PythonDataService/tests/services/test_boot_recovery.py PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py PythonDataService/tests/broker/alpaca/test_fault_injection_live.py
git commit -m "docs(alpaca): the live authority reference note, the predecessors' sentences, and the operator's env truth (ADR 0059 slice 7, R14–R17)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Gates on the final tree

- [ ] **Step 1: Python**

From `PythonDataService/`:
```bash
.venv/bin/ruff check app/ tests/ scripts/ && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca tests/services tests/routers tests/broker/v2panel tests/scripts tests/contracts -q -p no:cacheprovider -n auto && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main"
```
Expected: ruff 0 findings; every test passes (record the count); the contract is unchanged since Task 9; the import prints nothing. If `-n auto` is unavailable, drop it.

- [ ] **Step 2: Frontend**

From `Frontend/`: `npx eslint src/ --max-warnings 0 && npm run codegen:check && npx tsc --noEmit`, then the five specs from Task 9 Step 5 by exact path.
Expected: all exit 0.

- [ ] **Step 3: File sizes and the diff**

From `PythonDataService/`: `wc -l app/broker/alpaca/clerk/sqlite/runtime.py app/services/bot_runner.py app/broker/alpaca/clerk/sqlite/cutover.py app/services/broker_v2_panel/paper_deploy_service.py app/broker/alpaca/clerk/live_authority.py app/broker/alpaca/clerk/live_arming_gate.py app/broker/alpaca/clerk/sqlite/arming_admission.py app/services/live_arming_admission.py` and compare with the baseline `1491 / 1798 / 943 / 804`: `runtime.py` may have grown by at most 20 lines, `bot_runner.py` by at most 4, `cutover.py` by at most 35; every new module is under 300. Report the numbers in the PR body.
From the worktree root: `git diff --stat origin/master..HEAD` — every path must be one this plan names; a path it does not name was swept in from another session and must be removed from the branch before the PR.

- [ ] **Step 4: Hand off**

The branch is ready for the independent thermo review the repo requires before its first push (`feedback_pr_workflow`, `feedback_independent_review_never_self`): a fresh reviewer, read-only, over `git diff origin/master...HEAD`, with the design spec beside it. The PR body must name the three owner-facing rulings (R1, R10, R14), the eight owner questions E1–E8, the file-size numbers, and the operator's `.env` facts (two `ALPACA_MODE` lines, both flags `true`, the six `ALPACA_LIVE_*` lines in the wrong file).

---

## Self-review

**1. Spec coverage.** Every ruling maps to at least one task:

| Ruling | Task(s) |
|---|---|
| R1 (graduation as boot-time selection) | Task 4 — the fork on `store.latest(account.account_id)`; `test_a_live_account_with_no_activation_still_boots_the_shadow_authority` |
| R2 (three-way agreement; mid-session disagreement) | Task 4 (`select_live_clerk_runtime`'s first two refusals) and Task 3 (the sync's `BrokerAccountModeDisagreement` path, `ArmingGate.invalidate(reason_code=LIVE_MODE_DISAGREEMENT)`) |
| R3 (live cutover) | Task 5 |
| R4 (composition; no new runtime kind) | Task 4 and Task 3 (facade invariant) |
| R5 (the third admission; codes; transient) | Task 2 |
| R6 (launch admitted; UNREADABLE refuses; the note) | Task 6 |
| R7 (`live` mode; twin receipt) | Task 7 |
| R8 (the thirteen gates) | Tasks 1, 4, 5, 6, 7 — rows 3, 4, 6, 7, 8, 9, 11, 13; rows 1, 2, 5, 10, 12 unchanged by design (row 6, `historical_execution_recovery`, is **not** in a task — see Plan notes) |
| R9 (verdict) | Task 8 |
| R10 (the halt as refusal + warning) | Task 3 (`_note_transitions`) and Task 2 (the refusal) |
| R11 (codes; copy map unchanged) | Task 2 |
| R12 (contract; Frontend; compat; `submit_mode` untouched) | Tasks 1, 7, 9 |
| R13 (`live-evidence:`; reader = writer) | Task 1 |
| R14 (control refusal; fault injection verified) | Task 4 (refusal), Task 10 (pin) |
| R15 (foreign shadow bindings) | Task 10 (pin) |
| R16 (docs) | Task 10 |
| R17 (reduce-only paths) | Task 10 (pin) |
| R18 (cold start) | Task 4 (the structural pin's `verify_shadow_namespace_empty not in live_source`) |

**2. Placeholder scan.** Every code step carries the code. Three steps tell the implementer to *read* a neighbouring construction before mirroring it (`_broker_order_fixture`'s field list, the shadow spec's submit helper, the request fixture's sizing model) — each names the file and lines to read and states the assertions that must hold, and each exists because the exact identifier is on disk and this plan will not guess it.

**3. Type consistency.** `ArmingGate.invalidate(why, *, reason_code=)` (Task 2) is what Task 3's sync calls with `reason_code=LIVE_MODE_DISAGREEMENT`; `ArmingSnapshot.armed_instance_ids(now_ms)` (Task 2) is what Task 3's `_note_transitions` calls; `compose_repository_runtime(arming_gate=, instance_seals=)` (Task 3) is what Task 4's selector passes; `InstanceSeal.program` (Task 7) is what `twin_receipt` reads; `authority_kind_in_world` (Task 1) is imported by name in `sqlite_clerk_compat.py` and `run_replay_proof.py`; `live_settings`, `record_sealed_binding`, `seal_receipt`, `activate_shadow_fence`, `ARMED_AT_MS`, `ARMING_SID` are the slice-6 fixture names on disk; `_binding`, `RUN_ID` (`test_runtime_program_leg`), `_retain`, `DAY` (`test_shadow_broker`), `BAR_CLOSE`, `DECISION_MINUTE`, `NOW_MS`, `shadow_runtime` (`test_shadow_envelope_runtime`), `_ActivationStore` (`test_active_authority`) are imported exactly as the slice-5/6 tests import them.

## Plan notes for the controller

- **`historical_execution_recovery` (R8 #6) is not in a task.** The design says it admits live; the code change is two lines (`_require_paper_account` drops its mode refusal and keeps the identity check) plus the two `LIVE_ACCOUNT_REFUSED` pins in `test_historical_execution_recovery.py` gaining live counterparts. It was left out of Tasks 1–10 so that Task 7's commit, which is already the widest, does not absorb an unrelated module; **add it as Task 7b** before executing: modify `historical_execution_recovery.py:131-141`, rename the helper `_require_configured_account`, delete the `LIVE_ACCOUNT_REFUSED` branch, and in the test file turn the row `(_Read(activities=[_activity()], account_mode="live"), "LIVE_ACCOUNT_REFUSED")` into a passing recovery on a live read while keeping `test_historical_exact_execution_recovery_rechecks_paper_mode_on_confirm` as a *mismatch* test (`account_mode` differing between plan and confirm is still a refusal, under `BROKER_ACCOUNT_MISMATCH` or a renamed identity code — the reviewer decides the code's name).
- **`runtime.py` grows by roughly twenty lines (Task 3)** — the invariant, two properties, one keyword, one argument — on a file already over the limit. Every other addition went to new modules. The reviewer may ask for an extraction; the natural one is the facade's identity checks (lines 221–230 plus the new invariant) into `account_authority.require_facade_identity(authority_kind, account_id, account_mode, *, live_gates_present)`, which would net the file to −2.
- **The sync's `_note_transitions` judges at `snapshot.observed_at_ms`**, not at a later admission instant, so an instance that lapses between ticks is warned about at the next tick — 15 s late — while its ENTER already refuses at the instant. Consistent with R5's "the gate is a 15 s cache"; stated here so nobody reads the log's timestamp as the moment of lapse.
- **The recording trade port double (Task 4) accepts `submit(*args, **kwargs)`** because the port protocol's exact parameter names were read but the Clerk's call form (positional versus keyword) was not; the double answers either. If the fold rejects the returned order because `client_order_id` did not round-trip, the double's extraction is the first suspect.
- **Task 10's boot-recovery pin uses `registry.stop`** to make the deployed bot off duty before the reboot; if the existing tests express that with `_custody_proof` plus a cancelled task instead (as `test_boot_without_lifecycle_authority_leaves_stale_binding_unprojected` does), mirror that shape — the pin is about the reboot, not the stop.
- **Copy map (R11):** deliberately untouched, with the reviewer's own finding as the reason (no ENTER-refusal code is in `OPERATOR_COPY`; receipts render through `receiptLabel`). If the thermo reviewer disagrees, the change is the regenerator run plus one Frontend entry per code.
- **Owner questions E1–E8** ride the PR body verbatim from the design's Residuals; none blocks execution, and each names what the plan built so the owner's answer is a diff, not a design.

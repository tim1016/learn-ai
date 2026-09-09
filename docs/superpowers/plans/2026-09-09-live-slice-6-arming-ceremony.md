# ADR 0059 Slice 6 — Arming ceremony, ledger and lapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A supervised, per-instance arming ceremony — read-only `plan`, re-observing `apply`, a 120 s/300 s confirmation TTL — appends a sha256-sealed `LiveArmingRecord` to an append-only, account-rooted ledger; the record's envelope seals the runtime gate so a changed environment refuses every ENTER `LIVE_ENVELOPE_DISAGREEMENT`; the arming lapses after `ALPACA_LIVE_ARMING_MAX_SESSIONS` calendar NYSE sessions; and the live verdict finally tells the truth about how many instances are armed.

**Architecture:** Four new pure/near-pure modules under `app/broker/alpaca/clerk/`: a shared `ceremony.py` (plan token, TTL bounds and checks, lifted out of `cutover.py` so `cutover.py` shrinks), a pure `live_arming.py` (the two records, sha sealing, the codes, `sessions_used`, `arming_status`), a `live_arming_ledger.py` over the existing `sealed_ledger.py` discipline, and a `live_arming_ceremony.py` that observes settings + activation proof + sealed binding + shadow receipt behind one injectable seam. The already-running `LiveEnvelopeSync` re-reads the ledger each tick and assigns `LiveEnvelopeGate.sealed`, which turns slice 5's `envelope_agreement` from a permanent `unsealed` into a real gate. An operator CLI (`scripts/manage_alpaca_arming.py`) is the only writer.

**Tech Stack:** Python 3.12, Pydantic v2 (settings only), the Clerk's append-only JSONL sealed-ledger discipline, `pandas_market_calendars` behind the sealed `app/lean_sidecar/trading_calendar.py`, FastAPI (verdict endpoint, unchanged schema), Angular 22 + Vitest (one banner spec case).

**Spec:** `docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md` (binding; its Rulings R1–R14 are copied verbatim below), arguing from `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` **Decision 3** (arming is a supervised, per-instance ceremony that lapses on an operator-set count; lines 44–49) and **Decision 8** (the live verdict is server-derived, reactive and loud; lines 87–89). Predecessors whose vocabulary this plan reuses: `docs/references/alpaca-shadow-authority.md` (slice 4) and `docs/references/alpaca-live-envelope.md` (slice 5).

## Design rulings (binding)

Copied verbatim from the design document. Where the ADR decides, it decides; every choice below that the ADR does not make is a controller ruling, recorded with its cost if wrong.

- **R1 — What is sealed.** `LiveArmingRecord(schema_version=1, live_account_id, strategy_instance_id, seal_hash, configured_signal_hash, shadow_receipt_sha256, envelope_values, envelope_sha256, armed_at_ms, max_sessions, record_sha256)`. `record_sha256 = canonical_sha256` over every other field — a superset of Decision 3's seven (it adds `configured_signal_hash`, which ties the record to the receipt it names, and the six `envelope_values`, which make the sealed envelope auditable and let the runtime rebuild it). A reader recomputes the record sha and checks `envelope_sha256 == LiveEnvelopeValues(**envelope_values).sha`; either mismatch is `LiveArmingInvalid`. *Cost if wrong:* a stricter seal than the ADR's minimum.
- **R2 — The seal hash** is the instance's whole sealed-program hash (`SealedBotProgram.bot_configuration_hash`, exposed as `sealed_program_hash` by the binding repository), not the signal-only `configured_signal_hash`. Arming binds to what will trade — size, action plan and account included — so a change to any of them disarms. `configured_signal_hash` is carried beside it because the shadow receipt binds to that. *Cost if wrong:* a size-only change forces a re-arm (conservative).
- **R3 — The ledger** is `accounts/arming/<live_account_id>/live_arming.jsonl` under the Clerk artifacts root (`AlpacaSettings.clerk_dir`), one canonical JSON object per line, `{"kind": "armed", ...}` or `{"kind": "disarmed", ...}`, appended under the advisory file lock with the `sealed_ledger` helpers exactly as shadow receipts are, regular-file only, file order is time order. It lives in a dedicated tree, never inside a custody namespace directory, so no custody-detection path (cutover initialization, latent-database checks) can mistake it for an authority. *Cost if wrong:* none — a path.
- **R4 — Disarm** is `LiveDisarmRecord(schema_version=1, live_account_id, strategy_instance_id, revokes_record_sha256, disarmed_at_ms, record_sha256)`, a single append with no plan/apply: disarming is the closed direction. Status after it is `disarmed` with `LIVE_ARMING_REVOKED` until the instance is armed again. *Cost if wrong:* an operator has a first-class way to revoke before lapse; the ADR names none but forbids none.
- **R5 — Status derivation** is pure: `arming_status(records, *, live_account_id, strategy_instance_id, seal_hash, configured_envelope, now_ms) -> ArmingStatus(state, reason_code, record, sessions_used, sessions_remaining)`. The instance's latest record decides: none → `unarmed`; a disarm → `disarmed` / `LIVE_ARMING_REVOKED`; an arming record whose `seal_hash` differs from the current one → `disarmed` / `LIVE_ARMING_SEAL_CHANGED`; whose `envelope_sha256` differs from the configured envelope's sha → `disarmed` / `LIVE_ENVELOPE_DISAGREEMENT`; whose sessions used exceed `max_sessions` → `lapsed` / `LIVE_ARMING_LAPSED`; otherwise `armed`. Checks run in that order; the first failure names the state. States: `unarmed | armed | lapsed | disarmed`.
- **R6 — Lapse counting** uses the canonical NYSE calendar only: `sessions_used = trading_session_count(et_date_at_ms(armed_at_ms), et_date_at_ms(now_ms))` (inclusive of both ET dates; an arming on a non-trading day starts counting at the next trading day; `et_date_at_ms` from `app/utils/session_anchors.py`, `trading_session_count` from `app/lean_sidecar/trading_calendar.py`, both sealed, import only). Lapsed iff `sessions_used > max_sessions`; `sessions_remaining = max(0, max_sessions − sessions_used)`. The arming session counts as one. *Cost if wrong:* arming at 15:59 ET spends one session on a minute; the status shows `sessions_remaining`.
- **R7 — Re-arming while armed is allowed** (renewal before lapse); the new record supersedes. No `ALREADY_ARMED` refusal.
- **R8 — Ceremony inputs**, each re-observed by `plan` and again by `apply`: (a) settings — `ALPACA_MODE=live` and a complete envelope (`LiveEnvelopeValues.from_settings`), `live_arming_max_sessions`, `live_shadow_sessions`; (b) the live account id from the shadow activation proof under the artifacts root (the account the shadow gate was run against); (c) the instance's sealed binding on that account (`sealed_account_id == live_account_id`) — its `sealed_program_hash` and `configured_signal_hash`; (d) a current shadow receipt: `ShadowReceiptStore(artifacts_root).current(strategy_instance_id, configured_signal_hash=..., required_sessions=live_shadow_sessions)`. Refusals: settings not live or incomplete → `LIVE_ENVELOPE_MISSING` (reused); no activation proof or no matching sealed binding → `LIVE_ARMING_INSTANCE_UNSEALED`; no current receipt → `LIVE_SHADOW_INCOMPLETE`. The ceremony never contacts the broker. *Cost if wrong:* none for safety — arming grants nothing until slice 7's admission re-checks mode agreement on the live authority.
- **R9 — Plan and apply** follow `cutover.py`: `LiveArmingPlan(schema_version=1, plan_id, confirmation_token, created_at_ms, expires_at_ms, live_account_id, strategy_instance_id, seal_hash, configured_signal_hash, shadow_receipt_sha256, envelope_values, envelope_sha256, max_sessions)` with `plan_id == confirmation_token == canonical_sha256(payload without the two ids)`; `apply` checks the token with `secrets.compare_digest` (`LIVE_ARMING_TOKEN_INVALID`), the TTL (`LIVE_ARMING_PLAN_EXPIRED`), re-observes every input and refuses any drift (`LIVE_ARMING_INPUTS_CHANGED`), then appends the record with `armed_at_ms = clock()`. The plan token, `DEFAULT_CONFIRMATION_TTL_MS = 120_000`, `MAX_CONFIRMATION_TTL_MS = 300_000` and the TTL check move from `cutover.py` into one small shared module (`app/broker/alpaca/clerk/ceremony.py`) that both ceremonies import; `cutover.py` shrinks and its tests do not change.
- **R10 — Runtime sealing (shadow authority only in this slice).** `LiveEnvelopeSync.tick()` refreshes `LiveEnvelopeGate.sealed` from the ledger's latest *arming* record for the account (its `envelope_values`; `None` if there is none) and logs each transition once (`live_envelope_sealed` / `live_envelope_unsealed`, with the agreement). Effect: once any instance on the account has been armed, an environment change makes every rehearsal ENTER refuse `LIVE_ENVELOPE_DISAGREEMENT` through the existing `require_envelope_admission` until a re-arm. Per-instance arming is **not** consulted at admission (slice 7). *Cost if wrong:* slice 7 may replace the account-level `sealed` with per-instance evaluation at admission — one seam.
- **R11 — Verdict truth.** `armed_instance_count` = the number of instances bound to the live account whose `arming_status` is `armed` at verdict time; `envelope_state = "sealed"` iff the ledger holds at least one arming record for the account, else `configured_unsealed`; `final_verdict = "live-armed"` iff configured live, mode agreed, Clerk installed and `armed_instance_count ≥ 1`, else `live-unarmed`. Copy is backend prose: the headline names the count; the detail states that no path submits a real-money order yet and names any lapsed or disarmed instance with its reason code. The schema is unchanged. One banner spec case proves the `live-armed` rendering that already exists in the component.
- **R12 — Codes** live beside the model (`app/broker/alpaca/clerk/live_arming.py`), strings equal to their names, prefixed `LIVE_ARMING_` except the two reused (`LIVE_SHADOW_INCOMPLETE` is defined here too, as the ADR's name for the receipt refusal; `LIVE_ENVELOPE_DISAGREEMENT` and `LIVE_ENVELOPE_MISSING` are imported from `live_envelope.py`). Refusals raise `LiveArmingRefused(reason_code, message)`; corrupt ledger content raises `LiveArmingInvalid`.
- **R13 — CLI** `scripts/manage_alpaca_arming.py`, modelled on `manage_alpaca_shadow.py`: `status [--strategy-instance-id]` (one or every instance with a record), `plan --strategy-instance-id [--confirmation-ttl-ms]`, `apply --plan-file <path> --confirmation-token <token>`, `disarm --strategy-instance-id`; `--artifacts-root` and `--now-ms` for tests; one JSON object per invocation; exit 0 answered, 1 usage or evidence refusal, 2 the ceremony refused (`LIVE_SHADOW_INCOMPLETE`, `LIVE_ARMING_*`). The plan JSON is written to stdout and, with `--plan-out <path>`, to a file `apply` reads back. Every JSON object from `plan`, `apply` and `status` carries `"submission_admitted": false` with a `note` that no path submits a real-money order in this slice.
- **R14 — Docs.** New `docs/references/alpaca-live-arming.md` (ceremony, ledger path and record shape, states and codes, the lapse rule with a worked example across a weekend, CLI usage, residuals). `CONTEXT.md`: extend the **Arming** entry with the four states and add **Arming lapse**. `docs/references/alpaca-live-envelope.md`: agreement is now sealed by the ceremony. `docs/references/alpaca-shadow-authority.md`: `LIVE_SHADOW_INCOMPLETE` is now a real code. `docs/architecture/engine-authority-map.md`: one row.

## Global Constraints

- Never commit secrets; `.env` only. This slice adds **no new environment variable** — `ALPACA_LIVE_ARMING_MAX_SESSIONS` and `ALPACA_LIVE_SHADOW_SESSIONS` already exist in `app/broker/alpaca/config.py` and in `.env.example`. Do not add values anywhere.
- Never edit sealed artifacts: `app/lean_sidecar/trading_calendar.py`, `app/utils/timestamps.py`, `app/utils/session_anchors.py`, `app/engine/consolidators/trade_bar_consolidator.py`, anything in `registry.py`'s `artifact_paths`. **Importing from them is fine and is exactly what R6 requires.**
- No `facts.py` / hash-chained custody model changes. The arming ledger is a JSONL file outside the custody hash chain; no transition payload and no `facts_json` shape changes.
- **No SQLite schema change.** Nothing in this slice touches `app/broker/alpaca/clerk/sqlite/schema.py`; the schema version stays where slice 5 left it.
- **No OpenAPI contract change.** `AlpacaLiveVerdict` gains no field: every value slice 6 produces (`armed_instance_count`, `envelope_state="sealed"`, `final_verdict="live-armed"`) is already declared. `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check` is a gate in Tasks 7 and 9 and must report the snapshot **unchanged**. If it does not, you changed a schema — revert that, do not regenerate.
- The live TRADE port is never bound to any authority; the shadow authority keeps `NoSubmitAlpacaTradePort`. An arming record admits nothing in this slice.
- Explicit-path staging only: `git add <paths>` (never `git add -A`, never `git add .`, never `git stash` — this is a shared checkout). Never self-review. Pushing is allowed at the end; merging `origin/master` is the owner's.
- No silent exception handlers (`except: pass`, `except Exception: pass`). Structured logging only, `logger.<level>(message, extra={"action": "...", ...})`; no `print()` in `app/` (the CLI in `scripts/` writes its single JSON object through `sys.stdout.write`, exactly as `manage_alpaca_shadow.py` does).
- Temporal rigor: every temporal value is `int64 ms UTC`; every stamp comes from the injected `clock` / `repo.clock()` / the caller's `now_ms`, never a wall clock in a test; ET dates and session counts come only from `app/utils/session_anchors.py::et_date_at_ms` and `app/lean_sidecar/trading_calendar.py::trading_session_count`; `MAX_TIMESTAMP_MS` (not `2**63 - 1`) is the timestamp ceiling in every validator.
- **No file may cross 1,000 lines**, and `app/broker/alpaca/clerk/sqlite/cutover.py` (954 lines today) **must shrink** in Task 1 and must never grow. Each new module stays well under 1,000 lines and near the size this plan's code specifies (`ceremony.py` ~90, `live_arming.py` ~370, `live_arming_ledger.py` ~135, `live_arming_ceremony.py` ~440, `manage_alpaca_arming.py` ~320); a module materially larger than its figure means code landed in the wrong one — report it, do not split modules on your own.
- Reason codes are SCREAMING_SNAKE and prefixed `LIVE_ARMING_`, plus the three reused (`LIVE_SHADOW_INCOMPLETE`, `LIVE_ENVELOPE_DISAGREEMENT`, `LIVE_ENVELOPE_MISSING`).
- Python commands run from `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService`:
  - tests: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <paths> -q -p no:cacheprovider`
  - lint: `.venv/bin/ruff check app/ tests/ scripts/`
  - contract: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check`
  - import smoke: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main"`
- Frontend commands run from `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/Frontend`:
  - spec: `npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts'` (exact spec path, never a directory glob)
  - lint: `npx eslint src/ --max-warnings 0`
- Every commit message ends with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Known pre-existing failures to ignore: none. If a suite named in a task fails before your change, baseline it against `origin/master` and say so in the PR description.

---

## File map

| File | Responsibility |
|---|---|
| `app/broker/alpaca/clerk/ceremony.py` (new) | The two-step confirmation ceremony both `cutover.py` and arming share: `DEFAULT_CONFIRMATION_TTL_MS`, `MAX_CONFIRMATION_TTL_MS`, `require_confirmation_ttl_ms`, `plan_content_token`, `require_plan_token`, `require_unexpired`. |
| `app/broker/alpaca/clerk/sqlite/cutover.py` | Shrinks: imports the six names above instead of defining the constants, the TTL bound, the digest and the `compare_digest` check. Behaviour and digests byte-identical. |
| `app/broker/alpaca/clerk/live_arming.py` (new) | Pure: `LiveArmingRecord`, `LiveDisarmRecord`, sha sealing and verification, the reason codes, `LiveArmingRefused`, `LiveArmingInvalid`, `ArmingStatus`, `arming_status(...)`, `sessions_used(...)`. |
| `app/broker/alpaca/clerk/live_envelope.py` | Gains one canonical constant: `LIVE_ENVELOPE_MISSING`. |
| `app/broker/alpaca/clerk/shadow_authority.py` | Uses the imported `LIVE_ENVELOPE_MISSING`; builds the `LiveArmingLedger` for the live account and hands it to `compose_repository_runtime`. |
| `app/broker/alpaca/clerk/live_arming_ledger.py` (new) | `LiveArmingLedger(artifacts_root, live_account_id=...)`: `path`, `append`, `records`, `records_for`, `latest`, `latest_arming`, `sealed_envelope`, `instance_ids`. |
| `app/broker/alpaca/clerk/synthetic_activation.py` | `IsolatedActivationStore.account_ids()` — the accounts with an activation row, so the ceremony can *observe* the live account instead of being told it. |
| `app/broker/alpaca/clerk/live_arming_ceremony.py` (new) | `ArmingInputs`, `observe_arming_inputs`, `instance_seal_hashes`, `live_account_id_for`, `LiveArmingPlan`, `plan_arming`, `apply_arming`, `disarm`, `arming_status_for`. |
| `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py` | Refreshes `LiveEnvelopeGate.sealed` from the ledger on every tick, with one log line per transition and a fail-closed `LiveArmingInvalid` path. |
| `app/broker/alpaca/clerk/active_runtime.py` | `compose_repository_runtime(..., arming_ledger=)` → `LiveEnvelopeSync(arming_ledger=)`. |
| `app/services/alpaca_live_verdict.py`, `app/routers/brokers.py` | `ArmingObservation`, `observe_arming(...)`, the truthful count/state/verdict and its copy. |
| `scripts/manage_alpaca_arming.py` (new) | The operator entry point: `status`, `plan`, `apply`, `disarm`. |
| `Frontend/src/app/shell/alpaca-live-banner.component.spec.ts` | One `live-armed` rendering case. |
| `tests/broker/alpaca/clerk/live_arming_fixtures.py` (new) | One live account, one sealed binding, one shadow receipt, one activation proof, one live `AlpacaSettings` — shared by the ceremony, CLI, runtime and verdict tests. |
| `docs/references/alpaca-live-arming.md` (new), `docs/references/alpaca-live-envelope.md`, `docs/references/alpaca-shadow-authority.md`, `docs/architecture/engine-authority-map.md`, `CONTEXT.md` | Docs (R14). |

---

### Task 1: `clerk/ceremony.py` — one confirmation ceremony, and `cutover.py` shrinks onto it

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/ceremony.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/cutover.py` (lines 10–18 imports, 51–57 the `operational_files` import block, 77–78 the constants, 440–441 the TTL bound, 493–517 the plan payload/token/return, 534–535 the expiry check, 929–945 `_validate_plan_token`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_ceremony.py`

**Interfaces:**
- Consumes: `app.broker.alpaca.clerk.sqlite.operational_files.canonical_json_bytes(payload: dict[str, Any]) -> bytes` (canonical JSON **with a trailing newline** — this is what makes the cutover digest byte-identical after the move; it is *not* `sealed_ledger.canonical_sha256`).
- Produces (Tasks 4 and 5 rely on these exact names and signatures):
  - `DEFAULT_CONFIRMATION_TTL_MS = 120_000`, `MAX_CONFIRMATION_TTL_MS = 300_000`
  - `type Refusal = Callable[[str], Exception]`
  - `require_confirmation_ttl_ms(confirmation_ttl_ms: int, *, refused: Refusal) -> int`
  - `plan_content_token(payload: Mapping[str, Any]) -> str`
  - `require_plan_token(payload: Mapping[str, Any], *, plan_id: str, confirmation_token: str, supplied_token: str, refused: Refusal, label: str) -> None`
  - `require_unexpired(*, now_ms: int, expires_at_ms: int, refused: Refusal, label: str) -> None`

- [ ] **Step 1: Record the two numbers that must not change**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && wc -l app/broker/alpaca/clerk/sqlite/cutover.py && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/sqlite/test_cutover.py tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py -q -p no:cacheprovider
```
Expected: `954 app/broker/alpaca/clerk/sqlite/cutover.py`, and 39 passing cutover tests. Write both numbers down — Step 7 asserts the file got *smaller* and the same 39 tests still pass **with no edit to either test file**.

- [ ] **Step 2: Write the failing test**

`tests/broker/alpaca/clerk/test_ceremony.py`:

```python
"""The confirmation ceremony `cutover.py` invented and arming reuses (ADR 0059 D3)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    MAX_CONFIRMATION_TTL_MS,
    plan_content_token,
    require_confirmation_ttl_ms,
    require_plan_token,
    require_unexpired,
)


class _Refused(ValueError):
    """A caller's own refusal type, exactly as `CutoverRefused` is."""


PAYLOAD = {"schema_version": 1, "account_id": "9LIVE0001", "created_at_ms": 10, "expires_at_ms": 20}


def test_the_bounds_are_the_ones_cutover_shipped() -> None:
    assert (DEFAULT_CONFIRMATION_TTL_MS, MAX_CONFIRMATION_TTL_MS) == (120_000, 300_000)


def test_the_ttl_must_be_a_whole_millisecond_count_inside_the_bound() -> None:
    assert require_confirmation_ttl_ms(1, refused=_Refused) == 1
    assert require_confirmation_ttl_ms(MAX_CONFIRMATION_TTL_MS, refused=_Refused) == MAX_CONFIRMATION_TTL_MS
    for bad in (0, -1, MAX_CONFIRMATION_TTL_MS + 1, True, 1.0):
        with pytest.raises(_Refused, match=r"confirmation TTL must be within 1\.\.300000 ms"):
            require_confirmation_ttl_ms(bad, refused=_Refused)  # type: ignore[arg-type]


def test_the_token_is_the_payloads_own_canonical_digest_and_is_key_order_free() -> None:
    token = plan_content_token(PAYLOAD)
    assert len(token) == 64
    assert plan_content_token(dict(reversed(list(PAYLOAD.items())))) == token
    assert plan_content_token({**PAYLOAD, "created_at_ms": 11}) != token


def test_a_plan_that_does_not_hash_to_its_own_ids_is_refused_before_the_token_is_compared() -> None:
    token = plan_content_token(PAYLOAD)
    with pytest.raises(_Refused, match="cutover plan content hash does not verify"):
        require_plan_token(
            PAYLOAD,
            plan_id="0" * 64,
            confirmation_token=token,
            supplied_token=token,
            refused=_Refused,
            label="cutover",
        )
    with pytest.raises(_Refused, match="cutover plan content hash does not verify"):
        require_plan_token(
            PAYLOAD,
            plan_id=token,
            confirmation_token="0" * 64,
            supplied_token=token,
            refused=_Refused,
            label="cutover",
        )


def test_a_quoted_token_that_is_not_the_plans_is_refused_by_name() -> None:
    token = plan_content_token(PAYLOAD)
    require_plan_token(
        PAYLOAD, plan_id=token, confirmation_token=token, supplied_token=token, refused=_Refused, label="live arming"
    )
    with pytest.raises(_Refused, match="live arming confirmation token does not match the plan"):
        require_plan_token(
            PAYLOAD,
            plan_id=token,
            confirmation_token=token,
            supplied_token="0" * 64,
            refused=_Refused,
            label="live arming",
        )


def test_expiry_is_inclusive_of_the_last_admissible_millisecond() -> None:
    require_unexpired(now_ms=20, expires_at_ms=20, refused=_Refused, label="cutover")
    with pytest.raises(_Refused, match="cutover confirmation token has expired"):
        require_unexpired(now_ms=21, expires_at_ms=20, refused=_Refused, label="cutover")


def test_the_refusal_type_is_the_callers_own() -> None:
    """`LiveArmingRefused` takes (reason_code, message), so the seam is a callable."""

    class _Coded(ValueError):
        def __init__(self, reason_code: str, message: str) -> None:
            super().__init__(message)
            self.reason_code = reason_code

    with pytest.raises(_Coded) as caught:
        require_unexpired(
            now_ms=21,
            expires_at_ms=20,
            refused=lambda message: _Coded("LIVE_ARMING_PLAN_EXPIRED", message),
            label="live arming",
        )
    assert caught.value.reason_code == "LIVE_ARMING_PLAN_EXPIRED"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_ceremony.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.broker.alpaca.clerk.ceremony'`.

- [ ] **Step 4: Write `app/broker/alpaca/clerk/ceremony.py`**

```python
"""The two-step confirmation ceremony every supervised operator action shares.

``cutover.py`` invented the shape: a read-only ``plan`` whose content hash *is*
its confirmation token, a bounded confirmation window, and an ``apply`` that
re-checks both before it re-observes anything. Slice 6's arming ceremony (ADR
0059 D3) is the second one, so the bounds and the three checks live here once
instead of being copied -- a correction to any of them then lands in one place
rather than protecting only one ceremony.

Each caller keeps what is genuinely its own: its plan dataclass, the payload it
hashes, and the error it refuses with, passed in as ``refused`` and ``label``.
``refused`` is a callable rather than an exception class because the arming
ceremony's refusal carries a reason code as well as a message.

The digest is ``operational_files.canonical_json_bytes`` -- canonical JSON with
a trailing newline -- because that is the exact byte sequence ``cutover.py``
has always hashed. It is deliberately *not* ``sealed_ledger.canonical_sha256``,
which omits the newline: changing it would invalidate every plan token an
operator is holding.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from app.broker.alpaca.clerk.sqlite.operational_files import canonical_json_bytes

DEFAULT_CONFIRMATION_TTL_MS = 120_000
MAX_CONFIRMATION_TTL_MS = 300_000

type Refusal = Callable[[str], Exception]


def require_confirmation_ttl_ms(confirmation_ttl_ms: int, *, refused: Refusal) -> int:
    """Bound the operator-chosen confirmation window, or refuse.

    ``type(...) is not int`` rather than ``isinstance``: ``True`` is an ``int``
    and a boolean TTL is a caller bug, not a one-millisecond window.
    """
    if type(confirmation_ttl_ms) is not int or not 1 <= confirmation_ttl_ms <= MAX_CONFIRMATION_TTL_MS:
        raise refused(f"confirmation TTL must be within 1..{MAX_CONFIRMATION_TTL_MS} ms")
    return confirmation_ttl_ms


def plan_content_token(payload: Mapping[str, Any]) -> str:
    """The plan's own content hash, which is also its confirmation token."""
    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


def require_plan_token(
    payload: Mapping[str, Any],
    *,
    plan_id: str,
    confirmation_token: str,
    supplied_token: str,
    refused: Refusal,
    label: str,
) -> None:
    """Verify the plan hashes to its own two ids, and that the operator quoted them.

    The self-hash check comes first: a plan whose content no longer matches its
    ids is a forged or mutated plan, and comparing a token against it would
    answer a question about the wrong document.
    """
    expected = plan_content_token(payload)
    if plan_id != expected or confirmation_token != expected:
        raise refused(f"{label} plan content hash does not verify")
    if not secrets.compare_digest(supplied_token, expected):
        raise refused(f"{label} confirmation token does not match the plan")


def require_unexpired(*, now_ms: int, expires_at_ms: int, refused: Refusal, label: str) -> None:
    """Refuse a confirmation whose window has closed (inclusive of its last ms)."""
    if now_ms > expires_at_ms:
        raise refused(f"{label} confirmation token has expired")


__all__ = [
    "DEFAULT_CONFIRMATION_TTL_MS",
    "MAX_CONFIRMATION_TTL_MS",
    "Refusal",
    "plan_content_token",
    "require_confirmation_ttl_ms",
    "require_plan_token",
    "require_unexpired",
]
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_ceremony.py -q -p no:cacheprovider`
Expected: 7 passed.

- [ ] **Step 6: Shrink `cutover.py` onto it — five exact edits**

**Edit A — imports.** In the `from dataclasses import ...` line (line 16), add `replace`:

before: `from dataclasses import asdict, dataclass`
after: `from dataclasses import asdict, dataclass, replace`

Delete `import secrets` (line 14) — nothing else in the file uses it (confirm with `grep -n "secrets" app/broker/alpaca/clerk/sqlite/cutover.py` after the edit; expect no hits). Keep `import hashlib`: it is still used at lines ~370 and ~411.

Insert, in first-party import order (after the `from app.broker.alpaca.clerk.sqlite import writes` group's `activation` import and before `cutover_initialization`, i.e. sorted as `app.broker.alpaca.clerk.ceremony` which precedes every `app.broker.alpaca.clerk.sqlite.*` line — put it immediately above `from app.broker.alpaca.clerk.sqlite import writes`):

```python
from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    plan_content_token,
    require_confirmation_ttl_ms,
    require_plan_token,
    require_unexpired,
)
```

Do **not** import `MAX_CONFIRMATION_TTL_MS` — after Edit B nothing in `cutover.py` references it.

**Edit B — the constants and the TTL bound.** Delete these two lines (77–78):

```python
DEFAULT_CONFIRMATION_TTL_MS = 120_000
MAX_CONFIRMATION_TTL_MS = 300_000
```

In `plan_cutover`, replace these two lines (440–441):

```python
    if type(confirmation_ttl_ms) is not int or not 1 <= confirmation_ttl_ms <= MAX_CONFIRMATION_TTL_MS:
        raise CutoverRefused("confirmation TTL must be within 1..300000 ms")
```

with one:

```python
    require_confirmation_ttl_ms(confirmation_ttl_ms, refused=CutoverRefused)
```

**Edit C — the plan payload, token and return.** Replace the whole block from `    payload = {` (line 493) through the closing `    )` of `return CutoverPlan(...)` (line 517) with:

```python
    draft = CutoverPlan(
        schema_version=3,
        plan_id="",
        confirmation_token="",
        account_id=account_id,
        created_at_ms=now,
        expires_at_ms=now + confirmation_ttl_ms,
        initialization=initialization,
        database=database,
        broker_evidence=normalized_broker,
        runner_roster=runner_roster,
        legacy_artifacts=legacy,
    )
    token = plan_content_token(_plan_payload(draft))
    return replace(draft, plan_id=token, confirmation_token=token)
```

**Edit D — the expiry check.** In `apply_cutover`, replace these two lines (534–535, immediately after `_validate_plan_token(plan, confirmation_token)`):

```python
    if now > plan.expires_at_ms:
        raise CutoverRefused("cutover confirmation token has expired")
```

with one:

```python
    require_unexpired(now_ms=now, expires_at_ms=plan.expires_at_ms, refused=CutoverRefused, label="cutover")
```

**Edit E — `_plan_payload` and `_validate_plan_token`.** Replace the whole of `_validate_plan_token` (lines 929–945) with:

```python
def _plan_payload(plan: CutoverPlan) -> dict[str, Any]:
    """The plan's content, from which its two ids are derived.

    ``asdict`` reproduces the hand-written payload exactly: it recurses into
    the three nested evidence dataclasses and maps each tuple field to a
    sequence the canonical encoder writes as the same JSON array. The two ids
    are removed because they *are* the digest of what remains.
    """
    payload = asdict(plan)
    del payload["plan_id"], payload["confirmation_token"]
    return payload


def _validate_plan_token(plan: CutoverPlan, supplied_token: str) -> None:
    if plan.schema_version != 3:
        raise CutoverRefused("cutover plan content hash does not verify")
    require_plan_token(
        _plan_payload(plan),
        plan_id=plan.plan_id,
        confirmation_token=plan.confirmation_token,
        supplied_token=supplied_token,
        refused=CutoverRefused,
        label="cutover",
    )
```

- [ ] **Step 7: Prove the behaviour is unchanged and the file shrank**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && git diff --stat -- app/broker/alpaca/clerk/sqlite/cutover.py tests/ && wc -l app/broker/alpaca/clerk/sqlite/cutover.py && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_ceremony.py tests/broker/alpaca/clerk/sqlite/test_cutover.py tests/broker/alpaca/clerk/sqlite/test_cutover_cli.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main" && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: `git diff --stat` shows **no test file changed** (the 39 cutover tests are untouched — that is the proof the digest and every message are byte-identical); `wc -l` reports **953** (or anything below 954 — it must be lower than the number you wrote down in Step 1; if it is not, you skipped Edit C or E); 46 tests pass; `import app.main` clean; ruff clean.

- [ ] **Step 8: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6 && git add PythonDataService/app/broker/alpaca/clerk/ceremony.py PythonDataService/app/broker/alpaca/clerk/sqlite/cutover.py PythonDataService/tests/broker/alpaca/clerk/test_ceremony.py && git commit -m "refactor(arming): lift the confirmation ceremony out of cutover so arming can share it

cutover.py shrinks from 954 to 953 lines and its 39 tests are untouched:
the plan digest, the TTL bound and every refusal message are byte-identical.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `clerk/live_arming.py` — the sealed records, the codes, and the pure status rule

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/live_arming.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/live_envelope.py` (add `LIVE_ENVELOPE_MISSING` beside the other three codes, and to `__all__`)
- Modify: `PythonDataService/app/broker/alpaca/clerk/shadow_authority.py:73-81` (use the imported constant instead of the string literal)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_live_arming.py`

**Interfaces:**
- Consumes: `app.broker.alpaca.clerk.sealed_ledger.canonical_sha256(payload: dict[str, Any]) -> str`; `app.broker.alpaca.clerk.account_authority.require_real_account_id(account_id: str) -> str`; `app.broker.alpaca.clerk.live_envelope.LiveEnvelopeValues` (frozen dataclass with fields `loss_fraction: float, loss_usd: float, shadow_sessions: int, arming_max_sessions: int, xh_entry_bps: float, xh_exit_bps: float`, methods `to_mapping() -> dict[str, float | int]` and property `sha -> str`) and `LIVE_ENVELOPE_DISAGREEMENT`; `app.utils.session_anchors.MAX_TIMESTAMP_MS`, `et_date_at_ms(ms: int) -> date`; `app.lean_sidecar.trading_calendar.trading_session_count(start: date, end: date) -> int`.
- Produces (Tasks 3–7 rely on these exact names):
  - codes, each a `str` equal to its own name: `LIVE_ARMING_LAPSED`, `LIVE_ARMING_REVOKED`, `LIVE_ARMING_SEAL_CHANGED`, `LIVE_ARMING_INSTANCE_UNSEALED`, `LIVE_ARMING_TOKEN_INVALID`, `LIVE_ARMING_PLAN_EXPIRED`, `LIVE_ARMING_INPUTS_CHANGED`, `LIVE_ARMING_NOT_ARMED`, `LIVE_ARMING_TTL_INVALID`, `LIVE_SHADOW_INCOMPLETE`; re-exported `LIVE_ENVELOPE_DISAGREEMENT`, `LIVE_ENVELOPE_MISSING`; `ARMING_REASON_CODES: frozenset[str]` (all twelve)
  - `ArmingState = Literal["unarmed", "armed", "lapsed", "disarmed"]`
  - `class LiveArmingInvalid(ValueError)`
  - `class LiveArmingRefused(ValueError)` with `__init__(self, reason_code: str, message: str)` and attribute `reason_code: str`
  - `@dataclass(frozen=True) class LiveArmingRecord` with fields `kind: Literal["armed"]`, `schema_version: int`, `live_account_id: str`, `strategy_instance_id: str`, `seal_hash: str`, `configured_signal_hash: str`, `shadow_receipt_sha256: str`, `envelope_values: dict[str, float | int]`, `envelope_sha256: str`, `armed_at_ms: int`, `max_sessions: int`, `record_sha256: str`; classmethods `create(*, live_account_id, strategy_instance_id, seal_hash, configured_signal_hash, shadow_receipt_sha256, envelope: LiveEnvelopeValues, armed_at_ms: int, max_sessions: int) -> LiveArmingRecord` and `from_payload(payload: Mapping[str, Any]) -> LiveArmingRecord`; property `envelope -> LiveEnvelopeValues`
  - `@dataclass(frozen=True) class LiveDisarmRecord` with fields `kind: Literal["disarmed"]`, `schema_version: int`, `live_account_id: str`, `strategy_instance_id: str`, `revokes_record_sha256: str`, `disarmed_at_ms: int`, `record_sha256: str`; classmethods `create(*, live_account_id, strategy_instance_id, revokes_record_sha256, disarmed_at_ms) -> LiveDisarmRecord` and `from_payload(payload) -> LiveDisarmRecord`
  - `LedgerRecord = LiveArmingRecord | LiveDisarmRecord`
  - `sessions_used(*, armed_at_ms: int, now_ms: int) -> int`
  - `@dataclass(frozen=True) class ArmingStatus(state: ArmingState, reason_code: str | None, record: LiveArmingRecord | None, sessions_used: int, sessions_remaining: int)`
  - `arming_status(records: Sequence[LedgerRecord], *, live_account_id: str, strategy_instance_id: str, seal_hash: str | None, configured_envelope: LiveEnvelopeValues, now_ms: int) -> ArmingStatus`
  - in `live_envelope.py`: `LIVE_ENVELOPE_MISSING = "LIVE_ENVELOPE_MISSING"`

- [ ] **Step 1: Write the failing test**

`tests/broker/alpaca/clerk/test_live_arming.py`:

```python
"""The sealed arming record, its revocation, and the pure status rule (ADR 0059 D3)."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date

import pytest

from app.broker.alpaca.clerk.live_arming import (
    ARMING_REASON_CODES,
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_REVOKED,
    LIVE_ARMING_SEAL_CHANGED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_MISSING,
    ArmingStatus,
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    arming_status,
    sessions_used,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.lean_sidecar.trading_calendar import is_trading_day, trading_session_count
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import MAX_TIMESTAMP_MS

ACCOUNT = "9LIVE0001"
SID = "ema-shadow-1"
SEAL = "a" * 64
SIGNAL = "b" * 64
RECEIPT = "c" * 64
ENVELOPE = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)
# Friday 2026-09-11, 10:00 ET — a full NYSE session.
FRIDAY_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)
MONDAY_MS = et_minute_of_day_ms(date(2026, 9, 14), 10 * 60)
TUESDAY_MS = et_minute_of_day_ms(date(2026, 9, 15), 10 * 60)


def _armed(*, max_sessions: int = 20, armed_at_ms: int = FRIDAY_MS, seal: str = SEAL, instance: str = SID,
           account: str = ACCOUNT, envelope: LiveEnvelopeValues = ENVELOPE) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=account,
        strategy_instance_id=instance,
        seal_hash=seal,
        configured_signal_hash=SIGNAL,
        shadow_receipt_sha256=RECEIPT,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=max_sessions,
    )


def _disarmed(record: LiveArmingRecord, *, at_ms: int = MONDAY_MS) -> LiveDisarmRecord:
    return LiveDisarmRecord.create(
        live_account_id=record.live_account_id,
        strategy_instance_id=record.strategy_instance_id,
        revokes_record_sha256=record.record_sha256,
        disarmed_at_ms=at_ms,
    )


def _status(records: list[LedgerRecord], **overrides: object) -> ArmingStatus:
    kwargs: dict = {
        "live_account_id": ACCOUNT,
        "strategy_instance_id": SID,
        "seal_hash": SEAL,
        "configured_envelope": ENVELOPE,
        "now_ms": FRIDAY_MS,
    }
    kwargs.update(overrides)
    return arming_status(records, **kwargs)


def test_the_record_seals_every_field_and_round_trips() -> None:
    record = _armed()
    assert record.kind == "armed" and record.schema_version == 1
    assert len(record.record_sha256) == 64
    assert record.envelope_sha256 == ENVELOPE.sha
    assert record.envelope == ENVELOPE
    assert LiveArmingRecord.from_payload(asdict(record)) == record


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_account_id", "9LIVE0002"),
        ("strategy_instance_id", "other"),
        ("seal_hash", "d" * 64),
        ("configured_signal_hash", "d" * 64),
        ("shadow_receipt_sha256", "d" * 64),
        ("armed_at_ms", FRIDAY_MS + 1),
        ("max_sessions", 19),
    ],
)
def test_tampering_with_any_sealed_field_breaks_the_digest(field: str, value: object) -> None:
    payload = {**asdict(_armed()), field: value}
    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveArmingRecord.from_payload(payload)


def test_a_rewritten_envelope_is_caught_by_the_envelope_sha_before_the_digest() -> None:
    """R1's second check: the sealed values must still hash to the sealed sha."""
    record = _armed()
    payload = {**asdict(record), "envelope_values": {**record.envelope_values, "loss_usd": 4_000.0}}
    with pytest.raises(LiveArmingInvalid, match="envelope sha does not match"):
        LiveArmingRecord.from_payload(payload)


def test_a_reserved_namespace_account_is_never_a_live_account() -> None:
    with pytest.raises(LiveArmingInvalid, match="reserved account identity"):
        _armed(account=f"shadow:{ACCOUNT}")


def test_invalid_integer_and_hash_facts_are_refused() -> None:
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(max_sessions=0)
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(armed_at_ms=MAX_TIMESTAMP_MS + 1)
    assert _armed(armed_at_ms=MAX_TIMESTAMP_MS).armed_at_ms == MAX_TIMESTAMP_MS
    with pytest.raises(LiveArmingInvalid, match="invalid hash facts"):
        _armed(seal="not-a-sha")


def test_a_type_confused_row_leaves_by_this_modules_own_error() -> None:
    payload = {**asdict(_armed()), "armed_at_ms": str(FRIDAY_MS)}
    with pytest.raises(LiveArmingInvalid, match="invalid shape"):
        LiveArmingRecord.from_payload(payload)
    with pytest.raises(LiveArmingInvalid, match="invalid shape"):
        LiveArmingRecord.from_payload({"kind": "armed"})


def test_a_disarm_row_is_sealed_and_names_what_it_revokes() -> None:
    record = _armed()
    disarm = _disarmed(record)
    assert disarm.kind == "disarmed" and disarm.revokes_record_sha256 == record.record_sha256
    assert LiveDisarmRecord.from_payload(asdict(disarm)) == disarm
    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveDisarmRecord.from_payload({**asdict(disarm), "disarmed_at_ms": MONDAY_MS + 1})


def test_a_refusal_carries_its_reason_code() -> None:
    refusal = LiveArmingRefused(LIVE_ARMING_LAPSED, "the arming lapsed")
    assert refusal.reason_code == LIVE_ARMING_LAPSED
    assert str(refusal) == "the arming lapsed"
    assert isinstance(refusal, ValueError)


def test_every_code_is_its_own_name_and_the_set_is_closed() -> None:
    assert ARMING_REASON_CODES == frozenset(
        {
            "LIVE_ARMING_LAPSED",
            "LIVE_ARMING_REVOKED",
            "LIVE_ARMING_SEAL_CHANGED",
            "LIVE_ARMING_INSTANCE_UNSEALED",
            "LIVE_ARMING_TOKEN_INVALID",
            "LIVE_ARMING_PLAN_EXPIRED",
            "LIVE_ARMING_INPUTS_CHANGED",
            "LIVE_ARMING_NOT_ARMED",
            "LIVE_ARMING_TTL_INVALID",
            "LIVE_SHADOW_INCOMPLETE",
            "LIVE_ENVELOPE_DISAGREEMENT",
            "LIVE_ENVELOPE_MISSING",
        }
    )
    assert LIVE_ENVELOPE_MISSING == "LIVE_ENVELOPE_MISSING"


def test_the_arming_session_counts_as_one_and_a_weekend_spends_nothing() -> None:
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS) == 1
    # Saturday and Sunday are not sessions: Monday is only the second.
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=MONDAY_MS) == 2
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=TUESDAY_MS) == 3
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=MONDAY_MS) == trading_session_count(
        date(2026, 9, 11), date(2026, 9, 14)
    )


def test_a_market_holiday_spends_nothing_either() -> None:
    """Thanksgiving 2026-11-26 is a Thursday and not a session; 11-27 is a half day."""
    assert not is_trading_day(date(2026, 11, 26))
    armed = et_minute_of_day_ms(date(2026, 11, 25), 10 * 60)
    checked = et_minute_of_day_ms(date(2026, 11, 30), 10 * 60)
    expected = trading_session_count(date(2026, 11, 25), date(2026, 11, 30))
    assert expected == 3  # Wed 25, Fri 27 (half day), Mon 30
    assert sessions_used(armed_at_ms=armed, now_ms=checked) == expected


def test_a_clock_behind_the_arming_spends_nothing() -> None:
    """The calendar refuses a reversed range, and a rolled-back clock used no session."""
    assert sessions_used(armed_at_ms=MONDAY_MS, now_ms=FRIDAY_MS) == 0


def test_no_record_is_unarmed() -> None:
    assert _status([]) == ArmingStatus(
        state="unarmed", reason_code=None, record=None, sessions_used=0, sessions_remaining=0
    )


def test_the_latest_record_decides_and_a_re_arm_supersedes() -> None:
    first = _armed(max_sessions=2)
    second = _armed(max_sessions=5, armed_at_ms=MONDAY_MS)
    status = _status([first, second], now_ms=MONDAY_MS)
    assert (status.state, status.record) == ("armed", second)
    assert (status.sessions_used, status.sessions_remaining) == (1, 4)


def test_a_disarm_revokes_until_the_instance_is_armed_again() -> None:
    record = _armed()
    status = _status([record, _disarmed(record)], now_ms=MONDAY_MS)
    assert (status.state, status.reason_code, status.record) == ("disarmed", LIVE_ARMING_REVOKED, None)
    assert (status.sessions_used, status.sessions_remaining) == (0, 0)

    rearmed = _armed(armed_at_ms=MONDAY_MS)
    assert _status([record, _disarmed(record), rearmed], now_ms=MONDAY_MS).state == "armed"


def test_a_changed_seal_disarms_and_an_absent_binding_counts_as_changed() -> None:
    records = [_armed()]
    changed = _status(records, seal_hash="d" * 64)
    assert (changed.state, changed.reason_code) == ("disarmed", LIVE_ARMING_SEAL_CHANGED)
    absent = _status(records, seal_hash=None)
    assert (absent.state, absent.reason_code) == ("disarmed", LIVE_ARMING_SEAL_CHANGED)
    # The record is still reported so an operator can see what was armed.
    assert changed.record == records[0] and changed.sessions_used == 1


def test_a_changed_environment_disagrees_with_the_sealed_envelope() -> None:
    status = _status([_armed()], configured_envelope=replace(ENVELOPE, loss_usd=4_000.0))
    assert (status.state, status.reason_code) == ("disarmed", LIVE_ENVELOPE_DISAGREEMENT)


def test_the_arming_lapses_only_once_the_count_is_exceeded() -> None:
    records = [_armed(max_sessions=2)]
    on_the_last_session = _status(records, now_ms=MONDAY_MS)
    assert on_the_last_session.state == "armed"
    assert (on_the_last_session.sessions_used, on_the_last_session.sessions_remaining) == (2, 0)

    lapsed = _status(records, now_ms=TUESDAY_MS)
    assert (lapsed.state, lapsed.reason_code) == ("lapsed", LIVE_ARMING_LAPSED)
    assert (lapsed.sessions_used, lapsed.sessions_remaining) == (3, 0)


def test_the_checks_run_in_the_order_r5_fixes() -> None:
    """A record that fails three ways at once is reported by the first failure."""
    records = [_armed(max_sessions=1)]
    status = _status(
        records, seal_hash="d" * 64, configured_envelope=replace(ENVELOPE, loss_usd=4_000.0), now_ms=TUESDAY_MS
    )
    assert status.reason_code == LIVE_ARMING_SEAL_CHANGED
    envelope_first = _status(records, configured_envelope=replace(ENVELOPE, loss_usd=4_000.0), now_ms=TUESDAY_MS)
    assert envelope_first.reason_code == LIVE_ENVELOPE_DISAGREEMENT


def test_another_accounts_or_another_instances_record_never_answers_here() -> None:
    foreign_account = _armed(account="9LIVE0002")
    foreign_instance = _armed(instance="other")
    assert _status([foreign_account, foreign_instance]).state == "unarmed"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.broker.alpaca.clerk.live_arming'`.

- [ ] **Step 3: Give `LIVE_ENVELOPE_MISSING` one canonical definition**

In `app/broker/alpaca/clerk/live_envelope.py`, add the constant beside its three siblings (after `LIVE_ENVELOPE_DISAGREEMENT = "LIVE_ENVELOPE_DISAGREEMENT"` on line 35):

```python
# The composition refusal, defined here with the three admission codes so the
# arming ceremony imports it rather than repeating the literal (ADR 0059 D4).
LIVE_ENVELOPE_MISSING = "LIVE_ENVELOPE_MISSING"
```

Leave `ENVELOPE_ADMISSION_REASON_CODES` alone: `LIVE_ENVELOPE_MISSING` is a *composition* refusal, not an ENTER admission refusal. Add `"LIVE_ENVELOPE_MISSING",` to `__all__` in its sorted position (between `LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE` and `LIVE_ENVELOPE_UNOBSERVED`).

In `app/broker/alpaca/clerk/shadow_authority.py`, add `LIVE_ENVELOPE_MISSING` to the existing import at line 25:

```python
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_MISSING,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
```

and at line 75 replace the literal `"LIVE_ENVELOPE_MISSING",` with `LIVE_ENVELOPE_MISSING,`.

- [ ] **Step 4: Write `app/broker/alpaca/clerk/live_arming.py`**

```python
"""The sealed arming record, its revocation, and the pure status rule (ADR 0059 D3).

Formula: ``sessions_used = trading_session_count(ET date of armed_at_ms, ET date
  of now_ms)`` over the canonical NYSE calendar, inclusive of both dates;
  lapsed iff ``sessions_used > max_sessions``.
Reference: ADR 0059 Decision 3; design rulings R1, R2, R4, R5, R6, R12 in
  ``docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md``.
Canonical implementation: this file. The ledger that stores these records is
  ``live_arming_ledger.py``; the ceremony that mints them is
  ``live_arming_ceremony.py``; the calendar is ``app/lean_sidecar/trading_calendar.py``.
Validated against: ``tests/broker/alpaca/clerk/test_live_arming.py``.

Nothing here touches a broker, a database, a file or a clock: every function is
a pure fact about records the caller already read, judged at the caller's
``now_ms``. Arming binds to the instance's *whole* sealed-program hash (R2), so
a change to its size, action plan or account disarms it just as a change to its
signal would.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_MISSING,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.lean_sidecar.trading_calendar import trading_session_count
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

LIVE_ARMING_LAPSED = "LIVE_ARMING_LAPSED"
LIVE_ARMING_REVOKED = "LIVE_ARMING_REVOKED"
LIVE_ARMING_SEAL_CHANGED = "LIVE_ARMING_SEAL_CHANGED"
LIVE_ARMING_INSTANCE_UNSEALED = "LIVE_ARMING_INSTANCE_UNSEALED"
LIVE_ARMING_TOKEN_INVALID = "LIVE_ARMING_TOKEN_INVALID"
LIVE_ARMING_PLAN_EXPIRED = "LIVE_ARMING_PLAN_EXPIRED"
LIVE_ARMING_INPUTS_CHANGED = "LIVE_ARMING_INPUTS_CHANGED"
LIVE_ARMING_NOT_ARMED = "LIVE_ARMING_NOT_ARMED"
LIVE_ARMING_TTL_INVALID = "LIVE_ARMING_TTL_INVALID"
# ADR 0059 D2 names this refusal; slice 4 could only write it in prose because
# nothing read the receipt yet. This is the first code that does.
LIVE_SHADOW_INCOMPLETE = "LIVE_SHADOW_INCOMPLETE"

ARMING_REASON_CODES: frozenset[str] = frozenset(
    {
        LIVE_ARMING_LAPSED,
        LIVE_ARMING_REVOKED,
        LIVE_ARMING_SEAL_CHANGED,
        LIVE_ARMING_INSTANCE_UNSEALED,
        LIVE_ARMING_TOKEN_INVALID,
        LIVE_ARMING_PLAN_EXPIRED,
        LIVE_ARMING_INPUTS_CHANGED,
        LIVE_ARMING_NOT_ARMED,
        LIVE_ARMING_TTL_INVALID,
        LIVE_SHADOW_INCOMPLETE,
        LIVE_ENVELOPE_DISAGREEMENT,
        LIVE_ENVELOPE_MISSING,
    }
)

ArmingState = Literal["unarmed", "armed", "lapsed", "disarmed"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LiveArmingInvalid(ValueError):
    """An arming or disarm row cannot be trusted."""


class LiveArmingRefused(ValueError):
    """The ceremony refused, under one named reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class LiveArmingRecord:
    """One instance armed on one live account, under one sealed envelope (R1)."""

    kind: Literal["armed"]
    schema_version: int
    live_account_id: str
    strategy_instance_id: str
    seal_hash: str
    configured_signal_hash: str
    shadow_receipt_sha256: str
    envelope_values: dict[str, float | int]
    envelope_sha256: str
    armed_at_ms: int
    max_sessions: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        live_account_id: str,
        strategy_instance_id: str,
        seal_hash: str,
        configured_signal_hash: str,
        shadow_receipt_sha256: str,
        envelope: LiveEnvelopeValues,
        armed_at_ms: int,
        max_sessions: int,
    ) -> LiveArmingRecord:
        unsigned: dict[str, Any] = {
            "kind": "armed",
            "schema_version": 1,
            "live_account_id": live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "seal_hash": seal_hash,
            "configured_signal_hash": configured_signal_hash,
            "shadow_receipt_sha256": shadow_receipt_sha256,
            "envelope_values": envelope.to_mapping(),
            "envelope_sha256": envelope.sha,
            "armed_at_ms": armed_at_ms,
            "max_sessions": max_sessions,
        }
        record = cls(**unsigned, record_sha256=canonical_sha256(unsigned))
        _validate_armed(record)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LiveArmingRecord:
        return _from_payload(cls, payload, _validate_armed)

    @property
    def envelope(self) -> LiveEnvelopeValues:
        """The envelope this record sealed, rebuilt from its own values."""
        return LiveEnvelopeValues(**self.envelope_values)


@dataclass(frozen=True)
class LiveDisarmRecord:
    """One operator revocation, the closed direction of the ceremony (R4)."""

    kind: Literal["disarmed"]
    schema_version: int
    live_account_id: str
    strategy_instance_id: str
    revokes_record_sha256: str
    disarmed_at_ms: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        live_account_id: str,
        strategy_instance_id: str,
        revokes_record_sha256: str,
        disarmed_at_ms: int,
    ) -> LiveDisarmRecord:
        unsigned: dict[str, Any] = {
            "kind": "disarmed",
            "schema_version": 1,
            "live_account_id": live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "revokes_record_sha256": revokes_record_sha256,
            "disarmed_at_ms": disarmed_at_ms,
        }
        record = cls(**unsigned, record_sha256=canonical_sha256(unsigned))
        _validate_disarmed(record)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LiveDisarmRecord:
        return _from_payload(cls, payload, _validate_disarmed)


LedgerRecord = LiveArmingRecord | LiveDisarmRecord


def _unsigned(record: LedgerRecord) -> dict[str, Any]:
    payload = asdict(record)
    del payload["record_sha256"]
    return payload


def _from_payload(cls, payload: Mapping[str, Any], validate) -> Any:
    """Build, validate and verify one row, or leave by this module's error.

    Everything is inside the guard for the reason ``shadow_receipt.from_payload``
    states: a hand-edited row whose integer became a string must leave as
    ``LiveArmingInvalid``, not as a bare ``TypeError`` from a comparison.
    """
    try:
        record = cls(**payload)
        validate(record)
        if record.record_sha256 != canonical_sha256(_unsigned(record)):
            raise LiveArmingInvalid("live arming record digest does not verify")
    except LiveArmingInvalid:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveArmingInvalid("live arming record has an invalid shape") from exc
    return record


def _require_real(account_id: str) -> None:
    try:
        require_real_account_id(account_id)
    except ValueError as exc:
        raise LiveArmingInvalid("live arming record names a reserved account identity as real") from exc


def _require_hashes(*values: str) -> None:
    if any(not isinstance(value, str) or _SHA256.match(value) is None for value in values):
        raise LiveArmingInvalid("live arming record has invalid hash facts")


def _validate_armed(record: LiveArmingRecord) -> None:
    _require_real(record.live_account_id)
    if (
        record.kind != "armed"
        or record.schema_version != 1
        or not record.strategy_instance_id
        or record.max_sessions < 1
        or not 0 <= record.armed_at_ms <= MAX_TIMESTAMP_MS
    ):
        raise LiveArmingInvalid("live arming record has invalid integer or identity facts")
    _require_hashes(
        record.seal_hash,
        record.configured_signal_hash,
        record.shadow_receipt_sha256,
        record.envelope_sha256,
    )
    try:
        sealed = LiveEnvelopeValues(**record.envelope_values)
    except TypeError as exc:
        raise LiveArmingInvalid("live arming record's sealed envelope has an invalid shape") from exc
    if sealed.sha != record.envelope_sha256:
        raise LiveArmingInvalid("live arming record's envelope sha does not match its sealed values")


def _validate_disarmed(record: LiveDisarmRecord) -> None:
    _require_real(record.live_account_id)
    if (
        record.kind != "disarmed"
        or record.schema_version != 1
        or not record.strategy_instance_id
        or not 0 <= record.disarmed_at_ms <= MAX_TIMESTAMP_MS
    ):
        raise LiveArmingInvalid("live arming record has invalid integer or identity facts")
    _require_hashes(record.revokes_record_sha256)


def sessions_used(*, armed_at_ms: int, now_ms: int) -> int:
    """Calendar NYSE sessions spent since the arming, both ET dates inclusive (R6).

    The arming session counts as one, so an arming at 15:59 ET spends a whole
    session on a minute -- disclosed, and what ``sessions_remaining`` is for. An
    arming on a non-trading ET date starts counting at the next session, which
    is exactly what the inclusive count over the calendar already says.

    A ``now_ms`` behind the arming spends nothing: the calendar refuses a
    reversed range, and a clock that moved backwards has consumed no session.
    """
    armed_date = et_date_at_ms(armed_at_ms)
    now_date = et_date_at_ms(now_ms)
    if now_date < armed_date:
        return 0
    return trading_session_count(armed_date, now_date)


@dataclass(frozen=True)
class ArmingStatus:
    """One instance's arming state and the evidence behind it."""

    state: ArmingState
    reason_code: str | None
    record: LiveArmingRecord | None
    sessions_used: int
    sessions_remaining: int


def arming_status(
    records: Sequence[LedgerRecord],
    *,
    live_account_id: str,
    strategy_instance_id: str,
    seal_hash: str | None,
    configured_envelope: LiveEnvelopeValues,
    now_ms: int,
) -> ArmingStatus:
    """This instance's arming state, decided by its latest record alone (R5).

    The checks run in R5's order and the first failure names the state. Order is
    meaning, not optimisation: an instance whose seal changed *and* whose arming
    has lapsed is reported as seal-changed, because re-sealing is what its
    operator has to do first.

    ``seal_hash=None`` means no sealed binding for this instance exists on this
    account any more, which is a change from whatever was armed -- so it is
    ``LIVE_ARMING_SEAL_CHANGED``, never ``armed``.
    """
    latest: LedgerRecord | None = None
    for record in records:
        if record.live_account_id == live_account_id and record.strategy_instance_id == strategy_instance_id:
            latest = record
    if latest is None:
        return ArmingStatus(state="unarmed", reason_code=None, record=None, sessions_used=0, sessions_remaining=0)
    if isinstance(latest, LiveDisarmRecord):
        return ArmingStatus(
            state="disarmed", reason_code=LIVE_ARMING_REVOKED, record=None, sessions_used=0, sessions_remaining=0
        )
    used = sessions_used(armed_at_ms=latest.armed_at_ms, now_ms=now_ms)
    remaining = max(0, latest.max_sessions - used)
    if seal_hash is None or seal_hash != latest.seal_hash:
        return ArmingStatus(
            state="disarmed",
            reason_code=LIVE_ARMING_SEAL_CHANGED,
            record=latest,
            sessions_used=used,
            sessions_remaining=remaining,
        )
    if latest.envelope_sha256 != configured_envelope.sha:
        return ArmingStatus(
            state="disarmed",
            reason_code=LIVE_ENVELOPE_DISAGREEMENT,
            record=latest,
            sessions_used=used,
            sessions_remaining=remaining,
        )
    if used > latest.max_sessions:
        return ArmingStatus(
            state="lapsed",
            reason_code=LIVE_ARMING_LAPSED,
            record=latest,
            sessions_used=used,
            sessions_remaining=0,
        )
    return ArmingStatus(
        state="armed", reason_code=None, record=latest, sessions_used=used, sessions_remaining=remaining
    )


__all__ = [
    "ARMING_REASON_CODES",
    "LIVE_ARMING_INPUTS_CHANGED",
    "LIVE_ARMING_INSTANCE_UNSEALED",
    "LIVE_ARMING_LAPSED",
    "LIVE_ARMING_NOT_ARMED",
    "LIVE_ARMING_PLAN_EXPIRED",
    "LIVE_ARMING_REVOKED",
    "LIVE_ARMING_SEAL_CHANGED",
    "LIVE_ARMING_TOKEN_INVALID",
    "LIVE_ARMING_TTL_INVALID",
    "LIVE_ENVELOPE_DISAGREEMENT",
    "LIVE_ENVELOPE_MISSING",
    "LIVE_SHADOW_INCOMPLETE",
    "ArmingState",
    "ArmingStatus",
    "LedgerRecord",
    "LiveArmingInvalid",
    "LiveArmingRecord",
    "LiveArmingRefused",
    "LiveDisarmRecord",
    "arming_status",
    "sessions_used",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming.py tests/broker/alpaca/clerk/test_live_envelope.py tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main" && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: all pass (the two existing suites unchanged — `LIVE_ENVELOPE_MISSING`'s *value* did not move); `import app.main` clean; ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6 && git add PythonDataService/app/broker/alpaca/clerk/live_arming.py PythonDataService/app/broker/alpaca/clerk/live_envelope.py PythonDataService/app/broker/alpaca/clerk/shadow_authority.py PythonDataService/tests/broker/alpaca/clerk/test_live_arming.py && git commit -m "feat(arming): the sealed arming record, its revocation, and the pure status rule

Lapse is counted only over the canonical NYSE calendar: a weekend and a
market holiday spend no session, and the arming session counts as one.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `clerk/live_arming_ledger.py` — the append-only, account-rooted ledger

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/live_arming_ledger.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ledger.py`

**Interfaces:**
- Consumes (Task 2): `LiveArmingRecord`, `LiveDisarmRecord`, `LedgerRecord`, `LiveArmingInvalid`, `LiveArmingRecord.create(...)`, `LiveArmingRecord.from_payload(payload)`, `LiveDisarmRecord.from_payload(payload)`, `LiveArmingRecord.envelope -> LiveEnvelopeValues`. Consumes from the repo: `app.broker.alpaca.clerk.sealed_ledger.{append_canonical_jsonl_line, read_canonical_jsonl_objects}` (the caller holds the lock; both take `invalid=` and `label=`), `app.broker.alpaca.paths.{resolve_contained_path, safe_path_component}`, `app.utils.advisory_lock.advisory_file_lock`, `app.broker.alpaca.clerk.account_authority.require_real_account_id`.
- Produces (Tasks 4–7 rely on these exact names):
  - `LIVE_ARMING_FILENAME = "live_arming.jsonl"`
  - `class LiveArmingLedger` — `__init__(self, artifacts_root: Path, *, live_account_id: str)`; `@property path -> Path`; `@property live_account_id -> str`; `append(record: LedgerRecord) -> None`; `records() -> tuple[LedgerRecord, ...]`; `records_for(strategy_instance_id: str) -> tuple[LedgerRecord, ...]`; `latest(strategy_instance_id: str) -> LedgerRecord | None`; `latest_arming() -> LiveArmingRecord | None`; `sealed_envelope() -> LiveEnvelopeValues | None`; `instance_ids() -> tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

`tests/broker/alpaca/clerk/test_live_arming_ledger.py`:

```python
"""The append-only, account-rooted arming ledger (ADR 0059 D3, design R3)."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.live_arming_ledger as ledger_module
from app.broker.alpaca.clerk.live_arming import (
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.live_arming_ledger import LIVE_ARMING_FILENAME, LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.services.session_authority import et_minute_of_day_ms
from app.utils.advisory_lock import try_advisory_file_lock

ACCOUNT = "9LIVE0001"
OTHER_ACCOUNT = "9LIVE0002"
SID = "ema-shadow-1"
ENVELOPE = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)
FRIDAY_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)


def _armed(
    *,
    account: str = ACCOUNT,
    instance: str = SID,
    armed_at_ms: int = FRIDAY_MS,
    envelope: LiveEnvelopeValues = ENVELOPE,
) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=account,
        strategy_instance_id=instance,
        seal_hash="a" * 64,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="c" * 64,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=20,
    )


def _row_json(record: LiveArmingRecord) -> str:
    return json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def test_the_ledger_is_account_rooted_outside_every_custody_namespace(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)

    assert ledger.path == tmp_path / "accounts" / "arming" / ACCOUNT / LIVE_ARMING_FILENAME
    assert ledger.live_account_id == ACCOUNT
    # Not under accounts/alpaca/ and not inside a custody namespace directory:
    # no cutover or latent-database check can mistake this tree for an authority.
    assert "alpaca" not in ledger.path.parts
    assert ledger.records() == () and ledger.instance_ids() == ()
    assert ledger.latest(SID) is None and ledger.latest_arming() is None
    assert ledger.sealed_envelope() is None


def test_a_reserved_namespace_account_never_gets_a_ledger(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        LiveArmingLedger(tmp_path, live_account_id=f"shadow:{ACCOUNT}")


def test_append_and_read_keep_file_order_and_survive_a_reopen(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    first = _armed()
    second = _armed(instance="ema-shadow-2", armed_at_ms=FRIDAY_MS + 1)
    ledger.append(first)
    ledger.append(second)

    reopened = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    assert reopened.records() == (first, second)
    assert reopened.records_for(SID) == (first,)
    assert reopened.latest(SID) == first
    assert reopened.latest_arming() == second
    assert reopened.instance_ids() == (SID, "ema-shadow-2")
    assert len(reopened.path.read_text(encoding="utf-8").splitlines()) == 2


def test_the_sealed_envelope_is_the_latest_arming_records_own(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    assert ledger.sealed_envelope() == ENVELOPE

    tightened = replace(ENVELOPE, loss_usd=4_000.0)
    ledger.append(_armed(instance="ema-shadow-2", armed_at_ms=FRIDAY_MS + 1, envelope=tightened))
    assert ledger.sealed_envelope() == tightened


def test_a_disarm_never_unseals_the_account_level_envelope(tmp_path: Path) -> None:
    """R10 seals from the latest *arming* record; a revocation is per instance."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    record = _armed()
    ledger.append(record)
    ledger.append(
        LiveDisarmRecord.create(
            live_account_id=ACCOUNT,
            strategy_instance_id=SID,
            revokes_record_sha256=record.record_sha256,
            disarmed_at_ms=FRIDAY_MS + 1,
        )
    )

    assert isinstance(ledger.latest(SID), LiveDisarmRecord)
    assert ledger.latest_arming() == record
    assert ledger.sealed_envelope() == ENVELOPE


def test_a_foreign_accounts_row_is_ignored_rather_than_answered_for(tmp_path: Path) -> None:
    """The tree is account-rooted, so a foreign row can only be hand-planted."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8") + _row_json(_armed(account=OTHER_ACCOUNT)),
        encoding="utf-8",
    )

    assert ledger.records() == (_armed(),)
    assert ledger.instance_ids() == (SID,)


def test_appending_another_accounts_record_is_refused_before_the_write(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)

    with pytest.raises(LiveArmingInvalid, match="belongs to another live account"):
        ledger.append(_armed(account=OTHER_ACCOUNT))

    assert not ledger.path.exists()


def test_a_symlinked_ledger_is_refused_on_both_read_and_append(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "elsewhere.jsonl").write_text("", encoding="utf-8")
    ledger.path.symlink_to(tmp_path / "elsewhere.jsonl")

    with pytest.raises(LiveArmingInvalid, match="must be a regular file"):
        ledger.records()
    with pytest.raises(LiveArmingInvalid, match="must be a regular file"):
        ledger.append(_armed())


def test_a_tampered_row_is_refused_rather_than_read(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"max_sessions":20', '"max_sessions":90'),
        encoding="utf-8",
    )

    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        ledger.records()


def test_a_row_with_no_recognised_kind_is_refused(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"kind":"armed"', '"kind":"maybe"'),
        encoding="utf-8",
    )

    with pytest.raises(LiveArmingInvalid, match="unrecognised kind"):
        ledger.records()


def test_the_append_holds_the_advisory_lock_across_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two writers appending at once must not interleave a row.

    ``flock`` binds to the open file description, so a second ``open`` in this
    same process is a genuine second contender: if the lock were not held here,
    the probe below would acquire it.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    observed: list[bool] = []
    real_append = ledger_module.append_canonical_jsonl_line

    def _probe(path: Path, payload: dict, *, invalid: type[ValueError], label: str) -> None:
        with try_advisory_file_lock(path) as acquired:
            observed.append(acquired)
        real_append(path, payload, invalid=invalid, label=label)

    monkeypatch.setattr(ledger_module, "append_canonical_jsonl_line", _probe)
    ledger.append(_armed())

    assert observed == [False], "append must hold the advisory lock while it writes"
    assert len(ledger.records()) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming_ledger.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.broker.alpaca.clerk.live_arming_ledger'`.

- [ ] **Step 3: Write `app/broker/alpaca/clerk/live_arming_ledger.py`**

```python
"""The append-only arming ledger for one live account (ADR 0059 D3, design R3).

``accounts/arming/<live_account_id>/live_arming.jsonl`` under the Clerk
artifacts root: one canonical JSON object per line, each sha256-sealed by
``live_arming.py``, appended under the advisory file lock with the same
``sealed_ledger`` discipline the shadow receipts use. File order is time order.

The tree is deliberately its own -- not ``accounts/alpaca/<id>/`` and not inside
a custody namespace directory -- so no custody-detection path (cutover
initialization, the latent-database checks) can mistake an arming ledger for an
authority. It is a sibling of ``accounts/shadow/`` and ``accounts/synthetic/``,
which is the shape those paths already tolerate.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_arming import (
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sealed_ledger import (
    append_canonical_jsonl_line,
    read_canonical_jsonl_objects,
)
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.utils.advisory_lock import advisory_file_lock

LIVE_ARMING_FILENAME = "live_arming.jsonl"
_LABEL = "live arming"
_ARMING_DIR = "arming"


def _verified_payload(record: LedgerRecord) -> dict[str, Any]:
    """What a reader will accept, re-derived from what a writer is about to write.

    The shadow receipt store's rule: a malformed record is refused before it
    reaches the file, rather than poisoning every later read of the ledger.
    """
    payload = asdict(record)
    verified: LedgerRecord = (
        LiveArmingRecord.from_payload(payload)
        if isinstance(record, LiveArmingRecord)
        else LiveDisarmRecord.from_payload(payload)
    )
    return asdict(verified)


class LiveArmingLedger:
    """Every arming and disarm row for one live account, in file order."""

    def __init__(self, artifacts_root: Path, *, live_account_id: str) -> None:
        self._live_account_id = require_real_account_id(live_account_id)
        self._path = resolve_contained_path(
            artifacts_root,
            "accounts",
            _ARMING_DIR,
            safe_path_component(self._live_account_id, "live account id"),
            LIVE_ARMING_FILENAME,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def live_account_id(self) -> str:
        return self._live_account_id

    def append(self, record: LedgerRecord) -> None:
        """Durably append one sealed row, under the lock, after re-verifying it."""
        if record.live_account_id != self._live_account_id:
            raise LiveArmingInvalid(f"{_LABEL} record belongs to another live account than this ledger's")
        canonical = _verified_payload(record)
        with advisory_file_lock(self._path):
            append_canonical_jsonl_line(self._path, canonical, invalid=LiveArmingInvalid, label=_LABEL)

    def records(self) -> tuple[LedgerRecord, ...]:
        """Every row this ledger's account owns, in file order.

        A row naming another account is ignored, not raised on: the tree is
        account-rooted, so such a row can only have been planted by hand, and
        one planted row must not make this account unreadable. A row whose
        ``kind`` is neither of the two *is* fatal -- it is a shape nothing here
        wrote, and guessing at it would be inventing custody evidence.
        """
        rows: list[LedgerRecord] = []
        for payload in read_canonical_jsonl_objects(self._path, invalid=LiveArmingInvalid, label=_LABEL):
            kind = payload.get("kind")
            if kind == "armed":
                record: LedgerRecord = LiveArmingRecord.from_payload(payload)
            elif kind == "disarmed":
                record = LiveDisarmRecord.from_payload(payload)
            else:
                raise LiveArmingInvalid(f"{_LABEL} record has an unrecognised kind")
            if record.live_account_id == self._live_account_id:
                rows.append(record)
        return tuple(rows)

    def records_for(self, strategy_instance_id: str) -> tuple[LedgerRecord, ...]:
        return tuple(row for row in self.records() if row.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> LedgerRecord | None:
        rows = self.records_for(strategy_instance_id)
        return rows[-1] if rows else None

    def latest_arming(self) -> LiveArmingRecord | None:
        """The account's newest arming record, ignoring revocations (R10).

        A disarm withdraws one instance's permission; it does not unseal the
        account's envelope, which stays whatever the last arming ceremony read
        out of the environment until another ceremony replaces it.
        """
        armings = [row for row in self.records() if isinstance(row, LiveArmingRecord)]
        return armings[-1] if armings else None

    def sealed_envelope(self) -> LiveEnvelopeValues | None:
        latest = self.latest_arming()
        return None if latest is None else latest.envelope

    def instance_ids(self) -> tuple[str, ...]:
        """Every instance with a row here, in first-appearance order."""
        return tuple(dict.fromkeys(row.strategy_instance_id for row in self.records()))


__all__ = ["LIVE_ARMING_FILENAME", "LiveArmingLedger"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming_ledger.py tests/broker/alpaca/clerk/test_live_arming.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add PythonDataService/app/broker/alpaca/clerk/live_arming_ledger.py PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ledger.py
git commit -m "feat(arming): the append-only, account-rooted arming ledger

Its own tree under accounts/arming/, never inside a custody namespace, so
no cutover or latent-database check can read it as an authority.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `clerk/live_arming_ceremony.py` — observe, plan, apply, disarm

**Files:**
- Create: `PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py` (add `IsolatedActivationStore.account_ids()`, after `latest()` at line 152–161)
- Create: `PythonDataService/tests/broker/alpaca/clerk/live_arming_fixtures.py`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ceremony.py`

**Interfaces:**
- Consumes (Task 1): `DEFAULT_CONFIRMATION_TTL_MS`, `plan_content_token(payload) -> str`, `require_confirmation_ttl_ms(ttl, *, refused)`, `require_plan_token(payload, *, plan_id, confirmation_token, supplied_token, refused, label)`, `require_unexpired(*, now_ms, expires_at_ms, refused, label)` — every `refused` is `Callable[[str], Exception]`.
- Consumes (Task 2): `LiveArmingRecord.create(...)`, `LiveDisarmRecord.create(...)`, `LiveArmingRefused(reason_code, message)`, `ArmingStatus`, `arming_status(...)`, and the codes `LIVE_ARMING_INPUTS_CHANGED`, `LIVE_ARMING_INSTANCE_UNSEALED`, `LIVE_ARMING_NOT_ARMED`, `LIVE_ARMING_PLAN_EXPIRED`, `LIVE_ARMING_TOKEN_INVALID`, `LIVE_ARMING_TTL_INVALID`, `LIVE_ENVELOPE_MISSING`, `LIVE_SHADOW_INCOMPLETE`.
- Consumes (Task 3): `LiveArmingLedger(artifacts_root, live_account_id=...)` with `append`, `records`, `latest`, `instance_ids`.
- Consumes from the repo: `ShadowActivationStore(artifacts_root)`; `ShadowReceiptStore(artifacts_root).current(sid, *, configured_signal_hash, required_sessions) -> ShadowReceipt | None` (the receipt's digest field is `receipt_sha256`); `live_state_binding_repository(artifacts_root) -> BotBindingRepository` with `.list_for_broker("alpaca") -> list[BrokerBotBinding]` (fields used: `strategy_instance_id`, `sealed_account_id: str | None`, `sealed_program: SealedBotProgram | None` whose fields are `bot_configuration_hash` and `configured_signal_hash`); `shadow_account_id_for_live_account`, `SHADOW_ACCOUNT_PREFIX`; `LiveEnvelopeValues.from_settings(settings)`, `LiveEnvelopeIncomplete`; `AlpacaSettings`; `Clock`, `now_ms_utc`.
- Produces (Tasks 5 and 7 rely on these exact names):
  - `@dataclass(frozen=True) class InstanceSeal(seal_hash: str, configured_signal_hash: str)`
  - `@dataclass(frozen=True) class ArmingInputs(live_account_id: str, strategy_instance_id: str, seal_hash: str, configured_signal_hash: str, shadow_receipt_sha256: str, envelope: LiveEnvelopeValues, max_sessions: int)`
  - `type ArmingObserver = Callable[..., ArmingInputs]`
  - `custody_account_ids_for(live_account_id: str) -> frozenset[str]`
  - `live_account_id_for(artifacts_root: Path) -> str`
  - `instance_seal_hashes(*, live_account_id: str, live_state_root: Path) -> dict[str, InstanceSeal]`
  - `observe_arming_inputs(*, strategy_instance_id: str, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings) -> ArmingInputs`
  - `@dataclass(frozen=True) class LiveArmingPlan(schema_version: int, plan_id: str, confirmation_token: str, created_at_ms: int, expires_at_ms: int, live_account_id: str, strategy_instance_id: str, seal_hash: str, configured_signal_hash: str, shadow_receipt_sha256: str, envelope_values: dict[str, float | int], envelope_sha256: str, max_sessions: int)`
  - `plan_arming(*, strategy_instance_id: str, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings, confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS, clock: Clock = now_ms_utc, observe: ArmingObserver = observe_arming_inputs) -> LiveArmingPlan`
  - `apply_arming(*, plan: LiveArmingPlan, confirmation_token: str, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings, clock: Clock = now_ms_utc, observe: ArmingObserver = observe_arming_inputs) -> LiveArmingRecord`
  - `disarm(*, strategy_instance_id: str, artifacts_root: Path, clock: Clock = now_ms_utc) -> LiveDisarmRecord`
  - `account_arming_statuses(*, live_account_id: str, artifacts_root: Path, live_state_root: Path, configured_envelope: LiveEnvelopeValues, now_ms: int, strategy_instance_ids: Sequence[str] | None = None) -> dict[str, ArmingStatus]`
  - in `synthetic_activation.py`: `IsolatedActivationStore.account_ids(self) -> tuple[str, ...]`
  - in `tests/broker/alpaca/clerk/live_arming_fixtures.py`: `ARMING_SID`, `ARMED_AT_MS`, `live_settings(**overrides) -> AlpacaSettings`, `paper_settings() -> AlpacaSettings`, `activate_shadow_fence(artifacts_root, *, live_account_id=LIVE_ACCT, activated_at_ms=ARMED_AT_MS) -> ShadowActivationRecord`, `sealed_program(*, strategy_instance_id=ARMING_SID, sealed_account_id=SHADOW_ACCT, quantity=1) -> SealedBotProgram`, `record_sealed_binding(live_state_root, *, strategy_instance_id=ARMING_SID, sealed_account_id=SHADOW_ACCT, quantity=1) -> SealedBotProgram`, `seal_receipt(artifacts_root, *, configured_signal_hash, live_account_id=LIVE_ACCT, strategy_instance_id=ARMING_SID, sessions=1, written_at_ms=...) -> ShadowReceipt`, `arming_ready(artifacts_root, live_state_root, *, strategy_instance_id=ARMING_SID) -> SealedBotProgram`

- [ ] **Step 1: Write the shared fixture module**

`tests/broker/alpaca/clerk/live_arming_fixtures.py`:

```python
"""One live account, one sealed instance, one shadow receipt, one activation proof.

The evidence the arming ceremony observes (ADR 0059 D3 R8), laid down with the
repository's own writers so a test exercises the real readers instead of a mock
of them.

Not a conftest: these builders are imported by name from
``tests/broker/alpaca/clerk/``, ``tests/scripts/`` and ``tests/services/``,
which share no conftest -- the same reason ``live_envelope_fixtures`` is a plain
module. They extend that module rather than restating it, so every slice-6 test
and every slice-5 test describe the same live account.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import shadow_account_id_for_live_account
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationRecord, ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import (
    ShadowReceipt,
    ShadowReceiptSession,
    ShadowReceiptStore,
)
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ExitEligibilityContract,
    NumericalProvenanceContract,
    ResolvedSignalParameter,
    SealedBotProgram,
    SignalBarIntegrityContract,
    SignalClockContract,
    SignalDataContract,
    SignalSeriesContract,
    seal_bot_program,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
    live_state_binding_repository,
)
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    SHADOW_ACCT,
    TEST_ENVELOPE_VALUES,
)

ARMING_SID = "ema-shadow-1"
# Friday 2026-09-11, 10:00 ET -- a full NYSE session, so the arming spends
# exactly one session and the weekend that follows spends none.
ARMED_AT_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)
_ONE_DAY_MS = 86_400_000


def live_settings(**overrides: Any) -> AlpacaSettings:
    """Live settings whose envelope is exactly ``TEST_ENVELOPE_VALUES``.

    Constructed with explicit keyword arguments, which outrank the process
    environment and ``.env`` in pydantic-settings, so the values are the test's
    and never the developer's.
    """
    values: dict[str, Any] = {
        "api_key_id": "k",
        "api_secret_key": "s",
        "mode": "live",
        "live_loss_fraction": TEST_ENVELOPE_VALUES.loss_fraction,
        "live_loss_usd": TEST_ENVELOPE_VALUES.loss_usd,
        "live_shadow_sessions": TEST_ENVELOPE_VALUES.shadow_sessions,
        "live_arming_max_sessions": TEST_ENVELOPE_VALUES.arming_max_sessions,
        "live_xh_entry_bps": TEST_ENVELOPE_VALUES.xh_entry_bps,
        "live_xh_exit_bps": TEST_ENVELOPE_VALUES.xh_exit_bps,
    }
    values.update(overrides)
    return AlpacaSettings(**values)


def paper_settings() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")


def activate_shadow_fence(
    artifacts_root: Path,
    *,
    live_account_id: str = LIVE_ACCT,
    activated_at_ms: int = ARMED_AT_MS,
) -> ShadowActivationRecord:
    """The shadow activation proof alone, without opening a custody database.

    ``activate_shadow_clerk_authority`` would also create the SQLite authority
    and hold its execution lease; the ceremony reads only the fence, so this
    writes only the fence.
    """
    record = ShadowActivationRecord.create(
        account_id=shadow_account_id_for_live_account(live_account_id),
        authority_generation=1,
        db_identity_token=f"arming-fixture-{live_account_id}",
        activated_at_ms=activated_at_ms,
    )
    ShadowActivationStore(artifacts_root).append(record)
    return record


def sealed_program(
    *,
    strategy_instance_id: str = ARMING_SID,
    sealed_account_id: str = SHADOW_ACCT,
    quantity: int = 1,
) -> SealedBotProgram:
    """A minimal but genuine v2 program seal, self-hashed by ``seal_bot_program``."""
    configured = ConfiguredSignalProgramSeal(
        program_key="ema_crossover_signal",
        program_version="ema-crossover-signal/v1",
        protocol_version="signal-session-protocol/v1",
        parameter_schema_version="ema-crossover-signal-params/v1",
        golden_trace_root="a" * 64,
        parameters={
            "fast_period": ResolvedSignalParameter(value=12, unit="bars", origin="registered_default"),
        },
        parameters_match_validated_settings=True,
        data=SignalDataContract(
            provider="polygon",
            symbol="SPY",
            base_timeframe_ms=60_000,
            decision_timeframe_ms=60_000,
        ),
        clock=SignalClockContract(use_rth=True, warmup_lookback_days=5),
        signals=(SignalSeriesContract(name="fast", indicator="ema", field="close", period=12, warmup_bars=12),),
        decision_streams=("ENTER", "EXIT"),
        bar_integrity=SignalBarIntegrityContract(),
        exit_eligibility=ExitEligibilityContract(countdown_decision_clocks=5, countdown_state_persistable=False),
        numerical_provenance=NumericalProvenanceContract(
            formula="test formula",
            reference="test reference",
            canonical_implementation="test canonical implementation",
            validated_against="test validated against",
            equivalence_level="bit_exact",
        ),
    )
    return seal_bot_program(
        strategy_instance_id=strategy_instance_id,
        configured_signal=configured,
        configured_signal_hash=configured.semantic_hash(),
        broker="alpaca",
        sealed_account_id=sealed_account_id,
        mode="trade",
        action_plan={"on_enter": [], "on_exit": []},
        quantity=quantity,
        carryover_policy="FORBID",
        validation_event_id="event-1",
        validation_snapshot_sha256="d" * 64,
        sealed_at_ms=1_000,
    )


def record_sealed_binding(
    live_state_root: Path,
    *,
    strategy_instance_id: str = ARMING_SID,
    sealed_account_id: str = SHADOW_ACCT,
    quantity: int = 1,
) -> SealedBotProgram:
    """Put one readable, sealed runner binding on disk and return its seal.

    ``quantity`` is a parameter because R2 binds arming to the *whole* sealed
    program: changing it is how a test proves a size change disarms.
    """
    seal = sealed_program(
        strategy_instance_id=strategy_instance_id,
        sealed_account_id=sealed_account_id,
        quantity=quantity,
    )
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id=strategy_instance_id,
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            quantity=quantity,
            action_plan=alpaca_v1_action_plan("SPY"),
            sealed_program=seal,
            sealed_account_id=sealed_account_id,
            run_id=f"{strategy_instance_id}-run-1",
            created_at_ms=1_757_000_000_000,
        ),
        launch_reason="deploy",
    )
    return seal


def seal_receipt(
    artifacts_root: Path,
    *,
    configured_signal_hash: str,
    live_account_id: str = LIVE_ACCT,
    strategy_instance_id: str = ARMING_SID,
    sessions: int = 1,
    written_at_ms: int = ARMED_AT_MS - _ONE_DAY_MS,
) -> ShadowReceipt:
    """One shadow receipt that is current for this instance's configured signal."""
    receipt = ShadowReceipt.create(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        configured_signal_hash=configured_signal_hash,
        twin_account_id="PA-TWIN-ARM",
        twin_strategy_instance_id=f"{strategy_instance_id}-twin",
        required_sessions=sessions,
        sessions=tuple(
            ShadowReceiptSession(
                session_open_ms=written_at_ms - _ONE_DAY_MS * (index + 1),
                shadow_run_id=f"{strategy_instance_id}-run-1",
                reconciliation_sha256="e" * 64,
            )
            for index in range(sessions)
        ),
        written_at_ms=written_at_ms,
    )
    ShadowReceiptStore(artifacts_root).append(receipt)
    return receipt


def arming_ready(
    artifacts_root: Path,
    live_state_root: Path,
    *,
    strategy_instance_id: str = ARMING_SID,
) -> SealedBotProgram:
    """Every input ``observe_arming_inputs`` needs, on disk, for one instance."""
    if not ShadowActivationStore(artifacts_root).account_ids():
        activate_shadow_fence(artifacts_root)
    seal = record_sealed_binding(live_state_root, strategy_instance_id=strategy_instance_id)
    seal_receipt(
        artifacts_root,
        configured_signal_hash=seal.configured_signal_hash,
        strategy_instance_id=strategy_instance_id,
        sessions=TEST_ENVELOPE_VALUES.shadow_sessions,
    )
    return seal


__all__ = [
    "ARMED_AT_MS",
    "ARMING_SID",
    "activate_shadow_fence",
    "arming_ready",
    "live_settings",
    "paper_settings",
    "record_sealed_binding",
    "seal_receipt",
    "sealed_program",
]
```

- [ ] **Step 2: Write the failing test**

`tests/broker/alpaca/clerk/test_live_arming_ceremony.py`:

```python
"""The supervised arming ceremony: observe, plan, apply, disarm (ADR 0059 D3)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INPUTS_CHANGED,
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_NOT_ARMED,
    LIVE_ARMING_PLAN_EXPIRED,
    LIVE_ARMING_TOKEN_INVALID,
    LIVE_ARMING_TTL_INVALID,
    LIVE_ENVELOPE_MISSING,
    LIVE_SHADOW_INCOMPLETE,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.live_arming_ceremony import (
    ArmingInputs,
    ArmingObserver,
    LiveArmingPlan,
    account_arming_statuses,
    apply_arming,
    custody_account_ids_for,
    disarm,
    instance_seal_hashes,
    live_account_id_for,
    observe_arming_inputs,
    plan_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    activate_shadow_fence,
    arming_ready,
    live_settings,
    paper_settings,
    record_sealed_binding,
    seal_receipt,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    SHADOW_ACCT,
    TEST_ENVELOPE_VALUES,
)

TTL_MS = 120_000


class _Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """The Clerk artifacts root and the runner ``live_state`` root, kept apart."""
    return tmp_path / "clerk", tmp_path / "runner"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _inputs(**overrides: object) -> ArmingInputs:
    values: dict[str, object] = {
        "live_account_id": LIVE_ACCT,
        "strategy_instance_id": ARMING_SID,
        "seal_hash": "a" * 64,
        "configured_signal_hash": "b" * 64,
        "shadow_receipt_sha256": "c" * 64,
        "envelope": TEST_ENVELOPE_VALUES,
        "max_sessions": TEST_ENVELOPE_VALUES.arming_max_sessions,
    }
    values.update(overrides)
    return ArmingInputs(**values)  # type: ignore[arg-type]


def _observer(inputs: ArmingInputs) -> ArmingObserver:
    def _observe(**_kwargs: object) -> ArmingInputs:
        return inputs

    return _observe


def _plan_with(inputs: ArmingInputs, artifacts_root: Path, *, now_ms: int = ARMED_AT_MS) -> LiveArmingPlan:
    return plan_arming(
        strategy_instance_id=inputs.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=artifacts_root,
        settings=live_settings(),
        clock=_Clock(now_ms),
        observe=_observer(inputs),
    )


def test_the_two_custody_ids_of_one_live_account_are_both_admissible() -> None:
    """Shadow custody seals ``shadow:<id>``; slice 7's real_live custody seals ``<id>``."""
    assert custody_account_ids_for(LIVE_ACCT) == frozenset({LIVE_ACCT, SHADOW_ACCT})


def test_the_observer_reads_the_four_inputs_off_disk(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    seal = arming_ready(artifacts_root, live_state_root)

    inputs = observe_arming_inputs(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
    )

    assert inputs.live_account_id == LIVE_ACCT
    # R2: arming binds to the whole sealed program, not the signal-only hash.
    assert inputs.seal_hash == seal.bot_configuration_hash
    assert inputs.configured_signal_hash == seal.configured_signal_hash
    assert inputs.seal_hash != inputs.configured_signal_hash
    assert len(inputs.shadow_receipt_sha256) == 64
    assert inputs.envelope == TEST_ENVELOPE_VALUES
    assert inputs.max_sessions == TEST_ENVELOPE_VALUES.arming_max_sessions


def test_a_paper_account_is_never_armed(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=paper_settings(),
        )

    assert caught.value.reason_code == LIVE_ENVELOPE_MISSING


def test_an_incomplete_live_envelope_refuses_by_the_same_code(roots: tuple[Path, Path]) -> None:
    """``model_copy`` bypasses the settings validator, which is the only way to
    reach this branch -- ``AlpacaSettings`` itself refuses to construct a live
    mode with a missing value, so this is the defence behind that door."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    incomplete = live_settings().model_copy(update={"live_loss_usd": None})

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=incomplete,
        )

    assert caught.value.reason_code == LIVE_ENVELOPE_MISSING
    assert "live_loss_usd" in str(caught.value)


def test_no_shadow_activation_proof_refuses_before_any_binding_is_read(roots: tuple[Path, Path]) -> None:
    artifacts_root, _live_state_root = roots
    artifacts_root.mkdir(parents=True)

    with pytest.raises(LiveArmingRefused) as caught:
        live_account_id_for(artifacts_root)

    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED
    assert "manage_alpaca_shadow activate" in str(caught.value)


def test_two_shadowed_accounts_refuse_rather_than_choosing_one(roots: tuple[Path, Path]) -> None:
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root, live_account_id=LIVE_ACCT)
    activate_shadow_fence(artifacts_root, live_account_id="9LIVE0002")

    with pytest.raises(LiveArmingRefused) as caught:
        live_account_id_for(artifacts_root)

    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED
    assert "more than one shadowed live account" in str(caught.value)


def test_an_unsealed_or_foreign_binding_is_not_an_armable_instance(roots: tuple[Path, Path]) -> None:
    from app.services.bot_binding_repository import (
        BrokerBotBinding,
        alpaca_v1_action_plan,
        live_state_binding_repository,
    )

    artifacts_root, live_state_root = roots
    activate_shadow_fence(artifacts_root)
    # A legacy binding with no v2 seal: skipped, never refused, so it cannot
    # stop a sealed sibling from arming.
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id="legacy",
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id="legacy-run-1",
            created_at_ms=1_757_000_000_000,
        ),
        launch_reason="deploy",
    )
    record_sealed_binding(live_state_root, strategy_instance_id="elsewhere", sealed_account_id="PA-OTHER")
    sealed = record_sealed_binding(live_state_root, strategy_instance_id=ARMING_SID)

    seals = instance_seal_hashes(live_account_id=LIVE_ACCT, live_state_root=live_state_root)

    assert set(seals) == {ARMING_SID}
    assert seals[ARMING_SID].seal_hash == sealed.bot_configuration_hash

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id="legacy",
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
        )
    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED


def test_no_current_shadow_receipt_is_the_adrs_own_refusal(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    activate_shadow_fence(artifacts_root)
    record_sealed_binding(live_state_root)
    # A receipt for a different configured signal is not this seal's proof.
    seal_receipt(artifacts_root, configured_signal_hash="f" * 64)

    with pytest.raises(LiveArmingRefused) as caught:
        observe_arming_inputs(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
        )

    assert caught.value.reason_code == LIVE_SHADOW_INCOMPLETE


def test_plan_writes_nothing_and_its_two_ids_are_its_own_content_hash(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    before = _snapshot(artifacts_root)

    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        confirmation_ttl_ms=TTL_MS,
        clock=_Clock(ARMED_AT_MS),
    )

    assert _snapshot(artifacts_root) == before
    assert plan.schema_version == 1
    assert plan.plan_id == plan.confirmation_token and len(plan.plan_id) == 64
    assert (plan.created_at_ms, plan.expires_at_ms) == (ARMED_AT_MS, ARMED_AT_MS + TTL_MS)
    assert plan.live_account_id == LIVE_ACCT
    assert plan.envelope_sha256 == TEST_ENVELOPE_VALUES.sha
    assert plan.envelope_values == TEST_ENVELOPE_VALUES.to_mapping()
    assert plan.max_sessions == TEST_ENVELOPE_VALUES.arming_max_sessions


@pytest.mark.parametrize("ttl", [0, -1, 300_001])
def test_a_confirmation_window_outside_the_bound_is_refused(roots: tuple[Path, Path], ttl: int) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    with pytest.raises(LiveArmingRefused) as caught:
        plan_arming(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            confirmation_ttl_ms=ttl,
            clock=_Clock(ARMED_AT_MS),
        )

    assert caught.value.reason_code == LIVE_ARMING_TTL_INVALID


def test_apply_arms_the_instance_and_appends_exactly_one_sealed_record(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    seal = arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )

    record = apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS + 1_000),
    )

    assert isinstance(record, LiveArmingRecord)
    assert record.armed_at_ms == ARMED_AT_MS + 1_000
    assert record.seal_hash == seal.bot_configuration_hash
    assert record.envelope_sha256 == TEST_ENVELOPE_VALUES.sha
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    assert ledger.records() == (record,)
    assert ledger.sealed_envelope() == TEST_ENVELOPE_VALUES


def test_apply_refuses_a_token_that_is_not_the_plans(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = _plan_with(_inputs(), artifacts_root)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token="0" * 64,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(_inputs()),
        )

    assert caught.value.reason_code == LIVE_ARMING_TOKEN_INVALID
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_apply_refuses_a_plan_whose_content_was_edited(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = _plan_with(_inputs(), artifacts_root)
    forged = replace(plan, max_sessions=999)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=forged,
            confirmation_token=forged.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(_inputs()),
        )

    assert caught.value.reason_code == LIVE_ARMING_TOKEN_INVALID
    assert "content hash does not verify" in str(caught.value)


def test_apply_refuses_once_the_confirmation_window_has_closed(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        confirmation_ttl_ms=10,
        clock=_Clock(ARMED_AT_MS),
    )

    # The last admissible millisecond still applies.
    on_the_edge = apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS + 10),
    )
    assert on_the_edge.armed_at_ms == ARMED_AT_MS + 10

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS + 11),
        )
    assert caught.value.reason_code == LIVE_ARMING_PLAN_EXPIRED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_account_id", "9LIVE0002"),
        ("strategy_instance_id", "someone-else"),
        ("seal_hash", "d" * 64),
        ("configured_signal_hash", "d" * 64),
        ("shadow_receipt_sha256", "d" * 64),
        ("max_sessions", 5),
    ],
)
def test_apply_refuses_every_input_that_drifted_between_plan_and_apply(
    roots: tuple[Path, Path], field: str, value: object
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    planned = _inputs()
    plan = _plan_with(planned, artifacts_root)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(replace(planned, **{field: value})),
        )

    assert caught.value.reason_code == LIVE_ARMING_INPUTS_CHANGED
    assert field in str(caught.value)
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_apply_refuses_an_envelope_that_changed_between_plan_and_apply(roots: tuple[Path, Path]) -> None:
    """The envelope drifts by its sha, not by its seven-field object identity."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    planned = _inputs()
    plan = _plan_with(planned, artifacts_root)

    with pytest.raises(LiveArmingRefused) as caught:
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS),
            observe=_observer(replace(planned, envelope=replace(TEST_ENVELOPE_VALUES, loss_usd=4_000.0))),
        )

    assert caught.value.reason_code == LIVE_ARMING_INPUTS_CHANGED
    assert "envelope_sha256" in str(caught.value)


def test_re_arming_while_armed_supersedes_with_no_already_armed_refusal(roots: tuple[Path, Path]) -> None:
    """R7: renewal before lapse is the same ceremony, run again."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    for offset in (0, 60_000):
        plan = plan_arming(
            strategy_instance_id=ARMING_SID,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS + offset),
        )
        apply_arming(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=live_settings(),
            clock=_Clock(ARMED_AT_MS + offset),
        )

    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    assert len(ledger.records()) == 2
    latest = ledger.latest(ARMING_SID)
    assert isinstance(latest, LiveArmingRecord) and latest.armed_at_ms == ARMED_AT_MS + 60_000


def test_disarm_revokes_the_latest_record_and_needs_no_confirmation(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    armed = apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )

    revocation = disarm(
        strategy_instance_id=ARMING_SID, artifacts_root=artifacts_root, clock=_Clock(ARMED_AT_MS + 5_000)
    )

    assert isinstance(revocation, LiveDisarmRecord)
    assert revocation.revokes_record_sha256 == armed.record_sha256
    assert revocation.disarmed_at_ms == ARMED_AT_MS + 5_000
    statuses = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS + 5_000,
    )
    assert statuses[ARMING_SID].state == "disarmed"
    assert statuses[ARMING_SID].reason_code == "LIVE_ARMING_REVOKED"


def test_disarm_with_nothing_to_revoke_is_refused_rather_than_written(roots: tuple[Path, Path]) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    with pytest.raises(LiveArmingRefused) as caught:
        disarm(strategy_instance_id=ARMING_SID, artifacts_root=artifacts_root, clock=_Clock(ARMED_AT_MS))

    assert caught.value.reason_code == LIVE_ARMING_NOT_ARMED
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_account_statuses_answer_every_instance_with_a_row_and_named_ones_besides(
    roots: tuple[Path, Path],
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )

    every = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS,
    )
    assert set(every) == {ARMING_SID}
    assert every[ARMING_SID].state == "armed"
    assert (every[ARMING_SID].sessions_used, every[ARMING_SID].sessions_remaining) == (1, 19)

    named = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS,
        strategy_instance_ids=["never-armed"],
    )
    assert named["never-armed"].state == "unarmed"


def test_a_size_change_alone_disarms_because_arming_binds_the_whole_seal(
    roots: tuple[Path, Path],
) -> None:
    """R2's whole point, end to end: quantity is inside ``bot_configuration_hash``."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan = plan_arming(
        strategy_instance_id=ARMING_SID,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    apply_arming(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=live_settings(),
        clock=_Clock(ARMED_AT_MS),
    )
    resized = record_sealed_binding(
        live_state_root / "resized", strategy_instance_id=ARMING_SID, quantity=7
    )
    assert resized.bot_configuration_hash != plan.seal_hash

    statuses = account_arming_statuses(
        live_account_id=LIVE_ACCT,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root / "resized",
        configured_envelope=TEST_ENVELOPE_VALUES,
        now_ms=ARMED_AT_MS,
    )

    assert statuses[ARMING_SID].state == "disarmed"
    assert statuses[ARMING_SID].reason_code == "LIVE_ARMING_SEAL_CHANGED"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming_ceremony.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.broker.alpaca.clerk.live_arming_ceremony'`.

- [ ] **Step 4: Let the activation store name the accounts it fenced**

In `app/broker/alpaca/clerk/synthetic_activation.py`, add this method to `IsolatedActivationStore`, immediately after `latest()` (which ends at line 161) and before `append()`:

```python
    def account_ids(self) -> tuple[str, ...]:
        """Every account with an activation row here, in first-appearance order.

        The arming ceremony *observes* the account it arms rather than being
        told it (ADR 0059 D3 R8), and this fence is the evidence that a shadow
        gate was ever run against one.
        """
        return tuple(dict.fromkeys(record.account_id for record in self._read_all()))
```

- [ ] **Step 5: Write `app/broker/alpaca/clerk/live_arming_ceremony.py`**

```python
"""The supervised arming ceremony: observe, plan, apply, disarm (ADR 0059 D3).

``plan_arming`` is strictly read-only. ``apply_arming`` accepts no force mode:
it verifies the plan's own content hash and the operator's quoted token, checks
the confirmation window, re-observes every input, refuses any drift, and only
then appends the sealed record. ``disarm`` is the closed direction and takes no
plan at all.

Nothing here contacts a broker. The four inputs (design R8) are settings, the
shadow activation proof, the instance's sealed binding, and a current shadow
receipt -- all durable evidence already on disk. Mode agreement against the
broker stays the runtime's job at boot, and (slice 7) at admission.

An arming record grants nothing in this slice: no path submits a real-money
order until slice 7 opens one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import (
    SHADOW_ACCOUNT_PREFIX,
    shadow_account_id_for_live_account,
)
from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    plan_content_token,
    require_confirmation_ttl_ms,
    require_plan_token,
    require_unexpired,
)
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INPUTS_CHANGED,
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_NOT_ARMED,
    LIVE_ARMING_PLAN_EXPIRED,
    LIVE_ARMING_TOKEN_INVALID,
    LIVE_ARMING_TTL_INVALID,
    LIVE_ENVELOPE_MISSING,
    LIVE_SHADOW_INCOMPLETE,
    ArmingStatus,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    arming_status,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.broker.alpaca.config import AlpacaSettings
from app.services.bot_binding_repository import live_state_binding_repository
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

_LABEL = "live arming"


@dataclass(frozen=True)
class InstanceSeal:
    """One instance's two hashes: what arming binds to, and what the receipt binds to."""

    seal_hash: str
    configured_signal_hash: str


@dataclass(frozen=True)
class ArmingInputs:
    """Everything one arming ceremony observed, at one instant."""

    live_account_id: str
    strategy_instance_id: str
    seal_hash: str
    configured_signal_hash: str
    shadow_receipt_sha256: str
    envelope: LiveEnvelopeValues
    max_sessions: int


type ArmingObserver = Callable[..., ArmingInputs]


@dataclass(frozen=True)
class LiveArmingPlan:
    """A read-only proposal whose content hash is its own confirmation token."""

    schema_version: int
    plan_id: str
    confirmation_token: str
    created_at_ms: int
    expires_at_ms: int
    live_account_id: str
    strategy_instance_id: str
    seal_hash: str
    configured_signal_hash: str
    shadow_receipt_sha256: str
    envelope_values: dict[str, float | int]
    envelope_sha256: str
    max_sessions: int


def _refused(reason_code: str) -> Callable[[str], LiveArmingRefused]:
    """Adapt this module's coded refusal to the ceremony's message-only seam."""
    return lambda message: LiveArmingRefused(reason_code, message)


def custody_account_ids_for(live_account_id: str) -> frozenset[str]:
    """The account ids a binding sealed on this live account may carry.

    Under the Shadow Account Authority custody is ``shadow:<live_account_id>``,
    so an instance rehearsing on this account seals the shadow id; slice 7's
    ``real_live`` custody will seal the live id itself. Both are the same
    account to an operator, and arming has to admit either.
    """
    return frozenset({live_account_id, shadow_account_id_for_live_account(live_account_id)})


def live_account_id_for(artifacts_root: Path) -> str:
    """The live account the shadow gate was run against -- observed, not supplied.

    Taking the account from the operator would let an arming name an account
    that was never shadowed. The activation fence is the evidence that one was.
    """
    shadow_ids = ShadowActivationStore(artifacts_root).account_ids()
    if not shadow_ids:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"no shadow activation proof under {artifacts_root}; run "
            "scripts.manage_alpaca_shadow activate before arming",
        )
    if len(shadow_ids) > 1:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the artifacts root names more than one shadowed live account "
            f"({', '.join(sorted(shadow_ids))}); arming cannot choose between them",
        )
    return shadow_ids[0].removeprefix(SHADOW_ACCOUNT_PREFIX)


def instance_seal_hashes(*, live_account_id: str, live_state_root: Path) -> dict[str, InstanceSeal]:
    """Every sealed Alpaca instance bound to this live account, by instance id.

    A binding with no v2 program seal is skipped rather than refused: it is a
    legacy record that cannot be armed, and its presence must not stop a sealed
    sibling from arming.
    """
    admissible = custody_account_ids_for(live_account_id)
    seals: dict[str, InstanceSeal] = {}
    for binding in live_state_binding_repository(live_state_root).list_for_broker("alpaca"):
        seal = binding.sealed_program
        if seal is None or binding.sealed_account_id not in admissible:
            continue
        seals[binding.strategy_instance_id] = InstanceSeal(
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
        )
    return seals


def observe_arming_inputs(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
) -> ArmingInputs:
    """Read the four inputs R8 names, refusing by code when any one is absent."""
    if settings.mode != "live":
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"ALPACA_MODE={settings.mode}; only a live account can be armed (ADR 0059 D3).",
        )
    try:
        envelope = LiveEnvelopeValues.from_settings(settings)
    except LiveEnvelopeIncomplete as exc:
        raise LiveArmingRefused(LIVE_ENVELOPE_MISSING, str(exc)) from exc
    live_account_id = live_account_id_for(artifacts_root)
    seal = instance_seal_hashes(
        live_account_id=live_account_id, live_state_root=live_state_root
    ).get(strategy_instance_id)
    if seal is None:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"{strategy_instance_id} has no sealed alpaca binding on {live_account_id}",
        )
    receipt = ShadowReceiptStore(artifacts_root).current(
        strategy_instance_id,
        configured_signal_hash=seal.configured_signal_hash,
        required_sessions=envelope.shadow_sessions,
    )
    if receipt is None:
        raise LiveArmingRefused(
            LIVE_SHADOW_INCOMPLETE,
            f"{strategy_instance_id} has no current shadow receipt for this seal over "
            f"{envelope.shadow_sessions} session(s)",
        )
    return ArmingInputs(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        seal_hash=seal.seal_hash,
        configured_signal_hash=seal.configured_signal_hash,
        shadow_receipt_sha256=receipt.receipt_sha256,
        envelope=envelope,
        max_sessions=envelope.arming_max_sessions,
    )


def _plan_payload(plan: LiveArmingPlan) -> dict[str, Any]:
    """The plan's content, from which its two ids are derived."""
    payload = asdict(plan)
    del payload["plan_id"], payload["confirmation_token"]
    return payload


def plan_arming(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
    confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS,
    clock: Clock = now_ms_utc,
    observe: ArmingObserver = observe_arming_inputs,
) -> LiveArmingPlan:
    """Read and content-address every input without writing anything."""
    now = clock()
    require_confirmation_ttl_ms(confirmation_ttl_ms, refused=_refused(LIVE_ARMING_TTL_INVALID))
    inputs = observe(
        strategy_instance_id=strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
    )
    draft = LiveArmingPlan(
        schema_version=1,
        plan_id="",
        confirmation_token="",
        created_at_ms=now,
        expires_at_ms=now + confirmation_ttl_ms,
        live_account_id=inputs.live_account_id,
        strategy_instance_id=inputs.strategy_instance_id,
        seal_hash=inputs.seal_hash,
        configured_signal_hash=inputs.configured_signal_hash,
        shadow_receipt_sha256=inputs.shadow_receipt_sha256,
        envelope_values=inputs.envelope.to_mapping(),
        envelope_sha256=inputs.envelope.sha,
        max_sessions=inputs.max_sessions,
    )
    token = plan_content_token(_plan_payload(draft))
    return replace(draft, plan_id=token, confirmation_token=token)


# The facts a plan named and an apply must find unchanged. An allowlist rather
# than a whole-object comparison, so the two ids, the clock stamps and the
# expanded envelope values -- every one of which is derived from these -- cannot
# make a same-input re-observation look like drift.
_DRIFTABLE: tuple[str, ...] = (
    "live_account_id",
    "strategy_instance_id",
    "seal_hash",
    "configured_signal_hash",
    "shadow_receipt_sha256",
    "envelope_sha256",
    "max_sessions",
)


def _require_no_drift(plan: LiveArmingPlan, current: ArmingInputs) -> None:
    observed: dict[str, Any] = {
        "live_account_id": current.live_account_id,
        "strategy_instance_id": current.strategy_instance_id,
        "seal_hash": current.seal_hash,
        "configured_signal_hash": current.configured_signal_hash,
        "shadow_receipt_sha256": current.shadow_receipt_sha256,
        "envelope_sha256": current.envelope.sha,
        "max_sessions": current.max_sessions,
    }
    for name in _DRIFTABLE:
        if observed[name] != getattr(plan, name):
            raise LiveArmingRefused(
                LIVE_ARMING_INPUTS_CHANGED, f"{name} changed after arming planning"
            )


def apply_arming(
    *,
    plan: LiveArmingPlan,
    confirmation_token: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
    clock: Clock = now_ms_utc,
    observe: ArmingObserver = observe_arming_inputs,
) -> LiveArmingRecord:
    """Recheck the plan, re-observe every input, then append the sealed record."""
    now = clock()
    if plan.schema_version != 1:
        raise LiveArmingRefused(LIVE_ARMING_TOKEN_INVALID, f"{_LABEL} plan content hash does not verify")
    require_plan_token(
        _plan_payload(plan),
        plan_id=plan.plan_id,
        confirmation_token=plan.confirmation_token,
        supplied_token=confirmation_token,
        refused=_refused(LIVE_ARMING_TOKEN_INVALID),
        label=_LABEL,
    )
    require_unexpired(
        now_ms=now,
        expires_at_ms=plan.expires_at_ms,
        refused=_refused(LIVE_ARMING_PLAN_EXPIRED),
        label=_LABEL,
    )
    current = observe(
        strategy_instance_id=plan.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
    )
    _require_no_drift(plan, current)
    record = LiveArmingRecord.create(
        live_account_id=current.live_account_id,
        strategy_instance_id=current.strategy_instance_id,
        seal_hash=current.seal_hash,
        configured_signal_hash=current.configured_signal_hash,
        shadow_receipt_sha256=current.shadow_receipt_sha256,
        envelope=current.envelope,
        armed_at_ms=now,
        max_sessions=current.max_sessions,
    )
    LiveArmingLedger(artifacts_root, live_account_id=record.live_account_id).append(record)
    logger.warning(
        "live instance armed",
        extra={
            "action": "live_arming_applied",
            "account_id": record.live_account_id,
            "strategy_instance_id": record.strategy_instance_id,
            "max_sessions": record.max_sessions,
            "record_sha256": record.record_sha256,
        },
    )
    return record


def disarm(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    clock: Clock = now_ms_utc,
) -> LiveDisarmRecord:
    """Revoke one instance's arming: one append, no plan, no confirmation (R4).

    Disarming is the closed direction, so it reads no settings, no binding and
    no receipt: an operator must be able to revoke an arming whose evidence has
    already gone. It needs only the account the shadow gate ran against and the
    record it revokes.
    """
    live_account_id = live_account_id_for(artifacts_root)
    ledger = LiveArmingLedger(artifacts_root, live_account_id=live_account_id)
    latest = ledger.latest(strategy_instance_id)
    if not isinstance(latest, LiveArmingRecord):
        raise LiveArmingRefused(
            LIVE_ARMING_NOT_ARMED,
            f"{strategy_instance_id} has no arming record to revoke on {live_account_id}",
        )
    record = LiveDisarmRecord.create(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        revokes_record_sha256=latest.record_sha256,
        disarmed_at_ms=clock(),
    )
    ledger.append(record)
    logger.warning(
        "live instance disarmed",
        extra={
            "action": "live_arming_revoked",
            "account_id": live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "revokes_record_sha256": latest.record_sha256,
        },
    )
    return record


def account_arming_statuses(
    *,
    live_account_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    configured_envelope: LiveEnvelopeValues,
    now_ms: int,
    strategy_instance_ids: Sequence[str] | None = None,
) -> dict[str, ArmingStatus]:
    """Every named instance's arming status, reading the ledger and bindings once.

    ``strategy_instance_ids=None`` means "every instance with a row in the
    ledger" -- the set an operator's ``status`` and the live verdict both want.
    An instance whose sealed binding has gone gets ``seal_hash=None``, which
    ``arming_status`` reads as a change from what was armed.
    """
    ledger = LiveArmingLedger(artifacts_root, live_account_id=live_account_id)
    records = ledger.records()
    seals = instance_seal_hashes(live_account_id=live_account_id, live_state_root=live_state_root)
    wanted = ledger.instance_ids() if strategy_instance_ids is None else tuple(strategy_instance_ids)
    statuses: dict[str, ArmingStatus] = {}
    for sid in wanted:
        seal = seals.get(sid)
        statuses[sid] = arming_status(
            records,
            live_account_id=live_account_id,
            strategy_instance_id=sid,
            seal_hash=None if seal is None else seal.seal_hash,
            configured_envelope=configured_envelope,
            now_ms=now_ms,
        )
    return statuses


__all__ = [
    "ArmingInputs",
    "ArmingObserver",
    "InstanceSeal",
    "LiveArmingPlan",
    "account_arming_statuses",
    "apply_arming",
    "custody_account_ids_for",
    "disarm",
    "instance_seal_hashes",
    "live_account_id_for",
    "observe_arming_inputs",
    "plan_arming",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_live_arming_ceremony.py tests/broker/alpaca/clerk/test_synthetic_activation.py tests/broker/alpaca/clerk/test_shadow_activation.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: all pass; ruff clean. If `test_the_observer_reads_the_four_inputs_off_disk` fails with `strategy instance configuration hash is invalid`, the binding you built in the fixture is not round-tripping through `BotBindingRepository._read_normalized` — re-check that `record_sealed_binding` passes `sealed_account_id` on the **binding** as well as inside the seal, and that `quantity` matches on both.

- [ ] **Step 7: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py PythonDataService/tests/broker/alpaca/clerk/live_arming_fixtures.py PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ceremony.py
git commit -m "feat(arming): the supervised arming ceremony - observe, plan, apply, disarm

plan is read-only; apply re-observes every input and refuses any drift; the
account is observed from the shadow activation fence, never supplied.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `scripts/manage_alpaca_arming.py` — the operator entry point

**Files:**
- Create: `PythonDataService/scripts/manage_alpaca_arming.py`
- Test: `PythonDataService/tests/scripts/test_manage_alpaca_arming.py`

**Interfaces:**
- Consumes (Task 1): `DEFAULT_CONFIRMATION_TTL_MS`, `MAX_CONFIRMATION_TTL_MS`.
- Consumes (Task 2): `LiveArmingInvalid`, `LiveArmingRefused(reason_code, message)` with attribute `reason_code`, `ArmingStatus(state, reason_code, record, sessions_used, sessions_remaining)`, `LIVE_ENVELOPE_MISSING`.
- Consumes (Task 3): `LiveArmingLedger(artifacts_root, live_account_id=...)` with `latest_arming()`.
- Consumes (Task 4): `LiveArmingPlan`, `account_arming_statuses(...)`, `apply_arming(...)`, `disarm(...)`, `live_account_id_for(artifacts_root)`, `plan_arming(...)`; and from `tests/broker/alpaca/clerk/live_arming_fixtures.py` (created by Task 4): `ARMED_AT_MS`, `ARMING_SID`, `activate_shadow_fence`, `arming_ready`, `live_settings`, `record_sealed_binding`.
- Consumes from the repo: `app.broker.alpaca.clerk.account_authority.AccountAuthorityIdentityError`; `app.broker.alpaca.clerk.shadow_activation.ShadowActivationInvalid`; `app.broker.alpaca.clerk.shadow_receipt.ShadowReceiptInvalid`; `app.broker.alpaca.clerk.live_envelope.{LiveEnvelopeIncomplete, LiveEnvelopeValues}`; `app.broker.alpaca.clerk.sqlite.operational_files.atomic_write_json(path, payload) -> str`; `app.broker.alpaca.config.{AlpacaSettings, get_alpaca_settings}`; `app.broker.ibkr.config.live_artifacts_root() -> Path`; `app.utils.session_anchors.MAX_TIMESTAMP_MS`; `app.utils.timestamps.{Clock, now_ms_utc}`.
- Produces (Task 9 runs it; nothing imports it):
  - `SUBMISSION_NOTE: str`
  - `class ArmingOperatorRefusal(ValueError)`
  - `main(argv: list[str] | None = None, *, settings: AlpacaSettings | None = None) -> int`

- [ ] **Step 1: Read the CLI this one mirrors**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && sed -n '1,30p;95,135p;314,351p' scripts/manage_alpaca_shadow.py`
You are copying three things from it: `exit_on_error=False` on the top parser **and every subparser** (without it argparse exits 2 on its own, and 2 is the code "the ceremony refused" owns here); the `_write` one-object-per-invocation discipline; and the `main(argv, *, <injected seam>)` shape that lets a test drive the real code with no environment.

- [ ] **Step 2: Write the failing test**

`tests/scripts/test_manage_alpaca_arming.py`:

```python
"""The arming operator CLI: status, plan, apply, disarm (ADR 0059 D3, design R13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from scripts.manage_alpaca_arming import main
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    activate_shadow_fence,
    arming_ready,
    live_settings,
    record_sealed_binding,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

SETTINGS = live_settings()


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "clerk", tmp_path / "runner"


def _flags(roots: tuple[Path, Path]) -> list[str]:
    artifacts_root, live_state_root = roots
    return ["--artifacts-root", str(artifacts_root), "--live-state-root", str(live_state_root)]


def _last_object(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def _arm(roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], *, now_ms: int = ARMED_AT_MS) -> dict:
    """Run the real plan → apply pair through the CLI and return the record object."""
    artifacts_root, _live_state_root = roots
    plan_file = artifacts_root / "plan.json"
    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--plan-out",
                str(plan_file),
                "--now-ms",
                str(now_ms),
            ],
            settings=SETTINGS,
        )
        == 0
    )
    plan = _last_object(capsys)
    assert (
        main(
            [
                *_flags(roots),
                "apply",
                "--plan-file",
                str(plan_file),
                "--confirmation-token",
                plan["confirmation_token"],
                "--now-ms",
                str(now_ms),
            ],
            settings=SETTINGS,
        )
        == 0
    )
    return _last_object(capsys)


def test_status_on_an_account_with_no_records_answers_unarmed(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 0

    report = _last_object(capsys)
    assert report["live_account_id"] == LIVE_ACCT
    assert report["armed_instance_count"] == 0
    assert report["envelope_state"] == "configured_unsealed"
    assert report["instances"] == []
    assert report["submission_admitted"] is False
    assert "Slice 7" in report["note"]


def test_plan_writes_only_the_plan_file_and_apply_writes_the_sealed_record(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    record = _arm(roots, capsys)

    assert record["kind"] == "armed"
    assert record["live_account_id"] == LIVE_ACCT
    assert record["armed_at_ms"] == ARMED_AT_MS
    assert record["submission_admitted"] is False
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    assert len(ledger.records()) == 1
    assert (artifacts_root / "plan.json").is_file()


def test_status_after_arming_counts_the_instance_and_reports_the_sealed_envelope(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 0

    report = _last_object(capsys)
    assert report["armed_instance_count"] == 1
    assert report["envelope_state"] == "sealed"
    (instance,) = report["instances"]
    assert instance["strategy_instance_id"] == ARMING_SID
    assert instance["state"] == "armed"
    assert instance["reason_code"] is None
    assert (instance["sessions_used"], instance["sessions_remaining"]) == (1, 19)


def test_a_quoted_token_that_is_not_the_plans_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan_file = artifacts_root / "plan.json"
    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID, "--plan-out", str(plan_file),
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(plan_file), "--confirmation-token", "0" * 64,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_TOKEN_INVALID"
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_applying_after_the_confirmation_window_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan_file = artifacts_root / "plan.json"
    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID, "--plan-out", str(plan_file),
             "--confirmation-ttl-ms", "10", "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )
    token = _last_object(capsys)["confirmation_token"]

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(plan_file), "--confirmation-token", token,
             "--now-ms", str(ARMED_AT_MS + 11)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_PLAN_EXPIRED"


def test_planning_without_a_current_shadow_receipt_refuses_by_the_adrs_code(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    activate_shadow_fence(artifacts_root)
    record_sealed_binding(live_state_root)

    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID, "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    report = _last_object(capsys)
    assert report["error"] == "LIVE_SHADOW_INCOMPLETE"
    assert report["submission_admitted"] is False


def test_disarm_revokes_and_status_names_the_revocation(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    armed = _arm(roots, capsys)

    assert (
        main(
            [*_flags(roots), "disarm", "--strategy-instance-id", ARMING_SID, "--now-ms", str(ARMED_AT_MS + 1)],
            settings=SETTINGS,
        )
        == 0
    )
    revocation = _last_object(capsys)
    assert revocation["kind"] == "disarmed"
    assert revocation["revokes_record_sha256"] == armed["record_sha256"]

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS + 1)], settings=SETTINGS) == 0
    report = _last_object(capsys)
    assert report["armed_instance_count"] == 0
    (instance,) = report["instances"]
    assert (instance["state"], instance["reason_code"]) == ("disarmed", "LIVE_ARMING_REVOKED")


def test_disarming_something_that_was_never_armed_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    assert (
        main(
            [*_flags(roots), "disarm", "--strategy-instance-id", ARMING_SID, "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_NOT_ARMED"


def test_a_plan_file_that_is_not_a_plan_is_an_operator_error_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    junk = artifacts_root / "junk.json"
    junk.write_text('{"schema_version": 1}', encoding="utf-8")

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(junk), "--confirmation-token", "0" * 64],
            settings=SETTINGS,
        )
        == 1
    )
    assert "not an arming plan" in _last_object(capsys)["error"]

    absent = artifacts_root / "nope.json"
    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(absent), "--confirmation-token", "0" * 64],
            settings=SETTINGS,
        )
        == 1
    )
    assert "unreadable" in _last_object(capsys)["error"]


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        pytest.param([], "operation", id="missing-subcommand"),
        pytest.param(["nosuch"], "invalid choice", id="unknown-subcommand"),
        pytest.param(["plan"], "--strategy-instance-id", id="missing-required-flag"),
        pytest.param(
            ["plan", "--strategy-instance-id", "s", "--confirmation-ttl-ms", "0"],
            "--confirmation-ttl-ms",
            id="ttl-below-bound",
        ),
        pytest.param(
            ["plan", "--strategy-instance-id", "s", "--confirmation-ttl-ms", "300001"],
            "--confirmation-ttl-ms",
            id="ttl-above-bound",
        ),
        pytest.param(
            ["status", "--now-ms", str(MAX_TIMESTAMP_MS + 1)], "--now-ms", id="instant-outside-the-domain"
        ),
        pytest.param(["disarm", "--strategy-instance-id", "s", "--nope"], "unrecognized arguments", id="unknown-flag"),
    ],
)
def test_every_usage_refusal_is_one_json_object_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], argv: list[str], fragment: str
) -> None:
    """Exit ``1``, never argparse's bare ``2`` -- which "the ceremony refused" owns.

    A script reading only the exit code could not otherwise tell a typo from a
    real-money arming precondition that has not been met.
    """
    assert main([*_flags(roots), *argv], settings=SETTINGS) == 1

    assert fragment in _last_object(capsys)["error"]


def test_a_ledger_row_that_will_not_verify_is_an_evidence_error_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"max_sessions":20', '"max_sessions":90'),
        encoding="utf-8",
    )

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 1

    assert "digest does not verify" in _last_object(capsys)["error"]
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/scripts/test_manage_alpaca_arming.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.manage_alpaca_arming'`.

- [ ] **Step 4: Write `scripts/manage_alpaca_arming.py`**

```python
"""Operator CLI for the arming ceremony (ADR 0059 D3): status, plan, apply, disarm.

``status`` judges one instance, or every instance with a row in the ledger, and
writes nothing. ``plan`` re-observes every input and prints a read-only proposal
whose content hash is its own confirmation token. ``apply`` re-observes again,
refuses any drift, and appends the sealed arming record -- the one write on this
path. ``disarm`` appends a revocation; it is the closed direction and takes no
plan.

The live account is never supplied on the command line: it is observed from the
shadow activation fence under ``--artifacts-root``, so an arming can only name an
account a shadow gate was actually run against.

Exit codes: ``0`` the command answered; ``1`` the command cannot be run as asked
-- a plan file that is not one, a ledger row that will not verify, or a usage
refusal (an absent flag, an unknown subcommand, a flag outside its bound); ``2``
the ceremony refused, under a named ``LIVE_ARMING_*`` / ``LIVE_SHADOW_INCOMPLETE``
/ ``LIVE_ENVELOPE_MISSING`` code.

Every invocation writes exactly one JSON object to stdout, every temporal value
in it is ``int64 ms UTC``, and every object carries ``"submission_admitted":
false`` -- the fact an operator most easily assumes wrong. (``--help`` is
argparse's own usage text and exits ``0``; it runs no command.)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.ceremony import DEFAULT_CONFIRMATION_TTL_MS, MAX_CONFIRMATION_TTL_MS
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ENVELOPE_MISSING,
    ArmingStatus,
    LiveArmingInvalid,
    LiveArmingRefused,
)
from app.broker.alpaca.clerk.live_arming_ceremony import (
    LiveArmingPlan,
    account_arming_statuses,
    apply_arming,
    disarm,
    live_account_id_for,
    plan_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationInvalid
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptInvalid
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.ibkr.config import live_artifacts_root
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import Clock, now_ms_utc

SUBMISSION_NOTE = (
    "An arming record is evidence, not a submission path: no code path submits a "
    "real-money order in ADR 0059 slice 6. Slice 7 is what reads this record at "
    "ENTER admission."
)


class ArmingOperatorRefusal(ValueError):
    """This command cannot be run as asked -- a named input is absent or malformed."""


def _timestamp_ms(raw: str) -> int:
    """One instant, ``int64 ms UTC``, inside the domain's admissible range."""
    value = int(raw)
    if not 0 <= value <= MAX_TIMESTAMP_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_TIMESTAMP_MS} milliseconds since epoch UTC, not {value}"
        )
    return value


def _confirmation_ttl_ms(raw: str) -> int:
    """The same bound the ceremony enforces, on the flag that carries it."""
    value = int(raw)
    if not 1 <= value <= MAX_CONFIRMATION_TTL_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_CONFIRMATION_TTL_MS} milliseconds, not {value}"
        )
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # ``exit_on_error=False`` on this parser *and* on every subparser: without it
    # argparse writes usage to stderr and exits 2 on its own, which is the code
    # "the ceremony refused" already owns here.
    parser = argparse.ArgumentParser(
        prog="scripts.manage_alpaca_arming",
        description="Arm, disarm and inspect sealed instances on the shadowed live account.",
        exit_on_error=False,
    )
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--live-state-root", type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    status = subparsers.add_parser(
        "status", help="Report one instance, or every instance with a record.", exit_on_error=False
    )
    status.add_argument("--strategy-instance-id")

    planner = subparsers.add_parser(
        "plan", help="Re-observe every input and print a read-only arming plan.", exit_on_error=False
    )
    planner.add_argument("--strategy-instance-id", required=True)
    planner.add_argument("--confirmation-ttl-ms", type=_confirmation_ttl_ms, default=DEFAULT_CONFIRMATION_TTL_MS)
    planner.add_argument("--plan-out", type=Path)

    applier = subparsers.add_parser(
        "apply", help="Re-observe, refuse drift, and append the sealed arming record.", exit_on_error=False
    )
    applier.add_argument("--plan-file", type=Path, required=True)
    applier.add_argument("--confirmation-token", required=True)

    disarmer = subparsers.add_parser(
        "disarm", help="Append a revocation for one armed instance.", exit_on_error=False
    )
    disarmer.add_argument("--strategy-instance-id", required=True)

    for subparser in (status, planner, applier, disarmer):
        subparser.add_argument("--now-ms", type=_timestamp_ms)
    return parser.parse_args(argv)


def _write(payload: Mapping[str, Any]) -> None:
    """One JSON object per invocation, on stdout.

    Every object carries ``submission_admitted`` and its note because that is
    the fact an operator most easily assumes wrong: arming is a permission slice
    7 will read, not a submission path this slice opened. ``default=str`` covers
    the ``Path`` values a plan may carry; every temporal value is already
    ``int64 ms UTC``.
    """
    sys.stdout.write(
        json.dumps(
            {**payload, "submission_admitted": False, "note": SUBMISSION_NOTE},
            sort_keys=True,
            default=str,
        )
        + "\n"
    )


def _clock(now_ms: int | None) -> Clock:
    return now_ms_utc if now_ms is None else (lambda: now_ms)


def _configured_envelope(settings: AlpacaSettings) -> LiveEnvelopeValues:
    """The environment's current envelope, refused by the ceremony's own code."""
    if settings.mode != "live":
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"ALPACA_MODE={settings.mode}; arming is a live-account question (ADR 0059 D3).",
        )
    try:
        return LiveEnvelopeValues.from_settings(settings)
    except LiveEnvelopeIncomplete as exc:
        raise LiveArmingRefused(LIVE_ENVELOPE_MISSING, str(exc)) from exc


def _read_plan(path: Path) -> LiveArmingPlan:
    """The plan file, or a sentence naming what is wrong with it.

    ``submission_admitted`` and ``note`` are stripped so a plan an operator
    saved from stdout reads back exactly like one written by ``--plan-out``.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArmingOperatorRefusal(f"arming plan file is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArmingOperatorRefusal("arming plan file must contain a JSON object")
    payload.pop("submission_admitted", None)
    payload.pop("note", None)
    try:
        return LiveArmingPlan(**payload)
    except TypeError as exc:
        raise ArmingOperatorRefusal(f"arming plan file is not an arming plan: {exc}") from exc


def _instance_payload(status: ArmingStatus, *, strategy_instance_id: str) -> dict[str, Any]:
    record = status.record
    return {
        "strategy_instance_id": strategy_instance_id,
        "state": status.state,
        "reason_code": status.reason_code,
        "sessions_used": status.sessions_used,
        "sessions_remaining": status.sessions_remaining,
        "armed_at_ms": None if record is None else record.armed_at_ms,
        "max_sessions": None if record is None else record.max_sessions,
        "seal_hash": None if record is None else record.seal_hash,
        "envelope_sha256": None if record is None else record.envelope_sha256,
        "record_sha256": None if record is None else record.record_sha256,
    }


def _status(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    now_ms = now_ms_utc() if args.now_ms is None else args.now_ms
    live_account_id = live_account_id_for(artifacts_root)
    statuses = account_arming_statuses(
        live_account_id=live_account_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=_configured_envelope(settings),
        now_ms=now_ms,
        strategy_instance_ids=(
            None if args.strategy_instance_id is None else [args.strategy_instance_id]
        ),
    )
    sealed = LiveArmingLedger(artifacts_root, live_account_id=live_account_id).latest_arming()
    _write(
        {
            "now_ms": now_ms,
            "live_account_id": live_account_id,
            "envelope_state": "configured_unsealed" if sealed is None else "sealed",
            "armed_instance_count": sum(1 for status in statuses.values() if status.state == "armed"),
            "instances": [
                _instance_payload(status, strategy_instance_id=sid)
                for sid, status in sorted(statuses.items())
            ],
        }
    )
    return 0


def _plan(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    plan = plan_arming(
        strategy_instance_id=args.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
        confirmation_ttl_ms=args.confirmation_ttl_ms,
        clock=_clock(args.now_ms),
    )
    if args.plan_out is not None:
        atomic_write_json(args.plan_out, asdict(plan))
    _write(asdict(plan))
    return 0


def _apply(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    record = apply_arming(
        plan=_read_plan(args.plan_file),
        confirmation_token=args.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
        clock=_clock(args.now_ms),
    )
    _write(asdict(record))
    return 0


def _disarm(args: argparse.Namespace, *, artifacts_root: Path) -> int:
    _write(
        asdict(
            disarm(
                strategy_instance_id=args.strategy_instance_id,
                artifacts_root=artifacts_root,
                clock=_clock(args.now_ms),
            )
        )
    )
    return 0


def main(argv: list[str] | None = None, *, settings: AlpacaSettings | None = None) -> int:
    try:
        args = _parse_args(argv)
        resolved = get_alpaca_settings() if settings is None else settings
        artifacts_root = args.artifacts_root or resolved.clerk_dir
        live_state_root = args.live_state_root or live_artifacts_root()
        if args.operation == "status":
            return _status(
                args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
            )
        if args.operation == "plan":
            return _plan(
                args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
            )
        if args.operation == "apply":
            return _apply(
                args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
            )
        return _disarm(args, artifacts_root=artifacts_root)
    except LiveArmingRefused as exc:
        _write({"error": exc.reason_code, "detail": str(exc)})
        return 2
    except (
        ArmingOperatorRefusal,
        # The sealed stores are the last word on their own rows; their refusal
        # is a sentence for an operator, never a traceback.
        LiveArmingInvalid,
        ShadowActivationInvalid,
        ShadowReceiptInvalid,
        AccountAuthorityIdentityError,
        # A usage refusal is an input error, not a ceremony verdict. Reaching it
        # here is what keeps the promise above: one JSON object, always.
        argparse.ArgumentError,
    ) as exc:
        _write({"error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/scripts/test_manage_alpaca_arming.py tests/scripts/test_manage_alpaca_shadow.py -q -p no:cacheprovider && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: all pass; ruff clean. If `test_every_usage_refusal_is_one_json_object_at_exit_one[missing-subcommand]` fails with a bare `SystemExit`, argparse raised on the *required subparser* rather than an `ArgumentError` on this runtime — in that case add `argparse.ArgumentError` handling by catching `SystemExit` is **not** the fix; instead give the `status` subcommand no `required=True` on `dest="operation"` and refuse an absent operation yourself with `ArmingOperatorRefusal("operation is required")`. Re-run and keep the exit code at 1.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add PythonDataService/scripts/manage_alpaca_arming.py PythonDataService/tests/scripts/test_manage_alpaca_arming.py
git commit -m "feat(arming): the operator CLI - status, plan, apply, disarm

Every object it prints says submission_admitted=false: an arming record is
evidence slice 7 will read, not a submission path this slice opened.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Runtime sealing — the ledger seals the envelope gate on every tick

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py` (imports at 30–46; `LiveEnvelopeSync.__init__` at 157–184; `tick()` at 258–293)
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_runtime.py` (the `TYPE_CHECKING` block at 50–63; `compose_repository_runtime`'s signature at 239–255; the `LiveEnvelopeSync(...)` construction at 350–363)
- Modify: `PythonDataService/app/broker/alpaca/clerk/shadow_authority.py` (imports after line 24; the `compose_repository_runtime(...)` call at 156–178)
- Modify: `PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py` (append one test)
- Modify: `PythonDataService/tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py` (imports at 15–57; append four tests)

**Interfaces:**
- Consumes (Task 2): `LiveArmingInvalid`, `LiveArmingRecord.create(*, live_account_id, strategy_instance_id, seal_hash, configured_signal_hash, shadow_receipt_sha256, envelope, armed_at_ms, max_sessions)`.
- Consumes (Task 3): `LiveArmingLedger(artifacts_root, live_account_id=...)` with `path`, `append(record)`, `sealed_envelope() -> LiveEnvelopeValues | None`.
- Consumes from slice 5: `LiveEnvelopeGate` — public attributes `values: LiveEnvelopeValues` and `sealed: LiveEnvelopeValues | None`, both plain and assignable, plus `@property agreement -> Literal["unsealed", "agreed", "disagreed"]`; `require_envelope_admission`, which already refuses `LIVE_ENVELOPE_DISAGREEMENT` when `agreement == "disagreed"`.
- Produces (Task 7 relies on these exact signatures):
  - `LiveEnvelopeSync.__init__(self, *, repo, read, envelope, interval_s=ENVELOPE_SYNC_INTERVAL_S, sleep=asyncio.sleep, max_ticks=None, arming_ledger: LiveArmingLedger | None = None)`
  - `LiveEnvelopeSync._refresh_sealed_envelope(self) -> None`, called first in `tick()`
  - `compose_repository_runtime(..., arming_ledger: LiveArmingLedger | None = None)`
  - `select_shadow_clerk_runtime` builds `LiveArmingLedger(artifacts_root, live_account_id=account.account_id)` — the **live** account id, never the `shadow:` custody one

- [ ] **Step 1: Write the failing tests**

Append to `tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py`. Add to its imports (`logging` in the stdlib block, `replace` from `dataclasses`, and the two clerk names in the first-party block):

```python
import logging
from dataclasses import replace

from app.broker.alpaca.clerk.live_arming import LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
```

Then append these helpers and tests at the end of the file:

```python
def _arm(
    artifacts_root: Path,
    *,
    envelope: object = TEST_ENVELOPE_VALUES,
    armed_at_ms: int = NOW_MS,
) -> LiveArmingRecord:
    """Append one arming record straight to the ledger.

    The ceremony that mints these has its own tests; what is under test here is
    what the *runtime* does with a record that exists.
    """
    record = LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=SID,
        seal_hash="a" * 64,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="c" * 64,
        envelope=envelope,  # type: ignore[arg-type]
        armed_at_ms=armed_at_ms,
        max_sessions=20,
    )
    LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).append(record)
    return record


async def test_the_gate_is_unsealed_until_an_arming_record_exists(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker], tmp_path: Path
) -> None:
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    assert runtime.envelope_sync.envelope.sealed is None
    assert runtime.envelope_sync.envelope.agreement == "unsealed"

    _arm(tmp_path)

    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.sealed == TEST_ENVELOPE_VALUES
    assert runtime.envelope_sync.envelope.agreement == "agreed"


async def test_an_environment_change_after_arming_refuses_every_enter_end_to_end(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
    tmp_path: Path,
) -> None:
    """R10's whole point: a live ENTER is admitted against the *sealed* values.

    Both halves are pinned on their own -- the sync seals from the ledger, and a
    disagreeing gate refuses -- but the chain between them is what an operator
    is trusting, and a regression that broke the join would leave both halves
    green.
    """
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    assert await runtime.envelope_sync.tick() == "observed"

    admitted = await _enter(runtime, registered_running_bot, quantity=1)
    assert admitted.state.value == "submitted", admitted.explanation

    # The operator edited ALPACA_LIVE_LOSS_USD and restarted nothing.
    runtime.envelope_sync.envelope.values = replace(TEST_ENVELOPE_VALUES, loss_usd=4_000.0)
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.agreement == "disagreed"

    refused = await _enter(runtime, registered_running_bot, quantity=1, decision_id="d2")
    assert refused.state.value == "rejected"
    assert refused.explanation.startswith("LIVE_ENVELOPE_DISAGREEMENT:"), refused.explanation
    assert "re-arm" in refused.explanation


async def test_a_re_arm_seals_the_new_environment_and_admits_again(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    registered_running_bot: RetainedSourceBar,
    tmp_path: Path,
) -> None:
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    tightened = replace(TEST_ENVELOPE_VALUES, loss_usd=4_000.0)
    runtime.envelope_sync.envelope.values = tightened
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.agreement == "disagreed"

    _arm(tmp_path, envelope=tightened, armed_at_ms=NOW_MS + 1)
    assert await runtime.envelope_sync.tick() == "observed"

    assert runtime.envelope_sync.envelope.sealed == tightened
    assert runtime.envelope_sync.envelope.agreement == "agreed"
    admitted = await _enter(runtime, registered_running_bot, quantity=1)
    assert admitted.state.value == "submitted", admitted.explanation


async def test_an_unreadable_arming_ledger_unseals_the_gate_and_is_logged_once(
    shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail closed and loud: a ledger nobody can read seals nothing."""
    runtime, _broker = shadow_runtime
    assert runtime.envelope_sync is not None
    _arm(tmp_path)
    assert await runtime.envelope_sync.tick() == "observed"
    assert runtime.envelope_sync.envelope.sealed is not None

    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"max_sessions":20', '"max_sessions":90'),
        encoding="utf-8",
    )
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert await runtime.envelope_sync.tick() == "observed"
        assert await runtime.envelope_sync.tick() == "observed"

    assert runtime.envelope_sync.envelope.sealed is None
    assert runtime.envelope_sync.envelope.agreement == "unsealed"
    invalid = [
        record for record in caplog.records if getattr(record, "action", None) == "live_arming_ledger_invalid"
    ]
    assert len(invalid) == 1, "the fault is logged once per transition, not four times a minute"
```

Append to `tests/contracts/test_alpaca_active_authority_wiring.py`:

```python
def test_the_shadow_composition_hands_the_sync_the_accounts_arming_ledger() -> None:
    """ADR 0059 D3/R10, pinned structurally because the failure is a missing call.

    Without the ledger the gate is unsealed forever, ``envelope_agreement`` can
    never leave ``unsealed``, and ``LIVE_ENVELOPE_DISAGREEMENT`` can never be
    refused -- and every unit test still passes.
    """
    shadow_source = (APPLICATION_ROOT / "broker/alpaca/clerk/shadow_authority.py").read_text(encoding="utf-8")
    runtime_source = (APPLICATION_ROOT / "broker/alpaca/clerk/active_runtime.py").read_text(encoding="utf-8")

    assert (
        "arming_ledger=LiveArmingLedger(artifacts_root, live_account_id=account.account_id)" in shadow_source
    ), (
        "the shadow authority must build the arming ledger on the LIVE account id; the "
        "shadow: custody namespace is not where an arming record lives"
    )
    assert "arming_ledger=arming_ledger," in runtime_source, (
        "compose_repository_runtime must hand the arming ledger to LiveEnvelopeSync, or "
        "the sealed envelope is never refreshed"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py tests/contracts/test_alpaca_active_authority_wiring.py -q -p no:cacheprovider`
Expected: the four new runtime tests fail (the gate stays `sealed is None` after a tick, so `agreement` stays `"unsealed"`), and the new wiring test fails on both assertions.

- [ ] **Step 3: Refresh the sealed envelope on every tick**

In `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py`, add to the first-party imports (they sort above the existing `live_envelope` import):

```python
from app.broker.alpaca.clerk.live_arming import LiveArmingInvalid
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
```

Add the parameter to `__init__` (last, after `max_ticks`), and two attributes:

```python
        arming_ledger: LiveArmingLedger | None = None,
```

```python
        self._arming_ledger = arming_ledger
        # Whether the last refresh found the ledger unreadable, so the error is
        # logged once per transition rather than four times a minute.
        self._arming_ledger_invalid = False
```

Add the method (immediately above `tick`):

```python
    def _refresh_sealed_envelope(self) -> None:
        """Re-read the account's sealed envelope from the arming ledger (ADR 0059 D3/R10).

        The envelope a live ENTER is admitted against is the one an operator
        sealed at arming, not the one this process booted with -- so an edit to
        the ``ALPACA_LIVE_*`` environment after an arming is
        ``LIVE_ENVELOPE_DISAGREEMENT`` at every ENTER until a re-arm.

        Read on every tick rather than once at composition because an arming can
        happen while the service is running: the ceremony is an out-of-process
        CLI, and one sync cadence is the whole latency between an operator's
        ``apply`` and the gate that enforces it.

        A ledger nobody can read seals nothing: the gate returns to unsealed and
        the fault is logged at error level, once per transition. The account
        level is all this slice enforces; per-instance arming is consulted at
        admission in slice 7.
        """
        if self._arming_ledger is None:
            return
        try:
            sealed = self._arming_ledger.sealed_envelope()
        except LiveArmingInvalid as exc:
            sealed = None
            if not self._arming_ledger_invalid:
                logger.error(
                    "live arming ledger cannot be read; the envelope is unsealed",
                    extra={
                        "action": "live_arming_ledger_invalid",
                        "account_id": self._repo.account_id,
                        "path": str(self._arming_ledger.path),
                        "why": str(exc),
                    },
                )
            self._arming_ledger_invalid = True
        else:
            self._arming_ledger_invalid = False
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
                "observed_account_id": self._observed_account_id,
                "agreement": self.envelope.agreement,
            },
        )
```

And make it the first statement of `tick()`, before the `try:` that wraps `observe()`:

```python
    async def tick(self) -> EnvelopeSyncAction:
        """..."""
        self._refresh_sealed_envelope()
        try:
            reading = await self.observe()
```

(Leave the rest of `tick`'s body and its docstring exactly as they are.)

- [ ] **Step 4: Thread the ledger through composition**

In `app/broker/alpaca/clerk/active_runtime.py`, add `LiveArmingLedger` to the **`TYPE_CHECKING`** block beside `LiveEnvelopeGate` — a plain import there sorts above the `clerk.sqlite` imports and would run `sqlite/__init__` while `live_envelope` is still half-built, which is exactly what the comment above that block already explains:

```python
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
```

Add the parameter to `compose_repository_runtime`, after `envelope_read`:

```python
    arming_ledger: LiveArmingLedger | None = None,
```

and document it in that function's docstring by appending one paragraph:

```
    ``arming_ledger`` is the account's sealed-arming evidence (ADR 0059 D3). The
    envelope sync re-reads it every tick so an arming performed by the
    out-of-process CLI reaches the running gate within one cadence.
```

Pass it to the sync:

```python
        envelope_sync = (
            None
            if live_envelope is None
            else LiveEnvelopeSync(
                repo=repository,
                read=(
                    guarded_read
                    if envelope_read is None
                    else guard_broker_read_port(envelope_read, intake=intake)
                ),
                envelope=live_envelope,
                arming_ledger=arming_ledger,
            )
        )
```

In `app/broker/alpaca/clerk/shadow_authority.py`, add the import (it sorts between the `active_runtime` block and the `live_envelope` line):

```python
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
```

and add one argument to the `compose_repository_runtime(...)` call, immediately after `envelope_read=read,`:

```python
            # ADR 0059 D3/R10: the sealed envelope comes from this account's
            # arming ledger, which is rooted on the LIVE account id -- not the
            # ``shadow:`` custody namespace the rest of this composition uses.
            arming_ledger=LiveArmingLedger(artifacts_root, live_account_id=account.account_id),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py tests/broker/alpaca/clerk/sqlite/test_live_envelope_sync.py tests/broker/alpaca/clerk/test_active_runtime_taps.py tests/broker/alpaca/clerk/test_active_authority.py tests/broker/v2panel/test_shadow_operator_surfaces.py tests/contracts/test_alpaca_active_authority_wiring.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main" && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: all pass; `import app.main` clean (this is the import-cycle check — if it fails with `ImportError: cannot import name 'EnvelopeReservation'`, you imported `LiveArmingLedger` at module level in `active_runtime.py` instead of under `TYPE_CHECKING`); ruff clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py PythonDataService/app/broker/alpaca/clerk/active_runtime.py PythonDataService/app/broker/alpaca/clerk/shadow_authority.py PythonDataService/tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py PythonDataService/tests/contracts/test_alpaca_active_authority_wiring.py
git commit -m "feat(arming): the arming ledger seals the live envelope gate every tick

An ALPACA_LIVE_* edit after an arming now refuses every ENTER
LIVE_ENVELOPE_DISAGREEMENT until a re-arm; an unreadable ledger unseals
the gate and is logged once at error level.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Verdict truth — the count, the sealed state, and `live-armed`

**Files:**
- Modify: `PythonDataService/app/services/alpaca_live_verdict.py` (imports at 9–27; add `ArmingObservation` + `observe_arming` after `observe_loss_hold` at line 84; `alpaca_live_verdict`'s signature at 87–94; the live branch at 173–225)
- Modify: `PythonDataService/app/routers/brokers.py` (the `observe_*` import block at 95–99; `get_live_verdict` at 709–735)
- Test: `PythonDataService/tests/services/test_alpaca_live_verdict.py` (append)
- Test: `Frontend/src/app/shell/alpaca-live-banner.component.spec.ts` (append one case)

**Interfaces:**
- Consumes (Task 2): `LiveArmingInvalid`, `ArmingStatus(state, reason_code, record, sessions_used, sessions_remaining)`, `LiveArmingRecord.create(...)`.
- Consumes (Task 3): `LiveArmingLedger(artifacts_root, live_account_id=...)` with `latest_arming()`.
- Consumes (Task 4): `account_arming_statuses(*, live_account_id, artifacts_root, live_state_root, configured_envelope, now_ms, strategy_instance_ids=None) -> dict[str, ArmingStatus]`; and from `tests/broker/alpaca/clerk/live_arming_fixtures.py` (created by Task 4): `ARMED_AT_MS`, `ARMING_SID`, `arming_ready`, `live_settings`.
- Consumes from the repo: `LiveEnvelopeValues.from_settings(settings)`, `LiveEnvelopeIncomplete`, `SHADOW_ACCOUNT_PREFIX`, `SQLITE_FACADE_AUTHORITIES: frozenset[str]` (`{"sqlite", "shadow"}`, re-exported by `active_authority`), `EnvelopeState = Literal["not_applicable", "configured_unsealed", "sealed"]`, `live_artifacts_root() -> Path`.
- Produces:
  - `@dataclass(frozen=True) class ArmingObservation(armed_instance_count: int, envelope_state: EnvelopeState, detail: str)` with `@classmethod none() -> ArmingObservation`
  - `observe_arming(runtime: ActiveClerkRuntime | None, artifacts_root: Path, live_state_root: Path, *, settings: AlpacaSettings, now_ms: int) -> ArmingObservation`
  - `alpaca_live_verdict(*, settings, runtime, now_ms, shadow_state=None, loss_hold=None, arming: ArmingObservation | None = None) -> AlpacaLiveVerdict`
  - **No schema change**: `AlpacaLiveVerdict` gains no field, so `scripts/export_openapi_contract.py --check` must still report the snapshot unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_alpaca_live_verdict.py`. Add to its imports:

```python
from datetime import date
from pathlib import Path

from app.broker.alpaca.clerk.live_arming import LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.services.alpaca_live_verdict import ArmingObservation, observe_arming
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    arming_ready,
    live_settings,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES
```

Then append:

```python
MONDAY_MS = et_minute_of_day_ms(date(2026, 9, 14), 10 * 60)


def _arm_on_disk(
    artifacts_root: Path,
    live_state_root: Path,
    *,
    strategy_instance_id: str = ARMING_SID,
    armed_at_ms: int = ARMED_AT_MS,
    max_sessions: int = 20,
) -> LiveArmingRecord:
    """One sealed instance and one arming record for it, both on disk."""
    seal = arming_ready(artifacts_root, live_state_root, strategy_instance_id=strategy_instance_id)
    record = LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=strategy_instance_id,
        seal_hash=seal.bot_configuration_hash,
        configured_signal_hash=seal.configured_signal_hash,
        shadow_receipt_sha256="c" * 64,
        envelope=TEST_ENVELOPE_VALUES,
        armed_at_ms=armed_at_ms,
        max_sessions=max_sessions,
    )
    LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).append(record)
    return record


def test_an_absent_arming_observation_keeps_the_slice_five_verdict() -> None:
    """Every caller that has not been taught to observe arming still gets the truth it had."""
    verdict = alpaca_live_verdict(settings=_live(), runtime=_shadow_runtime(), now_ms=_NOW, shadow_state="none")

    assert verdict.armed_instance_count == 0
    assert verdict.envelope_state == "configured_unsealed"
    assert verdict.final_verdict == "live-unarmed"


def test_one_armed_instance_makes_the_verdict_live_armed_and_the_envelope_sealed() -> None:
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_shadow_runtime(),
        now_ms=_NOW,
        shadow_state="complete",
        arming=ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail=""),
    )

    assert verdict.armed_instance_count == 1
    assert verdict.envelope_state == "sealed"
    assert verdict.final_verdict == "live-armed"
    assert "1 instance armed" in verdict.headline
    assert "LIVE account 9LIVE0001 " in verdict.headline
    assert "no real-money order" in verdict.detail or "No path submits a real-money order" in verdict.detail
    assert "slice 7" in verdict.detail


def test_the_headline_counts_more_than_one_armed_instance_in_the_plural() -> None:
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_shadow_runtime(),
        now_ms=_NOW,
        shadow_state="complete",
        arming=ArmingObservation(armed_instance_count=3, envelope_state="sealed", detail=""),
    )

    assert "3 instances armed" in verdict.headline


def test_an_armed_count_without_an_installed_clerk_is_never_live_armed() -> None:
    """R11: the verdict is ``live-armed`` only where custody could exist at all."""
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001"),
        now_ms=_NOW,
        arming=ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail=""),
    )

    assert verdict.armed_instance_count == 1
    assert verdict.final_verdict == "live-unarmed"


def test_a_lapsed_or_disarmed_instance_is_named_in_the_detail_with_its_reason_code() -> None:
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_shadow_runtime(),
        now_ms=_NOW,
        shadow_state="complete",
        arming=ArmingObservation(
            armed_instance_count=0,
            envelope_state="sealed",
            detail=" Not armed: s1 (LIVE_ARMING_LAPSED); s2 (LIVE_ARMING_REVOKED).",
        ),
    )

    assert verdict.final_verdict == "live-unarmed"
    assert verdict.envelope_state == "sealed"
    assert "s1 (LIVE_ARMING_LAPSED)" in verdict.detail
    assert "s2 (LIVE_ARMING_REVOKED)" in verdict.detail


def test_paper_stays_untouched_even_when_an_arming_observation_is_supplied() -> None:
    verdict = alpaca_live_verdict(
        settings=_paper(),
        runtime=None,
        now_ms=_NOW,
        arming=ArmingObservation(armed_instance_count=2, envelope_state="sealed", detail=" Not armed: x."),
    )

    assert verdict.final_verdict == "paper"
    assert verdict.armed_instance_count == 0
    assert verdict.envelope_state == "not_applicable"
    assert "Not armed" not in verdict.detail


def test_observe_arming_counts_the_ledgers_armed_instances(tmp_path: Path) -> None:
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root)

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        live_state_root,
        settings=live_settings(),
        now_ms=ARMED_AT_MS,
    )

    assert observation == ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail="")


def test_observe_arming_names_a_lapsed_instance_and_counts_it_out(tmp_path: Path) -> None:
    """Armed Friday with a one-session grant; by Monday two sessions are spent."""
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root, max_sessions=1)

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        live_state_root,
        settings=live_settings(),
        now_ms=MONDAY_MS,
    )

    assert observation.armed_instance_count == 0
    assert observation.envelope_state == "sealed"
    assert f"{ARMING_SID} (LIVE_ARMING_LAPSED)" in observation.detail


def test_observe_arming_fails_closed_on_an_unreadable_ledger(tmp_path: Path) -> None:
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root)
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"max_sessions":20', '"max_sessions":90'),
        encoding="utf-8",
    )

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        live_state_root,
        settings=live_settings(),
        now_ms=ARMED_AT_MS,
    )

    assert observation.armed_instance_count == 0
    assert observation.envelope_state == "configured_unsealed"
    assert "cannot be read" in observation.detail


def test_observe_arming_reads_nothing_off_a_paper_or_absent_authority(tmp_path: Path) -> None:
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root)

    for runtime in (None, ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001")):
        assert (
            observe_arming(
                runtime, artifacts_root, live_state_root, settings=live_settings(), now_ms=ARMED_AT_MS
            )
            == ArmingObservation.none()
        )
```

Append to `Frontend/src/app/shell/alpaca-live-banner.component.spec.ts`, inside the existing `describe` block:

```typescript
  it('renders a live-armed account in the loudest treatment with the armed count', async () => {
    await renderWith(
      verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        armed_instance_count: 1,
        envelope_state: 'sealed',
        envelope_agreement: 'agreed',
        shadow_state: 'complete',
        final_verdict: 'live-armed',
        headline: 'LIVE account 9LIVE0001 — 1 instance armed, nothing submitted yet',
        detail: 'No path submits a real-money order in this slice.',
      }),
    );
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-armed');
    expect(status.textContent).toContain('1 instance armed');
    expect(status.textContent).toContain('1 armed');
  });
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'ArmingObservation' from 'app.services.alpaca_live_verdict'`.

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/Frontend && npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts'`
Expected: run it and confirm the new case **passes** immediately. This case is not red-first: the `is-live-armed` rendering already exists in the component and was unreachable only because nothing produced a `live-armed` verdict. The case is what makes it reachable evidence; Step 3's Python change is what makes the verdict real.

- [ ] **Step 3: Add the arming observation to the verdict service**

In `app/services/alpaca_live_verdict.py`, extend the imports:

```python
from dataclasses import dataclass
```
```python
from app.broker.alpaca.clerk.active_authority import SQLITE_FACADE_AUTHORITIES, ActiveClerkRuntime
from app.broker.alpaca.clerk.live_arming import LiveArmingInvalid
from app.broker.alpaca.clerk.live_arming_ceremony import account_arming_statuses
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
```
and add `EnvelopeState` to the `app.schemas.alpaca_live_verdict` import list. Add a module logger under the imports:

```python
logger = logging.getLogger(__name__)
```
(with `import logging` in the stdlib block).

Insert after `observe_loss_hold` (which ends at line 84):

```python
@dataclass(frozen=True)
class ArmingObservation:
    """What the durable arming ledger says for one live account (read-only).

    ``detail`` is a fragment of backend-authored operator prose, appended to the
    verdict's detail sentence. It names every instance the ledger knows that is
    *not* armed, with its reason code, because "0 armed" and "0 armed, and here
    is the one that lapsed last Tuesday" are different operator situations.
    """

    armed_instance_count: int
    envelope_state: EnvelopeState
    detail: str

    @classmethod
    def none(cls) -> ArmingObservation:
        """No arming evidence was consulted, and none is claimed."""
        return cls(armed_instance_count=0, envelope_state="configured_unsealed", detail="")


def observe_arming(
    runtime: ActiveClerkRuntime | None,
    artifacts_root: Path,
    live_state_root: Path,
    *,
    settings: AlpacaSettings,
    now_ms: int,
) -> ArmingObservation:
    """Count this live account's armed instances from durable evidence (read-only).

    No database is opened and the broker is never contacted: the arming ledger,
    the runner's sealed bindings and the configured envelope are the only
    inputs. Fails closed -- a ledger that will not verify counts no instance and
    says so in the detail, rather than reporting an account as unarmed for a
    reason nobody can see.
    """
    if (
        runtime is None
        or runtime.authority_kind != "shadow"
        or runtime.selected_account_id is None
        or settings.is_paper
    ):
        return ArmingObservation.none()
    live_account_id = runtime.selected_account_id.removeprefix(SHADOW_ACCOUNT_PREFIX)
    try:
        statuses = account_arming_statuses(
            live_account_id=live_account_id,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            configured_envelope=LiveEnvelopeValues.from_settings(settings),
            now_ms=now_ms,
        )
        # A second read of the same small file, deliberately: "the ledger holds
        # an arming record" is not "some instance is currently armed", and a
        # disarmed account is still a sealed one.
        sealed = LiveArmingLedger(artifacts_root, live_account_id=live_account_id).latest_arming()
    except (LiveArmingInvalid, LiveEnvelopeIncomplete) as exc:
        logger.error(
            "the arming ledger cannot be read; the verdict counts no armed instance",
            extra={
                "action": "live_arming_ledger_invalid",
                "account_id": live_account_id,
                "why": str(exc),
            },
        )
        return ArmingObservation(
            armed_instance_count=0,
            envelope_state="configured_unsealed",
            detail=f" The arming ledger cannot be read ({exc}); no instance is counted as armed.",
        )
    not_armed = [
        f"{strategy_instance_id} ({status.reason_code})"
        for strategy_instance_id, status in sorted(statuses.items())
        if status.state != "armed" and status.reason_code is not None
    ]
    return ArmingObservation(
        armed_instance_count=sum(1 for status in statuses.values() if status.state == "armed"),
        envelope_state="configured_unsealed" if sealed is None else "sealed",
        detail="" if not not_armed else f" Not armed: {'; '.join(not_armed)}.",
    )
```

- [ ] **Step 4: Make the live branch tell the truth**

Add the parameter to `alpaca_live_verdict`'s signature (after `loss_hold`):

```python
    arming: ArmingObservation | None = None,
```

and one paragraph to its docstring:

```
    ``arming`` is the caller's observation of the durable arming ledger. It is
    read only on an agreed live account; paper and unknown verdicts keep the
    empty count they already published.
```

Replace the live branch's tail — everything from `observed_loss_hold: LossHoldState = ...` (line 188) to the closing `)` of the `return AlpacaLiveVerdict(...)` (line 225) — with:

```python
    observed_loss_hold: LossHoldState = loss_hold if loss_hold is not None else "not_applicable"
    held = observed_loss_hold == "held"
    # ADR 0059 D8 / design R11: ``armed`` is a fact about durable arming
    # records, and ``live-armed`` additionally requires a Clerk that could hold
    # custody at all -- an armed record under a refused authority is a
    # permission nothing can act on, and must not read as the loudest state.
    observed_arming = arming if arming is not None else ArmingObservation.none()
    armed = observed_arming.armed_instance_count
    live_armed = armed >= 1 and authority in SQLITE_FACADE_AUTHORITIES
    instances = f"{armed} instance{'' if armed == 1 else 's'}"
    return AlpacaLiveVerdict(
        configured_mode="live",
        observed_account_id=account_id,
        mode_agreement="agreed",
        clerk_authority=authority,
        clerk_refusal_reason_code=refusal,
        armed_instance_count=armed,
        envelope_state=observed_arming.envelope_state,
        envelope_agreement=envelope_agreement,
        loss_hold=observed_loss_hold,
        shadow_state=observed_shadow,
        final_verdict="live-armed" if live_armed else "live-unarmed",
        headline=(
            f"LIVE account {named_account_id} — {instances} armed, nothing submitted yet"
            if live_armed
            else f"LIVE account {named_account_id} — shadow authority active, no instance armed"
            if shadow_active
            else f"LIVE account {named_account_id} — real money, no instance armed"
        )
        + (" — loss hold" if held else ""),
        detail=(
            "This is a real-money Alpaca account and an operator has armed "
            f"{instances} on it. No path submits a real-money order in this slice: the "
            "shadow authority still synthesizes every fill, and ADR 0059 slice 7 is what "
            "opens submission."
            if live_armed
            else "This is a real-money Alpaca account. Its shadow authority reads it and "
            "synthesizes every fill; nothing is submitted. Arming requires a completed "
            "shadow receipt and the supervised ceremony (ADR 0059)."
            if shadow_active
            else "This is a real-money Alpaca account. No sealed instance is armed, so "
            "every order path refuses. Arming requires a completed shadow receipt "
            "and the supervised ceremony (ADR 0059)."
        )
        + observed_arming.detail
        + (
            " The account is in loss hold: every ENTER is refused until an operator "
            "clears it with POST /api/brokers/alpaca/live-envelope/loss-hold/clear; "
            "exits still run."
            if held
            else ""
        ),
        observed_at_ms=now_ms,
    )
```

Also delete the now-stale comment on line 173 (`# Slice 1: no arming exists, so an agreed live account is always unarmed.`) — it is no longer true.

- [ ] **Step 5: Let the router supply the observation**

In `app/routers/brokers.py`, extend the service import block:

```python
from app.services.alpaca_live_verdict import (
    alpaca_live_verdict,
    observe_arming,
    observe_loss_hold,
    observe_shadow_state,
)
```

and add `from app.broker.ibkr.config import live_artifacts_root` in first-party import order (confirm with `grep -n "from app.broker.ibkr" app/routers/brokers.py` and put it beside whatever ibkr import already exists; if none exists, add it in sorted position).

Replace the body of `get_live_verdict` from `runtime = get_active_clerk_runtime()` to the end with:

```python
    runtime = get_active_clerk_runtime()
    # One instant for the whole verdict: the lapse count and the stamp the
    # banner shows must describe the same moment.
    observed_at_ms = now_ms_utc()
    return alpaca_live_verdict(
        settings=alpaca_settings,
        runtime=runtime,
        now_ms=observed_at_ms,
        shadow_state=(
            None if alpaca_settings is None else observe_shadow_state(runtime, alpaca_settings.clerk_dir)
        ),
        loss_hold=observe_loss_hold(runtime),
        arming=(
            None
            if alpaca_settings is None
            else observe_arming(
                runtime,
                alpaca_settings.clerk_dir,
                live_artifacts_root(),
                settings=alpaca_settings,
                now_ms=observed_at_ms,
            )
        ),
    )
```

and extend the endpoint's docstring's last sentence to `settings, the clerk selection outcome, and a read of the durable shadow and arming evidence are the only inputs.`

- [ ] **Step 6: Run every gate this task touches**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py tests/routers/test_alpaca_live_verdict_endpoint.py -q -p no:cacheprovider && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main" && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: all tests pass; the contract check reports the snapshot **unchanged** (no schema moved — if it reports a difference you added a field somewhere; revert that rather than regenerating); `import app.main` clean; ruff clean.

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/Frontend && npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts' && npx eslint src/ --max-warnings 0
```
Expected: all banner cases pass (8 now); eslint clean.

- [ ] **Step 7: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add PythonDataService/app/services/alpaca_live_verdict.py PythonDataService/app/routers/brokers.py PythonDataService/tests/services/test_alpaca_live_verdict.py Frontend/src/app/shell/alpaca-live-banner.component.spec.ts
git commit -m "feat(arming): the live verdict counts armed instances and can say live-armed

envelope_state is sealed once the ledger holds an arming record; a lapsed or
disarmed instance is named in the detail with its reason code. No schema
change: every value was already declared.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Docs — the reference note, the glossary, the cross-links and the authority map

**Files:**
- Create: `docs/references/alpaca-live-arming.md`
- Modify: `CONTEXT.md:491-495` (the **Arming** entry, plus one new **Arming lapse** entry after it)
- Modify: `docs/references/alpaca-live-envelope.md` (one bullet under `## Where it runs`; the `LIVE_ENVELOPE_DISAGREEMENT` row at line 102; the `unsealed` row at line 169)
- Modify: `docs/references/alpaca-shadow-authority.md:395-397` (the `LIVE_SHADOW_INCOMPLETE` follow-up is now a real code)
- Modify: `docs/architecture/engine-authority-map.md` (line 4's "Last reviewed" list; one new row after line 64)

**Interfaces:**
- Consumes: every name Tasks 1–7 produced. This task writes no code and changes no behaviour.
- Produces: nothing importable.

- [ ] **Step 1: Match the neighbouring documents' voice and row shapes**

Run: `cd /Users/inkant/learn-ai/.claude/worktrees/slice-6 && sed -n '1,62p' docs/references/alpaca-live-envelope.md && grep -n '^## ' docs/references/alpaca-live-envelope.md && sed -n '64p' docs/architecture/engine-authority-map.md`
The reference note below already follows that structure (a `**Status:**` line, `## What it is`, `## Where it runs` with full `PythonDataService/...::symbol` paths, tables for the facts and refusals, `## Residuals`, `## Decision record`). Keep it.

- [ ] **Step 2: Write `docs/references/alpaca-live-arming.md`**

The whole file, verbatim (the outer fence below is four backticks; the note's own three-backtick blocks are part of the file):

````markdown
# Alpaca live arming — the ceremony, the ledger, and the lapse

**Status:** canonical for ADR 0059 slice 6 (2026-09-09). Lineage: live.

## What it is

Real-money submission is permitted per **sealed instance**, never per account
and never by configuration. The permission is a supervised ceremony modelled on
the SQLite cutover: `plan` is read-only and re-observes every input, `apply`
re-observes again, accepts no force mode, and honours the same confirmation
window (120 s default, 300 s maximum). What `apply` writes is one sha256-sealed
`LiveArmingRecord` on an append-only, account-rooted ledger.

An arming is bound to what it named. A change to the instance's sealed program,
to any `ALPACA_LIVE_*` value, or to the account disarms it; re-arming is the
same ceremony run again. An arming also **lapses** after
`ALPACA_LIVE_ARMING_MAX_SESSIONS` calendar NYSE trading sessions — a deliberate
"come back and look" fence, because an armed bot nobody has looked at in a
month is the configuration accident this ADR was written to prevent.

**Nothing here submits a real-money order.** In this slice an arming record is
evidence: the runtime reads it to seal the risk envelope (below), and the live
verdict counts it. ADR 0059 slice 7 is what teaches ENTER admission to read it.
Every JSON object the CLI prints says so, in a `submission_admitted: false`
field and a `note`.

## Where it runs

- `PythonDataService/app/broker/alpaca/clerk/live_arming.py` — the two records,
  their sealing and verification, the reason codes, and the pure status rule
  (`arming_status`, `sessions_used`). No I/O, no clock, no broker.
- `PythonDataService/app/broker/alpaca/clerk/live_arming_ledger.py::LiveArmingLedger`
  — the append-only ledger at
  `<clerk_dir>/accounts/arming/<live_account_id>/live_arming.jsonl`, written
  under the advisory file lock with the same `sealed_ledger.py` discipline the
  shadow receipts use. Its own tree, deliberately: not `accounts/alpaca/<id>/`
  and not inside a custody namespace directory, so no custody-detection path
  (cutover initialization, the latent-database checks) can mistake an arming
  ledger for an authority.
- `PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py` —
  `observe_arming_inputs`, `plan_arming`, `apply_arming`, `disarm`,
  `account_arming_statuses`.
- `PythonDataService/app/broker/alpaca/clerk/ceremony.py` — the plan token, the
  TTL bounds and the three confirmation checks, shared with
  `clerk/sqlite/cutover.py`, which invented the shape.
- `PythonDataService/scripts/manage_alpaca_arming.py` — the operator entry
  point, and the only writer.
- `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync._refresh_sealed_envelope`
  — re-reads the ledger on every 15 s tick and assigns `LiveEnvelopeGate.sealed`
  from the newest arming record's own `envelope_values`.
- `PythonDataService/app/services/alpaca_live_verdict.py::observe_arming` — the
  verdict's `armed_instance_count` and `envelope_state`.

## The four inputs

`plan` reads each of these, and `apply` reads every one of them again and
refuses if any has moved (`LIVE_ARMING_INPUTS_CHANGED`). The ceremony never
contacts the broker: mode agreement against Alpaca stays the runtime's job at
boot, and slice 7's at admission.

| Input | Where it comes from | Refusal if absent |
|---|---|---|
| Settings | `ALPACA_MODE=live` and a complete `LiveEnvelopeValues.from_settings` | `LIVE_ENVELOPE_MISSING` |
| The live account id | the shadow activation fence under the artifacts root — **observed, never supplied**, so an arming cannot name an account no shadow gate was run against | `LIVE_ARMING_INSTANCE_UNSEALED` |
| The instance's sealed binding on that account | `BotBindingRepository.list_for_broker("alpaca")`, filtered to `sealed_account_id` in `{<live_account_id>, shadow:<live_account_id>}` | `LIVE_ARMING_INSTANCE_UNSEALED` |
| A current shadow receipt | `ShadowReceiptStore.current(sid, configured_signal_hash=…, required_sessions=ALPACA_LIVE_SHADOW_SESSIONS)` | `LIVE_SHADOW_INCOMPLETE` |

Both custody ids are admissible for the same account because shadow custody
seals `shadow:<live_account_id>` while slice 7's `real_live` custody will seal
the live id itself. They are one account to an operator.

## The record

```json
{"kind":"armed","schema_version":1,"live_account_id":"9LIVE0001",
 "strategy_instance_id":"ema-shadow-1","seal_hash":"<64 hex>",
 "configured_signal_hash":"<64 hex>","shadow_receipt_sha256":"<64 hex>",
 "envelope_values":{"arming_max_sessions":20,"loss_fraction":0.05,"loss_usd":5000.0,
                    "shadow_sessions":3,"xh_entry_bps":10.0,"xh_exit_bps":10.0},
 "envelope_sha256":"<64 hex>","armed_at_ms":1789394400000,"max_sessions":20,
 "record_sha256":"<64 hex>"}
```

`record_sha256` is the canonical sha256 of every other field. A reader
recomputes it *and* checks that `envelope_sha256` is the sha of the
`envelope_values` beside it; either mismatch is `LiveArmingInvalid` and the
whole ledger refuses to answer.

`seal_hash` is the instance's **whole** sealed-program hash
(`SealedBotProgram.bot_configuration_hash`), not the signal-only
`configured_signal_hash`. Arming binds to what will trade — size, action plan
and account included — so changing the size alone disarms the instance.
`configured_signal_hash` is carried beside it because that is what the shadow
receipt binds to.

A revocation is the same file, one line, `"kind": "disarmed"`:

```json
{"kind":"disarmed","schema_version":1,"live_account_id":"9LIVE0001",
 "strategy_instance_id":"ema-shadow-1","revokes_record_sha256":"<64 hex>",
 "disarmed_at_ms":1789394405000,"record_sha256":"<64 hex>"}
```

## States and codes

The instance's **latest** row decides, and the checks run in this order. The
first failure names the state, which is why an instance whose seal changed
*and* whose sessions are spent reports the seal: re-sealing is what its
operator has to do first.

| Order | Condition | State | `reason_code` |
|---|---|---|---|
| — | no row for this instance | `unarmed` | *(none)* |
| 1 | the latest row is a revocation | `disarmed` | `LIVE_ARMING_REVOKED` |
| 2 | `seal_hash` differs from the instance's current sealed-program hash (an absent binding counts as different) | `disarmed` | `LIVE_ARMING_SEAL_CHANGED` |
| 3 | `envelope_sha256` differs from the configured envelope's sha | `disarmed` | `LIVE_ENVELOPE_DISAGREEMENT` |
| 4 | `sessions_used > max_sessions` | `lapsed` | `LIVE_ARMING_LAPSED` |
| — | otherwise | `armed` | *(none)* |

The ceremony's own refusals, each raised as a `LiveArmingRefused` the CLI
prints and exits `2` on: `LIVE_ENVELOPE_MISSING`,
`LIVE_ARMING_INSTANCE_UNSEALED`, `LIVE_SHADOW_INCOMPLETE`,
`LIVE_ARMING_TTL_INVALID`, `LIVE_ARMING_TOKEN_INVALID`,
`LIVE_ARMING_PLAN_EXPIRED`, `LIVE_ARMING_INPUTS_CHANGED`,
`LIVE_ARMING_NOT_ARMED`.

## Lapse, and a worked example

```
sessions_used = trading_session_count(ET date of armed_at_ms, ET date of now_ms)
lapsed        = sessions_used > max_sessions
```

Both ET dates are inclusive and both come from the canonical calendar
(`app/lean_sidecar/trading_calendar.py`) and nowhere else, so there is no
session literal anywhere on this path. The arming session counts as one — an
arming at 15:59 ET spends a whole session on a minute, which is why `status`
reports `sessions_remaining` rather than a wall-clock expiry.

Worked, with `ALPACA_LIVE_ARMING_MAX_SESSIONS=2`, armed **Friday 2026-09-11 at
10:00 ET**:

| ET date | A session? | `sessions_used` | `sessions_remaining` | State |
|---|---|---|---|---|
| Fri 2026-09-11 | yes | 1 | 1 | `armed` |
| Sat 2026-09-12 | no | 1 | 1 | `armed` |
| Sun 2026-09-13 | no | 1 | 1 | `armed` |
| Mon 2026-09-14 | yes | 2 | 0 | `armed` — the last session it bought |
| Tue 2026-09-15 | yes | 3 | 0 | `lapsed`, `LIVE_ARMING_LAPSED` |

The weekend spends nothing. So does a market holiday: armed **Wednesday
2026-11-25** with the same grant, Thanksgiving (Thu 2026-11-26) is not a
session, the half day (Fri 2026-11-27) is, and the arming lapses on Mon
2026-11-30 — its third session.

An arming performed on a non-trading ET date simply starts counting at the next
session, which is what the inclusive count over the calendar already says.

## How the envelope gets sealed

Slice 5 shipped `envelope_agreement` with `sealed` permanently `None`, so it
could only ever answer `unsealed` and `LIVE_ENVELOPE_DISAGREEMENT` was
unreachable. Slice 6 supplies the missing half: on every 15 s tick
`LiveEnvelopeSync` re-reads the account's arming ledger and assigns
`LiveEnvelopeGate.sealed` from the **newest arming record's** own
`envelope_values` (a revocation never unseals an account — it withdraws one
instance's permission). Each transition is logged once, `live_envelope_sealed`
or `live_envelope_unsealed`, with the resulting agreement.

The effect: once any instance on the account has been armed, editing an
`ALPACA_LIVE_*` value makes every rehearsal ENTER refuse
`LIVE_ENVELOPE_DISAGREEMENT` through the existing `require_envelope_admission`,
until a re-arm seals the new numbers. Changing a bound is a re-arm, never a
silent drift.

A ledger nobody can read seals nothing: `LiveArmingInvalid` returns the gate to
unsealed and is logged at **error** level, once per transition
(`live_arming_ledger_invalid`), and the verdict counts zero armed instances and
names the fault in its detail.

Per-instance arming is **not** consulted at ENTER admission in this slice. The
sealed envelope is account-level; slice 7 is where an individual instance's
arming decides whether its order may be submitted.

## Operator recipe

Every command writes exactly one JSON object to stdout. Exit `0` answered,
`1` the command cannot be run as asked, `2` the ceremony refused.

```bash
cd PythonDataService

# What is armed on the shadowed live account, and how much of each grant is left.
python -m scripts.manage_alpaca_arming status

# Propose an arming. Read-only: it writes nothing but the optional plan file.
python -m scripts.manage_alpaca_arming plan \
    --strategy-instance-id ema-shadow-1 --plan-out /tmp/arming-plan.json

# Confirm it, within 120 s, quoting the token the plan printed.
python -m scripts.manage_alpaca_arming apply \
    --plan-file /tmp/arming-plan.json --confirmation-token <confirmation_token>

# Revoke before the lapse. One append, no confirmation: the closed direction.
python -m scripts.manage_alpaca_arming disarm --strategy-instance-id ema-shadow-1
```

`--artifacts-root`, `--live-state-root` and `--now-ms` exist for tests and for
an operator pointing at a non-default tree. There is no force mode and no HTTP
route: an arming is a supervised, out-of-process act.

## In the live verdict

| Field | What slice 6 makes it say |
|---|---|
| `armed_instance_count` | how many instances bound to this live account are `armed` right now |
| `envelope_state` | `sealed` once the ledger holds at least one arming record, else `configured_unsealed` |
| `final_verdict` | `live-armed` when the account is live, mode agreed, a Clerk installed and the count is at least 1; otherwise `live-unarmed` |

The headline names the count; the detail states that no path submits a
real-money order yet and names every instance the ledger knows that is not
armed, with its reason code. The schema is unchanged — every one of these
values was already declared in slice 1.

## Residuals

- **An arming record admits nothing.** Slice 7 is what reads it at ENTER
  admission. The CLI and the verdict both say so rather than leaving it to be
  inferred.
- **The ceremony does not re-observe the broker.** Mode agreement is proven at
  boot by `select_active_clerk_runtime` and (slice 7) again at admission. An
  arming that named a live account whose broker mode later disagreed is
  refused there, not here.
- **The sealed envelope is account-level under shadow.** Per-instance
  disagreement is reported by `status` and by the verdict; it is enforced per
  instance at admission in slice 7.
- **One shadowed account per artifacts root.** `live_account_id_for` refuses
  rather than choosing when the activation fence names two, because arming has
  no basis to pick one.
- **`ALPACA_LIVE_ARMING_MAX_SESSIONS` has no upper bound in code** — the owner
  rejected numbers in code. The plan output shows exactly how many sessions the
  arming buys, so an implausible grant is visible at the moment it is confirmed.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decision 3 (arming), Decision 4's sealing sentence (the envelope is read and
sealed at arming; a difference between the environment and the sealed record is
`LIVE_ENVELOPE_DISAGREEMENT`) and Decision 8 (the verdict's
`armed_instance_count`, `envelope_state`, `final_verdict`). The controller
rulings R1–R14 that fill in what the ADR left open are recorded in
`docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md`.
Predecessors: [alpaca-shadow-authority](alpaca-shadow-authority.md) (the
receipt this ceremony reads) and [alpaca-live-envelope](alpaca-live-envelope.md)
(the values it seals).
````

- [ ] **Step 3: Sharpen the glossary**

In `CONTEXT.md`, replace the **Arming** entry (lines 491–495) with:

```markdown
- **Arming** — the supervised ceremony that permits real-money submission for
  one sealed instance under one envelope. It is bound to the seal and the
  envelope it named and lapses after the operator-configured number of
  sessions. An instance is in exactly one of four states: `unarmed` (no
  record), `armed`, `lapsed` (its sessions are spent), or `disarmed` (an
  operator revoked it, or its seal or its envelope changed). Operator intent
  (PAUSE / STOP) is orthogonal to it. _Avoid_: enabling live, going
  live, turning on live
- **Arming lapse** — the expiry of an arming after the operator-configured
  number of *calendar NYSE trading sessions*, counted inclusively from the ET
  date it was armed, the arming session included. A weekend and a market
  holiday spend nothing; a half day that traded spends one. It is a "come back
  and look" fence rather than a failure: renewing is the same ceremony run
  again. _Avoid_: expiry, timeout, TTL (the confirmation window is the TTL;
  this is not)
```

- [ ] **Step 4: Cross-link the two predecessor notes**

In `docs/references/alpaca-live-envelope.md`:

Append this bullet to the end of the `## Where it runs` list (after the "Shadow rehearsal" bullet):

```markdown
- Sealing: `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync._refresh_sealed_envelope`
  re-reads the account's arming ledger on every tick and assigns
  `LiveEnvelopeGate.sealed` from the newest arming record's own
  `envelope_values` (ADR 0059 slice 6). Until then `sealed` was permanently
  `None`, so `envelope_agreement` could only answer `unsealed` and
  `LIVE_ENVELOPE_DISAGREEMENT` was unreachable. It is now the live rule: an
  `ALPACA_LIVE_*` edit after an arming refuses every ENTER until a re-arm. See
  [alpaca-live-arming](alpaca-live-arming.md).
```

Replace the `LIVE_ENVELOPE_DISAGREEMENT` row (line 102) with:

```markdown
| `LIVE_ENVELOPE_DISAGREEMENT` | The envelope sealed by the account's newest arming record disagrees with the current `ALPACA_LIVE_*` environment values ([alpaca-live-arming](alpaca-live-arming.md)) | ENTER refusal |
```

Replace the `unsealed` row (line 169) with:

```markdown
| | `unsealed` | An envelope is installed and this account's arming ledger holds no arming record, so nothing has sealed its values yet. |
```

In `docs/references/alpaca-shadow-authority.md`, replace the follow-up at lines 395–397 with:

```markdown
- **Slice 6 arming consumes `ShadowReceiptStore.current`.** Arming without a
  current receipt is `LIVE_SHADOW_INCOMPLETE` — a real reason code since ADR
  0059 slice 6, defined in
  `PythonDataService/app/broker/alpaca/clerk/live_arming.py` and raised by
  `live_arming_ceremony.observe_arming_inputs`. This slice produces the receipt
  that gate reads; the gate itself is
  [alpaca-live-arming](alpaca-live-arming.md).
```

- [ ] **Step 5: Register the row in the authority map**

In `docs/architecture/engine-authority-map.md`, prepend to line 4's list (immediately after `**Last reviewed:** `):

```
2026-09-09 (the arming ceremony, ledger and lapse, ADR 0059 slice 6);
```

and insert this row immediately after the live-risk-envelope row (line 64):

```markdown
| Arming ceremony, ledger and lapse (ADR 0059 D3) | **A supervised two-step ceremony over an append-only, account-rooted sealed ledger** | `PythonDataService/app/broker/alpaca/clerk/live_arming.py` (the sealed `LiveArmingRecord` / `LiveDisarmRecord`, the codes, and the pure `arming_status` / `sessions_used` rules), `app/broker/alpaca/clerk/live_arming_ledger.py` (`accounts/arming/<live_account_id>/live_arming.jsonl` over `sealed_ledger.py`), `app/broker/alpaca/clerk/live_arming_ceremony.py` (`observe_arming_inputs`, `plan_arming`, `apply_arming`, `disarm`, `account_arming_statuses`), `app/broker/alpaca/clerk/ceremony.py` (the plan token and confirmation window shared with `sqlite/cutover.py`); the one writer is `PythonDataService/scripts/manage_alpaca_arming.py`; consumers are `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync._refresh_sealed_envelope` and `app/services/alpaca_live_verdict.py::observe_arming`. | `plan` is read-only and `apply` re-observes every input and refuses any drift; there is no force mode and no HTTP route. Arming binds to the instance's whole sealed-program hash, so a size change disarms it, and to the envelope's sha, so an `ALPACA_LIVE_*` edit is `LIVE_ENVELOPE_DISAGREEMENT` at every ENTER until a re-arm. Lapse is counted only over the canonical NYSE calendar (`app/lean_sidecar/trading_calendar.py::trading_session_count`), inclusively from the ET arming date. **An arming record admits nothing in this slice**: no path submits a real-money order until slice 7 reads it at ENTER admission. | **canonical for ADR 0059 slice 6** — see [alpaca-live-arming](../references/alpaca-live-arming.md); validated by `tests/broker/alpaca/clerk/test_live_arming.py`, `test_live_arming_ledger.py`, `test_live_arming_ceremony.py`, `test_ceremony.py`, `test_shadow_envelope_runtime.py`, `tests/scripts/test_manage_alpaca_arming.py`, `tests/services/test_alpaca_live_verdict.py`, and `tests/contracts/test_alpaca_active_authority_wiring.py`. |
```

- [ ] **Step 6: Check the documentation contract still holds**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts/test_documentation_contract.py -q -p no:cacheprovider
```
Expected: pass. (`docs/doc-authority.md` registers document *classes*, not individual reference notes — neither `alpaca-live-envelope.md` nor `alpaca-shadow-authority.md` is listed there, so `alpaca-live-arming.md` needs no entry either. Confirm with `grep -n "alpaca-live-envelope" docs/doc-authority.md`, which returns nothing.)

- [ ] **Step 7: Commit**

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add docs/references/alpaca-live-arming.md docs/references/alpaca-live-envelope.md docs/references/alpaca-shadow-authority.md docs/architecture/engine-authority-map.md CONTEXT.md
git commit -m "docs(arming): the arming reference note, the lapse vocabulary and the registry row

Includes the worked lapse example across a weekend and Thanksgiving 2026,
both counted against the canonical NYSE calendar.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Gates on the final tree

**Files:**
- Modify: only whatever a gate says is wrong. This task writes no new feature code.
- Test: every suite named below.

**Interfaces:**
- Consumes: the whole of Tasks 1–8.
- Produces: nothing. **Do not push in this task** — the controller pushes.

- [ ] **Step 1: Lint at project scope, both stacks**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && .venv/bin/ruff check app/ tests/ scripts/
```
Expected: `All checks passed!`. This is the same scope CI uses; the pre-commit hook only sees staged paths, so cross-file drift (an unused import left in `cutover.py` after Task 1, a stale `secrets` import, sort order broken by a new first-party module) only shows up here.

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/Frontend && npx eslint src/ --max-warnings 0
```
Expected: no output, exit 0.

- [ ] **Step 2: Run every Python surface this branch touched**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca tests/services tests/scripts tests/contracts -q -p no:cacheprovider
```
Expected: all pass, no errors, no new skips. These four trees are chosen by consumer, not by file: `tests/broker/alpaca` covers the four new clerk modules, the sync and both compositions; `tests/services` covers the verdict and everything that reads a binding; `tests/scripts` covers both operator CLIs; `tests/contracts` covers the wiring pins and the documentation contract.

If something fails that your diff does not obviously touch, baseline it before treating it as inherited: stash nothing (this is a shared checkout — use `git worktree`/`git -c` reads instead), run the same command against `origin/master` in a separate checkout, and record any genuinely pre-existing failure in the PR description rather than silencing it.

- [ ] **Step 3: Prove the OpenAPI contract did not move**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python scripts/export_openapi_contract.py --check
```
Expected: the checked-in snapshot is reported **unchanged**, exit 0. This slice declares no new field: `armed_instance_count`, `envelope_state="sealed"` and `final_verdict="live-armed"` were all already in `app/schemas/alpaca_live_verdict.py`. A reported difference means a schema moved — find it and revert it; do **not** regenerate the snapshot.

- [ ] **Step 4: Prove the composition root still imports**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -c "import app.main"
```
Expected: no traceback. This is the import-cycle gate: `live_arming` imports `live_envelope`, which imports out of `clerk.sqlite`, and `clerk.sqlite.repository` imports back into `live_envelope`. A module-level `LiveArmingLedger` import in `active_runtime.py` breaks exactly here with `ImportError: cannot import name 'EnvelopeReservation'` — it belongs under `TYPE_CHECKING` (Task 6, Step 4).

- [ ] **Step 5: Run the banner spec by its exact path**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6/Frontend && npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts'
```
Expected: 8 passing cases, including `renders a live-armed account in the loudest treatment with the armed count`. Never a directory glob: the frontend container runs out of memory on a broad include.

- [ ] **Step 6: Check every touched file's size**

Run:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6 && wc -l \
  PythonDataService/app/broker/alpaca/clerk/ceremony.py \
  PythonDataService/app/broker/alpaca/clerk/live_arming.py \
  PythonDataService/app/broker/alpaca/clerk/live_arming_ledger.py \
  PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py \
  PythonDataService/app/broker/alpaca/clerk/live_envelope.py \
  PythonDataService/app/broker/alpaca/clerk/shadow_authority.py \
  PythonDataService/app/broker/alpaca/clerk/synthetic_activation.py \
  PythonDataService/app/broker/alpaca/clerk/active_runtime.py \
  PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py \
  PythonDataService/app/broker/alpaca/clerk/sqlite/cutover.py \
  PythonDataService/app/services/alpaca_live_verdict.py \
  PythonDataService/app/routers/brokers.py \
  PythonDataService/scripts/manage_alpaca_arming.py | sort -rn | head -20
```
Expected, and each one is a hard gate:
- **No file at 1,000 lines or more.** `app/routers/brokers.py` is the one to watch — check its number against `git show origin/master:PythonDataService/app/routers/brokers.py | wc -l`; this slice adds about six lines to it.
- **`cutover.py` is below 954.** Task 1 took it to 953. If it is 954 or higher, Task 1's Edit C or Edit E was skipped or reverted; go back and finish it. `cutover.py` may never grow.
- **Every new module is well under 1,000 lines and none has grown past the size this plan's code implies:** `ceremony.py` ~90, `live_arming.py` ~370, `live_arming_ledger.py` ~135, `live_arming_ceremony.py` ~440, `manage_alpaca_arming.py` ~320. A module materially larger than its figure means code landed in the wrong one — report it; do not split modules in this task.

Confirm the whole-repo picture with:
```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6 && git diff --stat origin/master -- PythonDataService/ Frontend/ docs/ CONTEXT.md
```
Expected: only the files this plan names, no `.env`, no snapshot regeneration, no SQLite schema file.

- [ ] **Step 7: Fix anything a gate found, then stop**

If a gate failed, fix it, re-run that gate and Step 2, then commit the fix with an explicit-path `git add` and:

```bash
cd /Users/inkant/learn-ai/.claude/worktrees/slice-6
git add <the exact paths you changed>
git commit -m "fix(arming): <what the gate found, in one line>

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

Do **not** push and do **not** open a PR: the controller does both, after running the `thermo-nuclear-code-quality-review` skill on the finished branch (a CLAUDE.md hard rule for the first push that opens a PR).

---

## Self-review

Run against the design document with fresh eyes after the plan was written. What was checked, and what it found:

**1. Spec coverage.** Every ruling maps to at least one task:

| Ruling | Task(s) |
|---|---|
| R1 (what is sealed) | Task 2 — `LiveArmingRecord`, `_validate_armed`'s two digest checks, the tamper parametrization |
| R2 (the seal hash is the whole sealed program) | Task 4 — `instance_seal_hashes` reads `bot_configuration_hash`; `test_a_size_change_alone_disarms_because_arming_binds_the_whole_seal` |
| R3 (the ledger) | Task 3 — path, lock, symlink, regular-file, foreign row, file order |
| R4 (disarm) | Task 2 (`LiveDisarmRecord`), Task 4 (`disarm`), Task 5 (`disarm` subcommand) |
| R5 (status derivation, in order) | Task 2 — `arming_status` and `test_the_checks_run_in_the_order_r5_fixes` |
| R6 (lapse counting) | Task 2 — `sessions_used`, the weekend case, the Thanksgiving case, the reversed-clock case |
| R7 (re-arming allowed) | Task 4 — `test_re_arming_while_armed_supersedes_with_no_already_armed_refusal` |
| R8 (the four inputs and their refusals) | Task 4 — `observe_arming_inputs` plus one test per refusal code |
| R9 (plan and apply; the shared ceremony module) | Task 1 (extraction, cutover shrinks, its tests untouched) and Task 4 (`plan_arming`, `apply_arming`, drift) |
| R10 (runtime sealing) | Task 6 — `_refresh_sealed_envelope`, the wiring pin, the four end-to-end tests |
| R11 (verdict truth) | Task 7 — `observe_arming`, the live branch, the banner case |
| R12 (codes beside the model) | Task 2 — `ARMING_REASON_CODES` closed-set test |
| R13 (CLI) | Task 5 — all four subcommands, both exit codes, `submission_admitted` |
| R14 (docs) | Task 8 — the note, both glossary entries, both cross-links, the authority-map row |

The design's **Testing** paragraph asks for eleven things; each is present: sha round trip and per-field tamper (T2), every status branch in order (T2), lapse across a weekend and a holiday with a pinned `now_ms` (T2), ledger append/read/lock/symlink/foreign-row/`sealed_envelope` (T3), ceremony happy path, every refusal code, TTL expiry, bad token, every input's drift, re-arm and disarm (T4), the composed-runtime sync test including the corrupt-ledger error log (T6), the verdict tests and the banner case (T7), and the CLI tests mirroring `test_manage_alpaca_shadow.py` (T5). The design's **Error handling** paragraph — `LiveArmingRefused` per refusal, `LiveArmingInvalid` for corruption, the sync's error-level log once per transition, the verdict counting zero and naming the fault — is covered by T2, T6 and T7. The **Data flow**'s five steps are exercised by T5 (steps 1, 2, 5) and T6 (steps 3, 4). All four **Residuals** are written down in Task 8's reference note.

**2. Placeholder scan.** Searched the plan for `TBD`, `TODO`, `...`, "similar to Task", "add validation", "handle edge cases", "write tests for the above", and for any step that says what to do without showing how. Findings and fixes: the bare `...` bodies that the slice-5 plan used in two test sketches are **not** present here — every test body in this plan is complete and runnable. Every implementation step carries the full code or the exact before/after text. The only prose-only steps are Task 1 Step 1 (record two numbers), Task 5 Step 1 (read a neighbouring file), Task 8 Step 1 (read a neighbouring file) and the Task 9 gates, all of which are commands with stated expected output.

**3. Type consistency.** Names and signatures were cross-checked across task boundaries:
- `LiveArmingRecord.create(..., envelope=)` takes a `LiveEnvelopeValues` (not a mapping) in Tasks 2, 3, 6 and 7; the record's stored field is `envelope_values: dict[str, float | int]` and its rebuilt property is `.envelope`. Consistent everywhere.
- `LiveArmingLedger(artifacts_root, live_account_id=...)` — positional root, keyword account — is used identically in Tasks 3, 4, 5, 6, 7 and in every test.
- `LiveArmingRefused(reason_code, message)` with attribute `.reason_code` is constructed in Tasks 2, 4 and 5 and asserted on in all three.
- `arming_status(records, *, live_account_id, strategy_instance_id, seal_hash, configured_envelope, now_ms)` (Task 2) is called only through `account_arming_statuses` (Task 4), whose signature Tasks 5 and 7 use verbatim.
- The `refused` seam is `Callable[[str], Exception]` in Task 1 and is satisfied by both a bare class (`CutoverRefused`, Task 1) and `_refused(code)` (Task 4).
- `ArmingObservation(armed_instance_count, envelope_state, detail)` (Task 7) is constructed by `observe_arming` and by the verdict's default `ArmingObservation.none()`; the field names match the verdict's use.
- `sessions_used` is both a module function (Task 2) and an `ArmingStatus` field name. They never collide in one scope: the dataclass field is only read as `status.sessions_used`, and the function is only called inside `arming_status` and the tests, which import it by name. Flagged and deliberately kept, because both names are the right one for their reader.
- `LiveEnvelopeGate.sealed` and `.values` are assigned in Task 6; both are plain instance attributes in the shipped slice-5 module (verified in `live_envelope.py:158-162`), so the assignments are legal.
- `SHADOW_ACCOUNT_PREFIX`, `shadow_account_id_for_live_account`, `require_real_account_id`, `safe_path_component`, `resolve_contained_path`, `advisory_file_lock`, `append_canonical_jsonl_line`, `read_canonical_jsonl_objects`, `canonical_sha256`, `canonical_json_bytes`, `et_date_at_ms`, `trading_session_count`, `MAX_TIMESTAMP_MS`, `Clock`, `now_ms_utc`, `live_state_binding_repository`, `live_artifacts_root`, `atomic_write_json`, `SQLITE_FACADE_AUTHORITIES` were each read in the source before being used here; their module paths and signatures in this plan are the ones on disk at `master`.

## Plan notes for the controller

- **`--live-state-root` is a fourth CLI flag R13 does not name.** The runner's `live_state` root is where sealed bindings live and is a different tree from the Clerk artifacts root, so `status`, `plan` and `apply` cannot find a seal without it. It defaults to `live_artifacts_root()`. (Added at the pre-flight scan; accepted by the controller.)
- **Controller ruling after the scan (Task 2):** a record dated after the clock (`now_ms < armed_at_ms`) is `disarmed` with a thirteenth code `LIVE_ARMING_FUTURE_DATED`, checked before the lapse arithmetic; `sessions_used` raises on a reversed range instead of returning 0. Carried in `task-2-controller-notes.md`; every count of "twelve codes" in this plan is thirteen in the implementation.

Ambiguities resolved while writing the plan, and the two places I think the design is wrong. Nothing here was silently deviated from — each is a decision the controller can reverse cheaply.

- **`LIVE_ENVELOPE_MISSING` does not exist in `live_envelope.py` today.** R12 says it is "imported from `live_envelope.py`", but that module defines only `LIVE_ENVELOPE_CASH_EXCEEDED`, `LIVE_ENVELOPE_DISAGREEMENT` and `LIVE_ENVELOPE_UNOBSERVED`; the string is a bare literal in `shadow_authority.py:75`. **Resolved:** Task 2 adds the constant to `live_envelope.py` (its one canonical home) and switches `shadow_authority.py` to the import. The value is unchanged, so `test_shadow_envelope_runtime.py`'s existing assertion still holds. This is the design's intent, not a deviation, but it is a source edit R12 did not name.
- **The plan token's digest is `canonical_json_bytes`, not `canonical_sha256`.** R9 says the token is `canonical_sha256(payload)`. Those are two different functions in this repo: `sealed_ledger.canonical_sha256` hashes canonical JSON, while `operational_files.canonical_json_bytes` (which `cutover.py` has always hashed) appends a trailing newline first. **Resolved:** the shared `plan_content_token` uses cutover's exact bytes, because the whole point of Task 1 is that cutover's digest and its 39 tests do not change. The arming plan uses the same helper. If the controller wants R9 read literally, arming's token would differ from cutover's by one byte of input and Task 1 would have to keep a second digest — worse, for nothing.
- **`kind` is a sealed field of both records.** R3 describes the rows as `{"kind": "armed", ...}` but R1's field list omits `kind`. **Resolved:** `kind` is a real dataclass field inside `record_sha256`, so a row cannot be relabelled without breaking its digest. This is a superset of R1, in the direction R1 already leans ("a stricter seal than the ADR's minimum").
- **Two codes the design does not name.** `LIVE_ARMING_TTL_INVALID` (a confirmation window outside 1..300 000 ms — R9 fixes the bound but names no code for violating it) and `LIVE_ARMING_NOT_ARMED` (`disarm` with nothing to revoke — R4 says disarm is a single append but does not say what happens when there is nothing to revoke; refusing is the conservative reading, since writing a revocation that revokes nothing would put a row in the ledger claiming a permission never existed). Both are in `ARMING_REASON_CODES` and both are prefixed `LIVE_ARMING_` per R12.
- **A binding sealed on the *shadow* custody id counts.** R8(c) says the binding must have `sealed_account_id == live_account_id`. Under the Shadow Account Authority, which is the only authority that exists on a live account in this slice, a registered instance's `sealed_account_id` is `shadow:<live_account_id>` (see `tests/broker/alpaca/clerk/test_shadow_envelope_runtime.py:105`). Read literally, R8(c) would make **every** instance unarmable today. **Resolved:** `custody_account_ids_for` admits both ids, and the reference note says why. Slice 7's `real_live` custody will seal the live id itself and the same predicate keeps working.
- **`seal_hash=None` (no sealed binding any more) is `LIVE_ARMING_SEAL_CHANGED`.** R5 says "an arming record whose `seal_hash` differs from the current one"; it does not say what "the current one" is when the binding has been deleted. Refusing as *changed* is the fail-closed reading.
- **A `now_ms` behind `armed_at_ms` spends zero sessions.** `trading_session_count` raises `ValueError("start_date must be before or equal to end_date.")` on a reversed range (verified against the calendar module), so `sessions_used` guards it. R6 does not cover a backwards clock. Zero is the conservative *count*, but note it means a rolled-back clock **extends** an arming rather than lapsing it; the alternative (treating it as lapsed) would let a clock blip disarm a live account. The controller may prefer the other trade-off.
- **The account is observed from the shadow activation fence, and exactly one is admitted.** R8(b) says the live account id comes from "the shadow activation proof under the artifacts root" but does not say what to do with two. Refusing (`LIVE_ARMING_INSTANCE_UNSEALED`, message "more than one shadowed live account") is the only safe reading; it is disclosed as a residual in the reference note. This required one new public method, `IsolatedActivationStore.account_ids()`, on a module the design does not mention.
- **`disarm` reads no settings, binding or receipt.** R4 says disarm has no plan/apply; it does not say which inputs it observes. Requiring the full observation would make an instance whose receipt or binding had gone impossible to revoke — the opposite of what a closed direction is for. `disarm` therefore needs only the activation fence and the ledger.
- **Every CLI object carries `submission_admitted`, including error objects.** R13 requires it on `plan`, `apply` and `status`. Putting it in `_write` puts it on `disarm` and on refusals too. That is a superset and, for a refusal, still true.
- **`envelope_state` is `sealed` while the account's only instance is disarmed.** R11 defines it as "the ledger holds at least one arming record", which a revocation does not remove. `LiveArmingLedger.latest_arming()` therefore ignores disarms, and so does the envelope the sync installs — a revocation withdraws one instance's permission, it does not un-seal an account's numbers. Stated in the reference note; worth the controller's eye because "disarmed but sealed" reads oddly the first time.
- **`final_verdict` also requires an installed Clerk.** R11 says "configured live, mode agreed, Clerk installed and `armed_instance_count ≥ 1`". Implemented as `authority in SQLITE_FACADE_AUTHORITIES` (`{"sqlite", "shadow"}`), so an armed record under a refused authority reports `live-unarmed` with a non-zero count. That combination is deliberate and tested.
- **No `docs/math-sources-of-truth.md` row was added.** R14 lists five doc targets and this is not one of them. `sessions_used` introduces no numeric formula of its own — it delegates entirely to the sealed canonical calendar — and `live_arming.py` carries the four-field provenance block naming that canonical implementation. If the controller wants the registry to list it anyway, it is one row and belongs beside the shadow-gate rows.
- **What I think may be wrong in the design, beyond the items above:** R10 says the sync logs `live_envelope_sealed` / `live_envelope_unsealed` "with the agreement", and the design's error-handling paragraph says an unreadable ledger is logged "once per transition" at error level. Those two dedup states are independent (the seal can change while the ledger stays readable, and vice versa), so the implementation keeps two flags rather than one. If the controller expected a single "last verdict" like `_acted`'s, that is a different, coarser behaviour and would drop the error line whenever a seal transition happened in the same tick.
- **Not in scope, and deliberately left alone:** `app/broker/v2panel/vocabulary.py` and the Frontend `broker-v2-emergency-copy.ts` / `broker-v2-vocabulary.snapshot.json` pair. No `LIVE_ARMING_*` code reaches the panel in this slice — the codes appear only in backend-authored verdict prose and in the CLI's JSON — so the closed copy map needs no entry and its contract test is untouched. Slice 7, which surfaces a per-instance refusal on the panel, is where that changes.

# Run-Scoped Replay Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every completed Paper (and Dry Run) bot run retains its exact source bars and, on Stop or on demand, produces a durable per-run parity receipt that replays those bars through `BacktestEngine` + the shared runner seam, reconciles the result against the run's own durable decision receipts, and classifies every divergence as expected-live-effect vs real drift.

**Architecture:** Three legs, all built on existing mechanisms. (1) **Retention** — `run_trade_bot` gains the same `_RetainedSourceBarFeed`/`SourceBarLedger` wiring `run_dry_run_bot` already has, backed by a new instance-scoped `paper:<strategy_instance_id>` evidence account (mirroring Dry Run's `sim:<sid>` scoping, which is what makes FR-016 retained-replay warmup safe). (2) **Proof** — a new `app/services/run_replay_proof.py` service computes two comparisons per run: an *engine-parity leg* (the existing, currently test-only `run_shadow_trace_evaluation` over the run's full retained stream — BacktestEngine vs runner seam, all-COMMIT) and a *run-fidelity leg* (a disposition-faithful replay through the production `strategy_evaluations` generator, aligned bucket-by-bucket against the run's decision receipts by deterministic `evaluation_id`, settling each stage with the live-recorded disposition so a legitimately-refused ENTER never cascades into false drift downstream). (3) **Receipt** — a Pydantic v2 `RunReplayReceipt` persisted at `live_state/<sid>/run_replay_receipts/<run_id>.json` (the `run_build_evidence/` per-run pattern), written `pending` synchronously inside Stop and completed by a background task; a new transport-only FastAPI router exposes GET (read) and POST (regenerate) with OpenAPI contract regeneration.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLite (existing `SourceBarLedger` / Clerk decision receipts), asyncio (`asyncio.to_thread` + `asyncio.run` inside the worker thread for the CPU-bound replay), pytest + pytest-asyncio + httpx `ASGITransport`.

**Spec:** docs/audits/strategy-execution-research-directions-2026-08-24.md (Direction 2)

## Global Constraints

- **Temporal rigor:** every temporal value in flight, at rest, or on the wire is `int64 ms UTC`. No ISO strings, no `datetime`/`DateTime` on wire or storage. (`.claude/rules/temporal-rigor.md`)
- **Typed exceptions, no silent catches:** no bare `except:`, no `except Exception: pass`. Domain errors get typed classes; unexpected errors propagate with context.
- **Structured logging:** `logger = logging.getLogger(__name__)` per module, `extra={...}` payloads with an `"action"` key, never string interpolation into messages, never `print()`.
- **Every new behavior ships a test in the same task** — no test-later tasks exist in this plan.
- **pytest conventions per `.claude/rules/testing.md`:** `httpx.AsyncClient` + `ASGITransport(app=app)` for endpoint tests (never `TestClient`), explicit `atol`/`rtol` on any float comparison, test names `test_<function>_<scenario>`, function-scoped fixtures.
- **Local pytest invocation:** prefix `DATA_PLANE_CONTROL_SECRET=""` (else ~33 router tests 403). All test commands below assume `cwd = PythonDataService/`.
- **Pydantic v2 only:** `model_validator` / `field_validator`, `ConfigDict` — never `@validator` or inner `Config` classes.
- **snake_case response fields** on every Pydantic response model (the .NET consumer expects snake_case).
- **New FastAPI endpoints regenerate the committed OpenAPI contract:** `python scripts/export_openapi_contract.py` (CI runs `--check` and fails otherwise). This is an explicit step in the endpoint task.
- **The Paper evidence-only override is permanent.** This plan adds evidence *generation* on the way out of a run. It must never add admission friction: no new Start/Resume/Deploy gate, no refusal keyed on replay receipts.
- **Routers are transport-only** (`.claude/rules/python.md` router-freeze discipline): the new router validates/parses, calls the registry facade, translates typed errors. All business logic lives in `app/services/`.
- **Sealed-program bytes are untouchable:** `app/engine/strategy/signal_program.py`, `app/engine/engine.py`, and every module listed in registered programs' `artifact_paths` must NOT be edited — an edit invalidates every qualification receipt. This plan only *calls* them.
- **No silent evidence repair:** never `drop_duplicates`, forward-fill, or reorder retained bars; ledger conflicts propagate as their typed errors.
- **Don't duplicate canonical helpers:** the settlement policy is `bot_trade_strategy_warmup._COMMIT_WORTHY_OUTCOMES` (a module-level frozenset) — import it, never re-declare the set. Same for `_includes_session_phase` (import from `app.services.bot_trade_strategy`).
- **Engine/math authority registries (AGENTS.md "Engine and math authority"):** any change that introduces, retires, or moves a math/engine path updates BOTH `docs/math-sources-of-truth.md` and `docs/architecture/engine-authority-map.md` in the same PR. This plan introduces an engine path (the replay receipt path) — Task 12 Step 2b carries the map row and the explicit no-new-math-concept statement.
- **Before push:** project-scope `ruff check PythonDataService/app/ PythonDataService/tests/`, the **full** pytest suite (no `-k "not slow"` — the full suite is the one pre-push gate per `.claude/rules/testing.md`), `python scripts/export_openapi_contract.py --check`, and the `thermo-nuclear-code-quality-review` skill (one-shot, before the PR-opening push).
- **Line citations in this plan were verified on origin/master 2fd0df84 (2026-08-24). Re-verify each before editing — lines drift.**

---

## Design decisions locked (from code reading — do not relitigate mid-task)

**(a) Per-run bar retention: instance-scoped ledger, not account-shared, not run-scoped.**
`SourceBarLedger` (`app/services/source_bar_ledger.py`) stores under `accounts/alpaca/<account_id>/source_bars.sqlite3` and refuses foreign-account rows. Dry Run is already effectively *instance*-scoped because its account id is `sim:<strategy_instance_id>` (`synthetic_account_id_for_strategy`, `app/broker/alpaca/clerk/account_authority.py:52`). Paper mirrors this with `paper:<strategy_instance_id>` (`:` is legal in `_SAFE_COMPONENT`, `app/broker/alpaca/paths.py:20`). A ledger shared across bots on the real account would break `_RetainedSourceBarFeed.recent_closed_bars`' retained-branch warmup (a second bot on the same symbol would warm from another bot's live window instead of provider history) and trip `SOURCE_BAR_HISTORY_AFTER_LIVE`. A *run*-scoped file would break FR-016 retained-replay across Resume. Retention economics: capacity is the existing `SOURCE_BAR_STREAM_CAPACITY = 200_000` bars/stream, fail-closed with `SourceBarRetentionLimitError` (#1740 reviewed-rollover policy applies unchanged; the retention-floor test `tests/services/test_signal_program_retention_floor.py` already pins capacity > warmup + one open cycle).

**(b) When the replay runs: background task triggered by Stop, receipt `pending` → final.**
`_stop_locked` (`app/services/bot_runner.py:836-945`) already does network-bound terminal custody proof after a 5 s cancel timeout (`_STOP_TIMEOUT_S`, `:175`), but the replay is CPU-bound over up to 200k bars — synchronous execution inside Stop would wreck the fleet stop budget (17 bots in 8.6 s today). So: `_stop_locked` writes a `status="pending"` receipt synchronously (durable intent survives a crash), then schedules `asyncio.create_task` held in a registry-owned set; the task runs the replay via `asyncio.to_thread` and atomically replaces the receipt with the final verdict (or `replay_failed` + error). POST regenerates on demand for any non-live run.

**(c) Run slicing without per-run bar copies — but run-BOUNDED (PR #1751 finding 4).** The replay input for run N is the retained stream for `(provider, symbol)` **bounded at both ends**, so regenerating run N after run N+1 has appended more bars yields byte-identical input, digest, and verdict:
- **End bound (primary, ledger-sequence):** `ledger_end_seq` — the stream's max `seq` snapshotted into the `pending` receipt at Stop time (`write_pending`), carried into the final receipt, and reused by every regeneration. **Fallback (wall-clock, for crashed/legacy runs with no snapshot):** `BotRunOutcomeRecord.recorded_at_ms` (`bot_binding_repository.py:227-241`, read via `read_outcome` :420-441) — bars with `end_ms <=` it. No snapshot AND no terminal outcome → refuse (`RunReplayUnavailableError`): an unbounded replay is not evidence. `BotRunRecord` is create-once (frozen), so the end bound lives in the receipt, not the run record.
- **Start/warmup split:** primary rule is decision-anchored — when the run has live decision records, warmup = bars with `end_ms <= first_record.bar_close_ms - decision_timeframe_ms` (the first recorded bucket's open; `decision_bar_close_ms` is captured into receipt facts by Task 5b, timeframe from `binding.sealed_program.configured_signal.clock.decision_timeframe_ms` — re-verify that attribute path against the seal model; `clock.warmup_lookback_days` on the same path is proven by `bot_trade_strategy_warmup.py:77-79`). Wall-clock fallback when no records/seal: `end_ms <= BotRunRecord.started_at_ms` (`bot_binding_repository.py:167-178`). The decision-anchored rule eliminates the startup race where a bar closing between launch and the warmup fetch would be misclassified live-vs-warmup.

**(d) Why the fidelity leg replays dispositions instead of diffing against the all-COMMIT reference.** `test_signal_program_mode_parity.py:56-67` documents the exact residual seam: Paper's liveness gate on ENTER and real Clerk rejections live outside the `SignalProgram` surface. Both are durably receipted as `outcome="blocked"` decision receipts (liveness/pause: `bot_trade_strategy._append_decision_receipt`; Clerk rejections: `_append_pre_custody_refusal`, `app/broker/alpaca/clerk/sqlite/runtime.py:1073-1085`, invoked from every `rejected(...)` branch of `execute_for_instance`). Because the reference (BacktestEngine) commits everything, a single legitimately-blocked ENTER would make every later bucket diverge (position state forks). The fidelity replay therefore settles each replayed stage with the *live-recorded* disposition (`_COMMIT_WORTHY_OUTCOMES` mapping — the exact FR-016 warmup policy from `bot_trade_strategy_warmup.py:56-58`), so state tracks the live path and every bucket is comparable full-length; each COMMIT/DISCARD fork point is itself classified (`expected_live_effect` when an enumerating `blocked`/crash receipt exists, `drift` otherwise). `evaluation_id` is a deterministic semantic hash of `(program_key, program_version, settings, bar_close_ms)` (`app/engine/strategy/signal_program.py:251-258`) — independent of decision content — so replay↔receipt alignment by `evaluation_id` is exact.

**(d2) Content-level comparison, not intent-kind-only (PR #1751 finding 3).** `evaluation_id` hashes identity, **not decision content**, and intent-kind matching alone would let numerical trace drift that preserves direction pass undetected. So live capture is extended (Task 5b): every decision receipt's facts gain `trace_digest = trace_root([evaluation.trace])` (per-bucket digest via the *existing canonical* `trace_root`, `signal_program.py:359-361` — no new hashing math) and `decision_bar_close_ms`. The fidelity replay then compares digest-by-digest; a `blocked` row is **never trusted on presence alone** — it classifies `expected_live_effect` only when (i) the replay staged an intent at that bucket, (ii) the row's digest (when present) matches the replayed trace digest, and (iii) its `reason_code` is in the closed live-only-gate set `EXPECTED_LIVE_GATE_REASON_CODES` (enumerated from the owning modules: `PAUSED_OBSERVE_ONLY` from `bot_trade_strategy.py`; the seven liveness fact codes from `app/services/market_liveness.py:60-205`; `STREAM_HEALTH_HOLD`/`MARKET_LIVENESS_BLOCKED`/`SIMULATED_SOURCE_BAR_UNPROVEN`/`EXIT_CUSTODY_UNPROVEN` from `clerk/sqlite/runtime.py:124,595,668,729,752`). Residual blind spot, stated in the schema: rows recorded **before** this feature carry no digest — those buckets fall back to intent-kind comparison and are excluded from `digest_verified_count`, so the receipt itself discloses its digest coverage.

**(e) Receipt fields** (design question c): run id, instance id, strategy key, symbol, provider, bar-set digest + count, `ledger_end_seq` (run-end bound snapshot), engine-parity trace root / compared count / first divergence, fidelity counts (compared/match/expected/drift/`digest_verified_count`) + bounded divergence list, records-truncated honesty flag, program version + sealed program hash, `generated_at_ms`, status (`pending | parity | parity_with_expected_live_effects | indeterminate | drift | replay_failed` — truncated or unprovable evidence yields `indeterminate`, never a proof verdict; PR #1751 finding 6), error.

---

### Task 1: Paper evidence account identity and the Real-Paper authority ledger

**Files:**
- Modify: `app/broker/alpaca/clerk/account_authority.py` (near `synthetic_account_id_for_strategy`, line ~52)
- Modify: `app/services/bot_binding_authority.py` (`RealPaperBindingAuthority` at :65-80, `BindingAuthoritySelector.for_binding` at :206-219)
- Test: `tests/services/test_bot_binding_authority_source_bars.py` (create)

**Interfaces:**
- Consumes: `SourceBarLedger(artifacts_root: Path, account_id: str)` (`app/services/source_bar_ledger.py:158`), `validate_strategy_instance_id` (`app/engine/live/identity.py:26`).
- Produces: `PAPER_EVIDENCE_ACCOUNT_PREFIX: str = "paper:"` and `paper_evidence_account_id_for_strategy(strategy_instance_id: str) -> str` in `account_authority.py`; `RealPaperBindingAuthority.source_bars() -> SourceBarLedger` (overriding the base-class `None` default at `bot_binding_authority.py:48`); `RealPaperBindingAuthority` gains an `artifacts_root: Path` field. Later tasks rely on: the paper ledger for instance `sid` lives at `<artifacts_root>/accounts/alpaca/paper:<sid>/source_bars.sqlite3`.

- [ ] **Step 1: Write the failing test**

```python
"""Instance-scoped source-bar evidence for the Real Paper authority (Direction 2)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from app.broker.alpaca.clerk.account_authority import (
    PAPER_EVIDENCE_ACCOUNT_PREFIX,
    paper_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.services.bot_binding_authority import RealPaperBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_lifecycle_projection import AlpacaLifecycleProjector


def _trade_binding(sid: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=sid,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=0,
    )


def test_paper_evidence_account_id_for_strategy_is_instance_scoped() -> None:
    account_id = paper_evidence_account_id_for_strategy("bot-a")

    assert account_id == f"{PAPER_EVIDENCE_ACCOUNT_PREFIX}bot-a"
    assert account_id != synthetic_account_id_for_strategy("bot-a")


def test_real_paper_authority_source_bars_opens_instance_scoped_ledger(tmp_path: Path) -> None:
    authority = RealPaperBindingAuthority(
        binding=_trade_binding("bot-a"),
        projector=cast(AlpacaLifecycleProjector, object()),
        external_start_guard=None,
        artifacts_root=tmp_path,
    )

    ledger = authority.source_bars()
    try:
        assert ledger is not None
        assert ledger.account_id == paper_evidence_account_id_for_strategy("bot-a")
        assert ledger.path == (
            tmp_path / "accounts" / "alpaca" / "paper:bot-a" / "source_bars.sqlite3"
        )
    finally:
        ledger.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_bot_binding_authority_source_bars.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'PAPER_EVIDENCE_ACCOUNT_PREFIX'`.

- [ ] **Step 3: Implement the identity helper and the authority override**

In `app/broker/alpaca/clerk/account_authority.py`, directly below `synthetic_account_id_for_strategy` (re-verify line ~52-57 and mirror its local-import style):

```python
PAPER_EVIDENCE_ACCOUNT_PREFIX = "paper:"
"""Instance-scoped evidence namespace for real-paper retained source bars.

Not a Clerk custody account: custody stays on the real Alpaca account. This
namespace only scopes the ``SourceBarLedger`` file so each paper instance's
retained-replay warmup (FR-016) sees exactly its own observations, mirroring
Dry Run's ``sim:`` scoping.
"""


def paper_evidence_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the isolated real-paper source-bar namespace for one instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{PAPER_EVIDENCE_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"
```

Export both names in the module's `__all__` if one exists (re-verify).

In `app/services/bot_binding_authority.py`:

1. Add imports: `paper_evidence_account_id_for_strategy` from `app.broker.alpaca.clerk.account_authority` (extend the existing import at :16).
2. `RealPaperBindingAuthority` (frozen dataclass at :65-80): add field `artifacts_root: Path` **before** the defaulted `account_id` field, and add:

```python
    def source_bars(self) -> SourceBarLedger:
        return SourceBarLedger(
            artifacts_root=self.artifacts_root,
            account_id=paper_evidence_account_id_for_strategy(
                self.binding.strategy_instance_id
            ),
        )
```

3. `BindingAuthoritySelector.for_binding` (:206-219): pass `artifacts_root=self.artifacts_root` when constructing `RealPaperBindingAuthority`.

- [ ] **Step 4: Run test to verify it passes**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_bot_binding_authority_source_bars.py -x -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the neighboring suites that construct these types**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_bot_runner.py tests/services/test_source_bar_ledger.py -q`
Expected: PASS (the selector is the only production constructor of `RealPaperBindingAuthority`; if any test constructs it positionally, fix that call site to keyword form in this task).

- [ ] **Step 6: Commit**

```bash
git add app/broker/alpaca/clerk/account_authority.py app/services/bot_binding_authority.py tests/services/test_bot_binding_authority_source_bars.py
git commit -m "feat(replay-proof): instance-scoped paper source-bar evidence account"
```

---

### Task 2: Wire source-bar retention into `run_trade_bot`

**Files:**
- Modify: `app/services/bot_trade_strategy.py` (`run_trade_bot` at :646-800)
- Modify: `app/services/bot_runtime.py` (`execute_bot_run` at :118-138)
- Test: `tests/services/test_run_trade_bot_source_bars.py` (create)

**Interfaces:**
- Consumes: `_RetainedSourceBarFeed` (`bot_trade_strategy.py:230-303`, already used by `run_dry_run_bot` at :878), `SourceBarLedger`, `paper_evidence_account_id_for_strategy` (Task 1). Test fakes: `_binding`, `_PhaseFeed`, `_fresh_live_market_liveness` from `tests/services/test_candidate_uncaptured_at_crash.py`; `_FakeClerk`, `_SID`, `_ema_parity_bars_through_first_exit` from `tests/services/test_bot_runner.py`; `ClerkSqliteRepository.initialize(account_id=..., artifacts_root=...)`.
- Produces: `run_trade_bot(binding, feed, *, source_bars: SourceBarLedger | None = None)` — when a ledger is supplied, every streamed observation is durably retained before the session sees it (capture-first, `use_rth=False`, exactly as Dry Run). `execute_bot_run` fails closed when a trade-mode run has no ledger. Later tasks rely on: a completed paper run's bars are readable via `SourceBarLedger.bars(provider=<feed_id>, symbol=...)`.

- [ ] **Step 1: Write the failing test**

```python
"""Paper trade runs retain their source bars (Direction 2, deliverable 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services.bot_runtime import execute_bot_run
from app.services.bot_trade_strategy import run_trade_bot
from app.services.source_bar_ledger import SourceBarLedger
from tests.services.test_bot_runner import (
    _SID,
    _ema_parity_bars_through_first_exit,
    _FakeClerk,
)
from tests.services.test_candidate_uncaptured_at_crash import (  # noqa: F401 -- autouse fixture
    _binding,
    _fresh_live_market_liveness,
    _PhaseFeed,
)


@pytest.mark.asyncio
async def test_run_trade_bot_retains_every_live_source_bar(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        bars = _ema_parity_bars_through_first_exit()

        await run_trade_bot(_binding(run_id="run-1"), _PhaseFeed(live_bars=bars), source_bars=ledger)

        retained = ledger.bars(provider="fake-phase", symbol="SPY")
        assert [row.end_ms for row in retained] == [bar.end_ms for bar in bars]
        assert [str(row.close) for row in retained] == [str(bar.close) for bar in bars]
    finally:
        ledger.close()
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_execute_bot_run_trade_mode_requires_source_bar_ledger(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="source-bar ledger"):
        await execute_bot_run(
            _binding(run_id="run-1"),
            _PhaseFeed(),
            run_gate=None,
            instance_dir=tmp_path,
            source_bars=None,
        )
```

Before finalizing, open `tests/services/test_candidate_uncaptured_at_crash.py:120-160` and mirror exactly how it constructs/tears down the clerk (`set_alpaca_clerk`) — if its teardown differs from `set_alpaca_clerk(None)`, copy its form.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_trade_bot_source_bars.py -x -q`
Expected: FAIL — `TypeError: run_trade_bot() got an unexpected keyword argument 'source_bars'`.

- [ ] **Step 3: Implement the wiring**

In `app/services/bot_trade_strategy.py`, change `run_trade_bot`'s signature (:646) and wrap the feed right after the clerk guards (after `capability_account_id = market_data_capability_account_id(feed)` — keep the capability lookup on the *raw* feed, mirroring `run_dry_run_bot` at :948):

```python
async def run_trade_bot(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    *,
    source_bars: SourceBarLedger | None = None,
) -> None:
    """Execute one admitted strategy; the Clerk owns all execution truth.

    ``source_bars`` retains every unfiltered feed observation before the
    sealed session's RTH policy applies (Direction 2: a paper run must be
    replayable from its own retained bars). ``None`` disables retention and
    exists only for focused unit tests; production wiring
    (``bot_runtime.execute_bot_run``) always supplies the instance-scoped
    ledger and fails closed without one.
    """
```

and replace the `strategy_evaluations(binding, feed, ...)` call's feed argument:

```python
    run_feed = _RetainedSourceBarFeed(feed, source_bars) if source_bars is not None else feed
    async for evaluation in strategy_evaluations(
        binding,
        run_feed,
        captured_decisions=captured_decision_outcomes(decision_receipts),
    ):
```

Everything else in the function body is untouched (in particular `_decision_bar_ref` already uses `evaluation.bar.feed_id`, which `_RetainedSourceBarFeed` preserves).

In `app/services/bot_runtime.py`, `execute_bot_run` (:128-136):

```python
    if binding.mode == "trade":
        if source_bars is None:
            raise RuntimeError("Paper trade runs require their durable source-bar ledger.")
        await run_trade_bot(binding, run_feed, source_bars=source_bars)
```

No change needed in `bot_runner._supervise` (:1148) — it already passes `self._authority_for(binding).source_bars()`, which Task 1 made non-`None` for trade bindings.

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_trade_bot_source_bars.py tests/services/test_candidate_uncaptured_at_crash.py -q`
Expected: PASS (existing 2-arg `run_trade_bot(...)` call sites keep working — the new kwarg defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add app/services/bot_trade_strategy.py app/services/bot_runtime.py tests/services/test_run_trade_bot_source_bars.py
git commit -m "feat(replay-proof): run_trade_bot retains source bars via the paper evidence ledger"
```

---

### Task 3: Ledger read helpers + run-record reader (replay input assembly)

**Files:**
- Modify: `app/services/source_bar_ledger.py` (add `providers_for`; extend `close` with `checkpoint` kwarg)
- Modify: `app/services/bot_binding_repository.py` (add `read_run_record`, next to `read_program_build_evidence` at :443)
- Create: `app/services/run_replay_proof.py` (module skeleton: error type, digest, split, conversions)
- Test: `tests/services/test_run_replay_proof_assembly.py` (create)

**Interfaces:**
- Consumes: `RetainedSourceBar`, `SourceBarLedger`, `BotRunRecord` (`bot_binding_repository.py:167-178`), `BotBindingRepository._run_path` / `_validate_run_id` (:656-664), `TradeBar` (`app/engine/data/trade_bar.py`), `MarketDataBar` (`app/marketdata/feed.py:37`).
- Produces (all in `app/services/run_replay_proof.py`):
  - `class RunReplayUnavailableError(RuntimeError)` with `detail: str` and `http_status: int` (default 409)
  - `RUN_REPLAY_RECEIPTS_DIRECTORY = "run_replay_receipts"`
  - `bar_set_digest(bars: Sequence[RetainedSourceBar]) -> str` (sha256 hex)
  - `split_warmup_and_live(bars: Sequence[RetainedSourceBar], run_started_at_ms: int) -> tuple[list[RetainedSourceBar], list[RetainedSourceBar]]`
  - `to_trade_bar(bar: RetainedSourceBar) -> TradeBar`, `to_market_bar(bar: RetainedSourceBar) -> MarketDataBar`
  - `replay_provider_for(ledger: SourceBarLedger, symbol: str) -> str`
  - Plus: `SourceBarLedger.providers_for(symbol: str) -> list[str]`, `SourceBarLedger.close(*, checkpoint: bool = True)`, `BotBindingRepository.read_run_record(strategy_instance_id: str, run_id: str) -> BotRunRecord | None`.

- [ ] **Step 1: Write the failing test**

```python
"""Replay input assembly: digests, run-boundary split, conversions (Direction 2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BotBindingRepository, BotRunRecord
from app.services.run_replay_proof import (
    RunReplayUnavailableError,
    bar_set_digest,
    replay_provider_for,
    split_warmup_and_live,
    to_market_bar,
    to_trade_bar,
)
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

_T0 = 1_700_000_000_000


def _market_bar(index: int, *, feed_id: str = "feed-a", close: str = "400.5") -> MarketDataBar:
    start = _T0 + index * 60_000
    return MarketDataBar(
        symbol="SPY",
        start_ms=start,
        end_ms=start + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start + 60_500,
        feed_id=feed_id,
        session_phase="RTH",
    )


def _retained(index: int, *, close: str = "400.5") -> RetainedSourceBar:
    return RetainedSourceBar.from_market_bar(
        seq=index + 1, account_id="paper:bot-a", bar=_market_bar(index, close=close)
    )


def test_bar_set_digest_changes_when_a_payload_changes() -> None:
    bars = [_retained(0), _retained(1)]
    tampered = [_retained(0), _retained(1, close="401.5")]

    assert bar_set_digest(bars) == bar_set_digest([_retained(0), _retained(1)])
    assert bar_set_digest(bars) != bar_set_digest(tampered)


def test_split_warmup_and_live_uses_run_start_boundary() -> None:
    bars = [_retained(0), _retained(1), _retained(2)]
    run_started_at_ms = bars[1].end_ms  # bar 1 closed exactly at start -> warmup

    warmup, live = split_warmup_and_live(bars, run_started_at_ms)

    assert [bar.seq for bar in warmup] == [1, 2]
    assert [bar.seq for bar in live] == [3]


def test_to_trade_bar_and_to_market_bar_round_trip_the_payload() -> None:
    retained = _retained(0)

    trade_bar = to_trade_bar(retained)
    market_bar = to_market_bar(retained)

    assert (trade_bar.symbol, trade_bar.start_ms, trade_bar.end_ms) == ("SPY", retained.start_ms, retained.end_ms)
    assert trade_bar.close == retained.close
    assert market_bar.feed_id == retained.provider
    assert market_bar.session_phase == retained.session_phase
    assert market_bar.fetched_at_ms == retained.fetched_at_ms


def test_replay_provider_for_requires_exactly_one_provider(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:bot-a")
    try:
        with pytest.raises(RunReplayUnavailableError):
            replay_provider_for(ledger, "SPY")  # zero providers

        ledger.append(_market_bar(0, feed_id="feed-a"))
        assert replay_provider_for(ledger, "SPY") == "feed-a"

        ledger.append(_market_bar(5, feed_id="feed-b"))
        with pytest.raises(RunReplayUnavailableError):
            replay_provider_for(ledger, "SPY")  # ambiguous evidence
    finally:
        ledger.close(checkpoint=False)


def test_read_run_record_returns_durable_launch_evidence(tmp_path: Path) -> None:
    repository = BotBindingRepository(tmp_path, instance_dir_for=lambda sid: tmp_path / "live_state" / sid)
    record = BotRunRecord(
        run_id="run-1",
        strategy_instance_id="bot-a",
        configuration_hash="0" * 64,
        launch_reason="deploy",
        started_at_ms=_T0,
    )
    runs_dir = tmp_path / "live_state" / "bot-a" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run-1.json").write_text(record.model_dump_json(), encoding="utf-8")

    loaded = repository.read_run_record("bot-a", "run-1")

    assert loaded is not None
    assert loaded.started_at_ms == _T0
    assert repository.read_run_record("bot-a", "run-2") is None
```

Before finalizing, re-verify the `runs/` directory name against `RUNS_DIRECTORY` in `bot_binding_repository.py` (used by `_run_path` at :656-658) and adjust the test path if it differs.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_proof_assembly.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.run_replay_proof'`.

- [ ] **Step 3: Implement**

Create `app/services/run_replay_proof.py`:

```python
"""Run-scoped replay proof: retained bars in, parity receipt out.

Direction 2 (docs/audits/strategy-execution-research-directions-2026-08-24.md):
every paper run becomes its own experiment receipt. This module owns the
pure assembly/compute pieces; the orchestration service and Stop trigger are
added by later slices of the same plan.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

from app.engine.data.trade_bar import TradeBar
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

logger = logging.getLogger(__name__)

RUN_REPLAY_RECEIPTS_DIRECTORY = "run_replay_receipts"
"""Per-run parity receipts, sibling of ``run_build_evidence/`` (same pattern)."""


class RunReplayUnavailableError(RuntimeError):
    """The replay proof cannot be computed for this run right now."""

    def __init__(self, message: str, *, detail: str = "", http_status: int = 409) -> None:
        self.detail = detail
        self.http_status = http_status
        super().__init__(message)


def bar_set_digest(bars: Sequence[RetainedSourceBar]) -> str:
    """Stable content digest of one retained stream, in durable order.

    Mirrors ``signal_program._semantic_hash``'s canonical-JSON discipline;
    excludes ``seq``/``fetched_at_ms``/``account_id`` so the digest names the
    market payload, not the storage row.
    """
    payload = [
        {
            "bar_identity": bar.bar_identity,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "session_phase": bar.session_phase,
        }
        for bar in bars
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def split_warmup_and_live(
    bars: Sequence[RetainedSourceBar],
    run_started_at_ms: int,
) -> tuple[list[RetainedSourceBar], list[RetainedSourceBar]]:
    """Split one retained stream at the run's durable launch instant.

    A bar that closed at or before ``started_at_ms`` was already history when
    the run launched, so the live run consumed it through warmup replay; a
    bar that closed after launch arrived through ``stream_bars``.
    """
    warmup = [bar for bar in bars if bar.end_ms <= run_started_at_ms]
    live = [bar for bar in bars if bar.end_ms > run_started_at_ms]
    return warmup, live


def to_trade_bar(bar: RetainedSourceBar) -> TradeBar:
    return TradeBar(
        symbol=bar.symbol,
        start_ms=bar.start_ms,
        end_ms=bar.end_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def to_market_bar(bar: RetainedSourceBar) -> MarketDataBar:
    return MarketDataBar(
        symbol=bar.symbol,
        start_ms=bar.start_ms,
        end_ms=bar.end_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        fetched_at_ms=bar.fetched_at_ms,
        feed_id=bar.provider,
        session_phase=bar.session_phase,
    )


def replay_provider_for(ledger: SourceBarLedger, symbol: str) -> str:
    """Return the one provider whose retained stream is this run's evidence."""
    providers = ledger.providers_for(symbol)
    if len(providers) != 1:
        raise RunReplayUnavailableError(
            f"Retained evidence for {symbol!r} names {len(providers)} providers; replay requires exactly one.",
            detail="A replay over mixed provider streams would not reproduce any single run.",
        )
    return providers[0]
```

Re-verify `TradeBar`'s constructor field order/types against `app/engine/data/trade_bar.py` before writing `to_trade_bar` (it is a dataclass with exactly these eight fields).

In `app/services/source_bar_ledger.py` add below `latest_for_symbol` (:260-266):

```python
    def providers_for(self, symbol: str) -> list[str]:
        """Return the distinct providers with retained evidence for one symbol."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT provider FROM source_bars WHERE symbol = ? ORDER BY provider",
                (symbol,),
            ).fetchall()
        return [str(row["provider"]) for row in rows]
```

and change `close` (:179-192) to:

```python
    def close(self, *, checkpoint: bool = True) -> None:
```

with the existing docstring plus one appended line — `"Read-only evidence consumers (run replay proof) pass checkpoint=False: they never wrote, so folding the WAL is the writer's job, and a busy checkpoint must not fail a read."` — and body:

```python
        with self._lock:
            if checkpoint:
                self.checkpoint_wal()
            self._conn.close()
```

Add `providers_for` to the class's public surface (no `__all__` change needed — the class is already exported).

In `app/services/bot_binding_repository.py`, add below `read_program_build_evidence` (:443-473), mirroring `read_outcome`'s identity check (:430-441):

```python
    def read_run_record(
        self,
        strategy_instance_id: str,
        run_id: str,
    ) -> BotRunRecord | None:
        """Return the create-once launch evidence for one run, if it exists."""
        instance_dir = self._instance_dir_for(strategy_instance_id)
        path = self._run_path(instance_dir, run_id)
        if not path.is_file():
            return None
        record = BotRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if record.strategy_instance_id != strategy_instance_id or record.run_id != run_id:
            raise ValueError("run record belongs to another run identity")
        return record
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_proof_assembly.py tests/services/test_source_bar_ledger.py -q`
Expected: PASS (the `close` signature change is backward compatible — `checkpoint` defaults to `True`).

- [ ] **Step 5: Commit**

```bash
git add app/services/run_replay_proof.py app/services/source_bar_ledger.py app/services/bot_binding_repository.py tests/services/test_run_replay_proof_assembly.py
git commit -m "feat(replay-proof): replay input assembly, ledger reads, run-record reader"
```

---

### Task 4: Engine-parity leg (wire `run_shadow_trace_evaluation` into the proof)

**Files:**
- Modify: `app/services/run_replay_proof.py`
- Test: `tests/services/test_run_replay_engine_parity.py` (create)

**Interfaces:**
- Consumes: `run_shadow_trace_evaluation`, `ShadowTraceDivergence`, `ShadowTraceDivergenceError`, `UnsupportedShadowProgramError` (`app/broker/alpaca/clerk/sqlite/qualification_shadow_trace.py:245-320` — currently invoked only by its own unit test; this task gives it its first production caller). Test helpers: `_ema_parity_bars_through_first_exit` (`tests/services/test_bot_runner.py`), `to_trade_bar` (Task 3).
- Produces:
  - `@dataclass(frozen=True) EngineParityResult(trace_root: str | None, compared_count: int, divergence: ShadowTraceDivergence | None, error: str | None)`
  - `engine_parity_over_bars(strategy_key: str, symbol: str, strategy_params: Mapping[str, Any] | None, bars: Sequence[TradeBar]) -> EngineParityResult` — **synchronous**, calls `asyncio.run(...)` internally, and therefore MUST only be called from a thread with no running event loop (the orchestrator calls it inside `asyncio.to_thread`; per `.claude/rules/python.md`, never mix `asyncio.run` with an existing loop).

- [ ] **Step 1: Write the failing test**

```python
"""Engine-parity leg: BacktestEngine vs runner seam over one run's bars."""

from __future__ import annotations

from app.services.run_replay_proof import engine_parity_over_bars, to_trade_bar
from app.services.source_bar_ledger import RetainedSourceBar
from tests.services.test_bot_runner import _ema_parity_bars_through_first_exit


def _fixture_trade_bars() -> list:
    market_bars = _ema_parity_bars_through_first_exit()
    return [
        to_trade_bar(
            RetainedSourceBar.from_market_bar(seq=index + 1, account_id="paper:t", bar=bar)
        )
        for index, bar in enumerate(market_bars)
    ]


def test_engine_parity_over_bars_proves_the_shared_seam_on_real_bars() -> None:
    bars = _fixture_trade_bars()

    result = engine_parity_over_bars("ema_crossover_signal", "SPY", None, bars)

    assert result.divergence is None
    assert result.error is None
    assert result.trace_root is not None and len(result.trace_root) == 64
    assert result.compared_count > 0


def test_engine_parity_over_bars_reports_an_unsupported_program_as_error() -> None:
    result = engine_parity_over_bars("no-such-strategy", "SPY", None, [])

    assert result.trace_root is None
    assert result.divergence is None
    assert result.error is not None and "no-such-strategy" in result.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_engine_parity.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'engine_parity_over_bars'`.

- [ ] **Step 3: Implement**

Append to `app/services/run_replay_proof.py` (add imports `asyncio`, `dataclass` from `dataclasses`, `Any`/`Mapping` from `typing`/`collections.abc`, and the three shadow-trace names):

```python
from app.broker.alpaca.clerk.sqlite.qualification_shadow_trace import (
    ShadowTraceDivergence,
    ShadowTraceDivergenceError,
    UnsupportedShadowProgramError,
    run_shadow_trace_evaluation,
)


@dataclass(frozen=True)
class EngineParityResult:
    """BacktestEngine vs runner-seam trace parity over one run's exact bars."""

    trace_root: str | None
    compared_count: int
    divergence: ShadowTraceDivergence | None
    error: str | None


def engine_parity_over_bars(
    strategy_key: str,
    symbol: str,
    strategy_params: Mapping[str, Any] | None,
    bars: Sequence[TradeBar],
) -> EngineParityResult:
    """Prove (or refute) the two-seam decision-math parity for one bar set.

    Synchronous by design: the orchestrator runs it inside
    ``asyncio.to_thread``, where ``asyncio.run`` is legal because the worker
    thread has no running loop. Never call this from a coroutine.
    """
    try:
        evaluation = asyncio.run(
            run_shadow_trace_evaluation(strategy_key, symbol, strategy_params, list(bars))
        )
    except ShadowTraceDivergenceError as error:
        return EngineParityResult(
            trace_root=None,
            compared_count=error.divergence.index,
            divergence=error.divergence,
            error=None,
        )
    except UnsupportedShadowProgramError as error:
        return EngineParityResult(trace_root=None, compared_count=0, divergence=None, error=str(error))
    return EngineParityResult(
        trace_root=evaluation.trace_root,
        compared_count=evaluation.compared_count,
        divergence=None,
        error=None,
    )
```

Note: `UnsupportedShadowProgramError` subclasses `ValueError` and `_registered_signal_program` raises it for unknown keys too (re-verify `qualification_shadow_trace.py:181-187`) — that is what the second test pins.

- [ ] **Step 4: Run test to verify it passes**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_engine_parity.py -x -q`
Expected: PASS (2 passed). This is the moment `run_shadow_trace_evaluation` stops being test-only.

- [ ] **Step 5: Commit**

```bash
git add app/services/run_replay_proof.py tests/services/test_run_replay_engine_parity.py
git commit -m "feat(replay-proof): engine-parity leg wires run_shadow_trace_evaluation"
```

---

### Task 5: Live decision evidence assembly from decision receipts

**Files:**
- Modify: `app/services/run_replay_proof.py`
- Test: `tests/services/test_run_replay_live_evidence.py` (create)

**Interfaces:**
- Consumes: `DecisionReceiptResource` (frozen dataclass: `strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, observed_at_ms, facts_json` — re-verify in `app/broker/alpaca/clerk/models.py:87-97` or wherever `grep -rn "class DecisionReceiptResource" app/` lands), `MAX_DECISION_RECEIPTS_PER_STRATEGY` (`app/broker/alpaca/clerk/sqlite/decision_receipts.py:25`). Stored `facts_json` carries `run_id`/`evaluation_id`/`reason_code` merged server-side for both ordinary and atomic appends — re-verify at `decision_receipts.py:150-270` (`stable_keys` at :496 confirms the merged identity keys).
- Produces:
  - `@dataclass(frozen=True) LiveDecisionRecord(seq: int, evaluation_id: str, outcome: str, reason_code: str, bar_ref: str, trace_digest: str, bar_close_ms: int)` — `trace_digest`/`bar_close_ms` parse the `trace_digest`/`decision_bar_close_ms` facts Task 5b captures at live time; empty string / `0` for rows recorded before that capture existed (legacy rows)
  - `@dataclass(frozen=True) LiveRunDecisionEvidence(records: tuple[LiveDecisionRecord, ...], crash_records: tuple[LiveDecisionRecord, ...], captured_decisions: dict[str, str], truncated: bool)`
  - `live_run_decision_evidence_from_rows(rows: Sequence[DecisionReceiptResource], run_id: str) -> LiveRunDecisionEvidence`
  - Semantics later tasks rely on: `records` is the run's per-bucket alignment sequence in `seq` order, **excluding** `candidate_uncaptured_at_crash` rows (those describe a *previous* run's crash window replayed at this run's warmup boundary, and warmup evaluations are never yielded by `strategy_evaluations` — they go to `crash_records` and are receipted as expected live effects directly); `captured_decisions` maps `evaluation_id -> outcome` over **all** rows (all runs), exactly the map `captured_decision_outcomes` (`bot_trade_strategy_warmup.py:174-195`) would build, so the fidelity replay's warmup reapplies prior-run dispositions exactly as the live run did.

- [ ] **Step 1: Write the failing test**

```python
"""Assembling one run's durable decision evidence for replay alignment."""

from __future__ import annotations

import json

from app.broker.alpaca.clerk.models import DecisionReceiptResource
from app.services.run_replay_proof import live_run_decision_evidence_from_rows


def _row(
    seq: int,
    *,
    outcome: str,
    run_id: str,
    evaluation_id: str,
    reason_code: str = "",
    trace_digest: str = "",
    bar_close_ms: int = 0,
) -> DecisionReceiptResource:
    return DecisionReceiptResource(
        strategy_instance_id="bot-a",
        seq=seq,
        outcome=outcome,
        symbol="SPY",
        intent_id=evaluation_id,
        order_ref=None,
        observed_at_ms=1_700_000_000_000 + seq,
        facts_json=json.dumps(
            {
                "run_id": run_id,
                "evaluation_id": evaluation_id,
                "reason_code": reason_code,
                "bar_ref": f"bar-{seq}",
                "trace_digest": trace_digest,
                "decision_bar_close_ms": bar_close_ms,
            }
        ),
    )


def test_live_run_decision_evidence_from_rows_filters_orders_and_classifies() -> None:
    rows = [
        _row(1, outcome="no_action", run_id="run-0", evaluation_id="e0", reason_code="NO_ACTION"),
        _row(2, outcome="candidate_uncaptured_at_crash", run_id="run-1", evaluation_id="e1",
             reason_code="CANDIDATE_UNCAPTURED_AT_CRASH"),
        _row(3, outcome="no_action", run_id="run-1", evaluation_id="e2", reason_code="NO_ACTION"),
        _row(4, outcome="blocked", run_id="run-1", evaluation_id="e3", reason_code="MARKET_CLOSED",
             trace_digest="a" * 64, bar_close_ms=1_700_000_900_000),
        _row(5, outcome="enter_intent", run_id="run-1", evaluation_id="e4"),
    ]

    evidence = live_run_decision_evidence_from_rows(rows, "run-1")

    assert [record.evaluation_id for record in evidence.records] == ["e2", "e3", "e4"]
    assert [record.evaluation_id for record in evidence.crash_records] == ["e1"]
    assert evidence.records[1].reason_code == "MARKET_CLOSED"
    assert evidence.records[1].trace_digest == "a" * 64
    assert evidence.records[1].bar_close_ms == 1_700_000_900_000
    assert evidence.records[0].trace_digest == ""  # legacy row: digest-less, disclosed not guessed
    assert evidence.records[0].bar_close_ms == 0
    assert evidence.captured_decisions == {
        "e0": "no_action",
        "e1": "candidate_uncaptured_at_crash",
        "e2": "no_action",
        "e3": "blocked",
        "e4": "enter_intent",
    }
    assert evidence.truncated is False


def test_live_run_decision_evidence_from_rows_flags_a_full_retention_window() -> None:
    from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY

    rows = [
        _row(seq, outcome="no_action", run_id="run-1", evaluation_id=f"e{seq}", reason_code="NO_ACTION")
        for seq in range(1, MAX_DECISION_RECEIPTS_PER_STRATEGY + 1)
    ]

    evidence = live_run_decision_evidence_from_rows(rows, "run-1")

    assert evidence.truncated is True
```

Adjust the `DecisionReceiptResource` import path if the grep in **Consumes** lands elsewhere.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_live_evidence.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'live_run_decision_evidence_from_rows'`.

- [ ] **Step 3: Implement**

Append to `app/services/run_replay_proof.py`:

```python
from app.broker.alpaca.clerk.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY

_CRASH_OUTCOME = "candidate_uncaptured_at_crash"


@dataclass(frozen=True)
class LiveDecisionRecord:
    """One durable per-bucket decision fact from the run's receipt journal."""

    seq: int
    evaluation_id: str
    outcome: str
    reason_code: str
    bar_ref: str
    # Task 5b live-time capture; empty/0 on rows recorded before it existed.
    trace_digest: str
    bar_close_ms: int


@dataclass(frozen=True)
class LiveRunDecisionEvidence:
    """Everything the fidelity replay needs from the decision-receipt journal."""

    records: tuple[LiveDecisionRecord, ...]
    crash_records: tuple[LiveDecisionRecord, ...]
    captured_decisions: dict[str, str]
    truncated: bool


def live_run_decision_evidence_from_rows(
    rows: Sequence[DecisionReceiptResource],
    run_id: str,
) -> LiveRunDecisionEvidence:
    """Shape one instance's retained receipt window into run-scoped evidence.

    ``records`` alignment excludes crash-window receipts: FR-016 records them
    during the *next* run's warmup replay, and warmup evaluations are never
    yielded by ``strategy_evaluations``, so they can never align with a
    replayed live bucket. They are reported as expected live effects instead.
    ``captured_decisions`` deliberately spans every run — it is the same
    map ``captured_decision_outcomes`` builds for the live warmup replay.
    """
    records: list[LiveDecisionRecord] = []
    crash_records: list[LiveDecisionRecord] = []
    captured: dict[str, str] = {}
    for row in rows:  # retained_window() yields ascending seq order
        facts = json.loads(row.facts_json) if row.facts_json else {}
        evaluation_id = row.intent_id or str(facts.get("evaluation_id") or "")
        if evaluation_id:
            captured[evaluation_id] = row.outcome
        if str(facts.get("run_id") or "") != run_id:
            continue
        if not evaluation_id:
            raise RunReplayUnavailableError(
                f"Decision receipt seq {row.seq} for run {run_id!r} carries no evaluation identity.",
                detail="Replay alignment is keyed on evaluation_id; this journal cannot be aligned.",
            )
        record = LiveDecisionRecord(
            seq=row.seq,
            evaluation_id=evaluation_id,
            outcome=row.outcome,
            reason_code=str(facts.get("reason_code") or ""),
            bar_ref=str(facts.get("bar_ref") or ""),
            trace_digest=str(facts.get("trace_digest") or ""),
            bar_close_ms=int(facts.get("decision_bar_close_ms") or 0),
        )
        (crash_records if row.outcome == _CRASH_OUTCOME else records).append(record)
    return LiveRunDecisionEvidence(
        records=tuple(records),
        crash_records=tuple(crash_records),
        captured_decisions=captured,
        truncated=len(rows) >= MAX_DECISION_RECEIPTS_PER_STRATEGY,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_live_evidence.py -x -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/run_replay_proof.py tests/services/test_run_replay_live_evidence.py
git commit -m "feat(replay-proof): run-scoped live decision evidence assembly"
```

---

### Task 5b: Live-time trace-digest capture into decision receipts (PR #1751 finding 3a)

**Files:**
- Modify: `app/services/bot_trade_strategy.py` (`_append_decision_receipt` at :811-839; the two `EffectDecisionEvidence(...)` constructions in `run_trade_bot` :766-776 and `run_dry_run_bot` :950-964; the signal_program import block at :31-38 gains `trace_root`)
- Modify: `app/broker/alpaca/clerk/decision_evidence.py` (`EffectDecisionEvidence` at :10-19)
- Modify: `app/broker/alpaca/clerk/sqlite/runtime.py` (atomic facts dict at :606-618; `_append_pre_custody_refusal` facts at :1073-1085)
- Test: `tests/services/test_run_replay_live_capture.py` (create)

**Interfaces:**
- Consumes: `trace_root` (`app/engine/strategy/signal_program.py:359-361` — the canonical trace hashing; NOT re-implemented), `StrategyEvaluation.trace` (`bot_trade_strategy.py:100` — the full canonical trace, populated for every registered Signal Program), `EvaluationTrace` (constructible dataclass, `signal_program.py:51-71`), `_append_pre_custody_refusal` (`runtime.py:1073` — signature `(repo, *, strategy_instance_id, run_id, evidence, reason_code, explanation)`, re-verify), `ClerkSqliteRepository.initialize` / `SqliteDecisionReceipts` (as in Task 2's test).
- Produces: every decision-receipt fact dict (ordinary appends, atomic effect appends, and Clerk pre-custody `blocked` refusals) carries `"trace_digest"` (per-bucket `trace_root([trace])`, omitted only when the evaluation has no trace) and `"decision_bar_close_ms"` (int64 ms UTC). `EffectDecisionEvidence` gains `trace_digest: str | None = None` and `decision_bar_close_ms: int | None = None` (optional with `None` defaults — every existing constructor keeps working). Sealed-program bytes untouched: `signal_program.py` is only *called*.

- [ ] **Step 1: Write the failing test**

```python
"""Live-time capture: decision receipts carry the canonical per-bucket trace digest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import _append_pre_custody_refusal
from app.engine.strategy.signal_program import EvaluationMode, EvaluationTrace, trace_root
from app.marketdata.feed import MarketDataBar
from app.services.bot_trade_strategy import StrategyEvaluation, _append_decision_receipt
from tests.services.test_candidate_uncaptured_at_crash import _binding

_T0 = 1_700_000_000_000
_EVAL_ID = "ab" * 32


def _trace() -> EvaluationTrace:
    return EvaluationTrace(
        program_key="ema_crossover_signal",
        program_version="v1",
        evaluation_id=_EVAL_ID,
        bar_close_ms=_T0 + 900_000,
        bar_qualified=True,
        bucket_closed=True,
        ready=True,
        relation_facts={},
        signal_facts={},
        staged_candidate=None,
        reason_evidence={},
        action_plan_request=None,
        evaluation_mode=EvaluationMode.DECIDE,
    )


def _evaluation(trace: EvaluationTrace | None) -> StrategyEvaluation:
    bar = MarketDataBar(
        symbol="SPY", start_ms=_T0, end_ms=_T0 + 60_000,
        open=Decimal("400"), high=Decimal("401"), low=Decimal("399"), close=Decimal("400.5"),
        volume=100, fetched_at_ms=_T0 + 60_500, feed_id="fake-phase", session_phase="RTH",
    )
    return StrategyEvaluation(
        bar=bar,
        evaluation_id=_EVAL_ID,
        decision_bar_close_ms=_T0 + 900_000,
        intents=(),
        settle_stage=lambda _settlement: None,
        trace=trace,
    )


class _CapturingReceipts:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    def append(self, **kwargs) -> None:
        self.appended.append(kwargs)


def test_append_decision_receipt_captures_trace_digest_and_bucket_close() -> None:
    receipts = _CapturingReceipts()
    trace = _trace()

    _append_decision_receipt(
        receipts,  # type: ignore[arg-type] -- duck-typed capture double
        binding=_binding(run_id="run-1"),
        evaluation=_evaluation(trace),
        outcome="no_action",
        reason_code="NO_ACTION",
    )

    facts = receipts.appended[0]["facts"]
    assert facts["trace_digest"] == trace_root([trace])
    assert facts["decision_bar_close_ms"] == _T0 + 900_000


def test_append_decision_receipt_omits_digest_for_a_traceless_evaluation() -> None:
    receipts = _CapturingReceipts()

    _append_decision_receipt(
        receipts,  # type: ignore[arg-type]
        binding=_binding(run_id="run-1"),
        evaluation=_evaluation(None),
        outcome="no_action",
        reason_code="NO_ACTION",
    )

    assert "trace_digest" not in receipts.appended[0]["facts"]


def test_pre_custody_refusal_receipt_carries_the_evidence_digest(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-CAP", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id="bot-a", symbol="SPY", config_hash="c1")

    _append_pre_custody_refusal(
        repo,
        strategy_instance_id="bot-a",
        run_id="run-1",
        evidence=EffectDecisionEvidence(
            evaluation_id=_EVAL_ID,
            bar_ref="decision-bar:fake-phase:SPY:1700000900000",
            symbol="SPY",
            outcome="enter_intent",
            observed_at_ms=_T0,
            trace_digest="cd" * 32,
            decision_bar_close_ms=_T0 + 900_000,
        ),
        reason_code="MARKET_LIVENESS_BLOCKED",
        explanation="stale evidence at intake",
    )

    rows = SqliteDecisionReceipts(repo, strategy_instance_id="bot-a").retained_window()
    facts = json.loads(rows[-1].facts_json)
    assert facts["trace_digest"] == "cd" * 32
    assert facts["decision_bar_close_ms"] == _T0 + 900_000
```

Re-verify before running: `_append_pre_custody_refusal`'s exact parameters at `runtime.py:1073` (mirror the call site at :571-579) and whether `register_strategy_instance`'s kwargs match Task 2's usage.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_live_capture.py -x -q`
Expected: FAIL — `TypeError`/`ValidationError`: `EffectDecisionEvidence` got unexpected `trace_digest` (extra="forbid"), and `facts["trace_digest"]` KeyError for the ordinary append.

- [ ] **Step 3: Implement the capture**

1. `app/broker/alpaca/clerk/decision_evidence.py` — add two optional fields to `EffectDecisionEvidence` (after `observed_at_ms`):

```python
    # Direction 2 (run-scoped replay proof): the canonical per-bucket trace
    # digest (`trace_root([trace])`) and the decision bucket's close, captured
    # at live time so a replay can compare decision CONTENT, not just intent
    # direction. Optional: legacy callers and traceless compatibility
    # strategies omit them; the replay receipt discloses digest coverage.
    trace_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_bar_close_ms: int | None = Field(default=None, ge=0)
```

2. `app/services/bot_trade_strategy.py`:
   - Add `trace_root` to the `app.engine.strategy.signal_program` import block (:31-38).
   - In `_append_decision_receipt` (:811-839), after the `facts` dict literal:

```python
    facts["decision_bar_close_ms"] = evaluation.decision_bar_close_ms
    if evaluation.trace is not None:
        facts["trace_digest"] = trace_root([evaluation.trace])
```

   - In `run_trade_bot`'s `EffectDecisionEvidence(...)` construction (:766-776) and `run_dry_run_bot`'s (:950-964), add:

```python
                trace_digest=(
                    trace_root([evaluation.trace]) if evaluation.trace is not None else None
                ),
                decision_bar_close_ms=evaluation.decision_bar_close_ms,
```

3. `app/broker/alpaca/clerk/sqlite/runtime.py`:
   - Atomic facts dict inside `execute_for_instance` (:612-618) — extend the `canonicalize({...})` dict:

```python
                            "reason_code": decision_evidence.reason_code,
                            "trace_digest": decision_evidence.trace_digest,
                            "decision_bar_close_ms": decision_evidence.decision_bar_close_ms,
```

   - `_append_pre_custody_refusal` (:1073-1085) — extend its facts dict the same way (`evidence.trace_digest`, `evidence.decision_bar_close_ms`). These receipt facts live in the receipts table, not in the custody transition `row_hash` payload (re-verify against `repository.py:846-876`: `row_hash` covers `payload`, the receipt is a separate insert in the same transaction), so no hash chain or sealed identity is touched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_live_capture.py tests/services/test_candidate_uncaptured_at_crash.py tests/broker/alpaca/clerk/sqlite -q`
Expected: PASS — the clerk sqlite suite proves no hash-chain/receipt regression from the two new fact keys (`None` values serialize as JSON null for legacy-shaped evidence).

- [ ] **Step 5: Commit**

```bash
git add app/services/bot_trade_strategy.py app/broker/alpaca/clerk/decision_evidence.py app/broker/alpaca/clerk/sqlite/runtime.py tests/services/test_run_replay_live_capture.py
git commit -m "feat(replay-proof): capture canonical trace digests into decision receipts at live time"
```

---

### Task 6: Run-fidelity leg — disposition-faithful replay + divergence classifier

**Files:**
- Modify: `app/services/run_replay_proof.py`
- Test: `tests/services/test_run_replay_fidelity.py` (create)

**Interfaces:**
- Consumes: `strategy_evaluations` (`app/services/bot_trade_strategy.py:572-591` — the exact generator Paper and Dry Run share), `_includes_session_phase` (`bot_trade_strategy.py:306-312`), `_COMMIT_WORTHY_OUTCOMES` (`bot_trade_strategy_warmup.py:56-58`), `Settlement` (`app.engine.strategy.signal_program`), `FeedHealth`/`MarketDataBar` (`app.marketdata.feed`), `now_ms_utc` (`app.utils.timestamps`), Task 3's `to_market_bar`, Task 5's `LiveDecisionRecord`.
- Produces:
  - `EXPECTED_LIVE_GATE_REASON_CODES: frozenset[str]` — the closed live-only-gate set (owners cited per entry; `STREAM_HEALTH_REASON_CODE` imported from `clerk/sqlite/runtime.py:124`, not restated)
  - `@dataclass(frozen=True) RunFidelityDivergence(evaluation_id: str, bar_close_ms: int, classification: str, reason_code: str, replay_staged: str | None, live_outcome: str | None, detail: str)` where `classification` is `"expected_live_effect"` or `"drift"`; drift reasons: `TRACE_DIGEST_MISMATCH`, `DECISION_MISMATCH`, `UNRECOGNIZED_BLOCK_REASON`, `MISSING_LIVE_RECORD`, `EVALUATION_ID_MISMATCH`, `UNMATCHED_LIVE_RECORD`
  - `@dataclass(frozen=True) RunFidelityResult(compared_count: int, match_count: int, expected_live_effect_count: int, drift_count: int, digest_verified_count: int, divergences: tuple[RunFidelityDivergence, ...])` — `digest_verified_count` = aligned buckets whose live `trace_digest` was present and matched the replayed `trace_root([evaluation.trace])` (finding 3's coverage disclosure)
  - `class _RunReplayFeed` — in-memory feed: `feed_id = provider`, `capability_account_id -> None`, `recent_closed_bars` returns the warmup list (session-phase-filtered, `lookback_days` ignored — mirroring the retained branch of `_RetainedSourceBarFeed`), `stream_bars` yields the live list (session-phase-filtered), `health` returns a connected `FeedHealth`.
  - `async run_fidelity_over_bars(binding: BrokerBotBinding, *, provider: str, warmup: Sequence[RetainedSourceBar], live: Sequence[RetainedSourceBar], records: Sequence[LiveDecisionRecord], captured_decisions: Mapping[str, str]) -> RunFidelityResult`

- [ ] **Step 1: Write the failing test**

```python
"""Disposition-faithful run replay: classification of live-vs-math divergence."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.marketdata.feed import MarketDataBar
from app.services.bot_trade_strategy import strategy_evaluations
from app.engine.strategy.signal_program import Settlement, trace_root
from app.services.run_replay_proof import (
    LiveDecisionRecord,
    run_fidelity_over_bars,
)
from app.services.source_bar_ledger import RetainedSourceBar
from tests.services.test_bot_runner import _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import _binding, _PhaseFeed


def _retained(bars: Sequence[MarketDataBar]) -> list[RetainedSourceBar]:
    return [
        RetainedSourceBar.from_market_bar(seq=index + 1, account_id="paper:t", bar=bar)
        for index, bar in enumerate(bars)
    ]


async def _record_live_pass(bars: Sequence[MarketDataBar], *, block_first_enter: bool) -> list[LiveDecisionRecord]:
    """Simulate exactly what run_trade_bot durably records for each bucket,
    including the Task 5b live-time trace-digest capture."""
    binding = _binding(run_id="run-1")
    records: list[LiveDecisionRecord] = []
    blocked_once = False
    async for evaluation in strategy_evaluations(binding, _PhaseFeed(live_bars=list(bars))):
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        seq = len(records) + 1
        digest = trace_root([evaluation.trace]) if evaluation.trace is not None else ""
        close_ms = evaluation.decision_bar_close_ms
        if staged is None:
            records.append(LiveDecisionRecord(seq=seq, evaluation_id=evaluation.evaluation_id,
                                              outcome="no_action", reason_code="NO_ACTION", bar_ref="",
                                              trace_digest=digest, bar_close_ms=close_ms))
            evaluation.settle_stage(Settlement.COMMIT)
        elif block_first_enter and staged == "ENTER" and not blocked_once:
            blocked_once = True
            records.append(LiveDecisionRecord(seq=seq, evaluation_id=evaluation.evaluation_id,
                                              outcome="blocked", reason_code="MARKET_CLOSED", bar_ref="",
                                              trace_digest=digest, bar_close_ms=close_ms))
            evaluation.settle_stage(Settlement.DISCARD)
        else:
            outcome = "enter_intent" if staged == "ENTER" else "exit_intent"
            records.append(LiveDecisionRecord(seq=seq, evaluation_id=evaluation.evaluation_id,
                                              outcome=outcome, reason_code="", bar_ref="",
                                              trace_digest=digest, bar_close_ms=close_ms))
            evaluation.settle_stage(Settlement.COMMIT)
    return records


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_full_parity_on_an_unblocked_run() -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    assert any(record.outcome == "enter_intent" for record in records)  # guard the guard

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.compared_count == len(records) > 0
    assert result.match_count == len(records)
    assert result.expected_live_effect_count == 0
    assert result.drift_count == 0
    assert result.digest_verified_count == len(records)  # every bucket content-verified


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_classifies_a_blocked_enter_as_expected() -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=True)

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count == 0
    assert result.expected_live_effect_count >= 1
    first = next(d for d in result.divergences if d.classification == "expected_live_effect")
    assert first.reason_code == "MARKET_CLOSED"
    assert first.replay_staged == "ENTER"
    assert first.live_outcome == "blocked"


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_classifies_a_tampered_record_as_drift() -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    victim = next(i for i, record in enumerate(records) if record.outcome == "no_action")
    records[victim] = LiveDecisionRecord(
        seq=records[victim].seq,
        evaluation_id=records[victim].evaluation_id,
        outcome="enter_intent",
        reason_code="",
        bar_ref="",
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "DECISION_MISMATCH"


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_flags_a_content_level_digest_mismatch_as_drift() -> None:
    """PR #1751 finding 3: same intent direction, different trace CONTENT -> drift."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    victim = next(i for i, record in enumerate(records) if record.outcome == "no_action")
    records[victim] = LiveDecisionRecord(
        seq=records[victim].seq,
        evaluation_id=records[victim].evaluation_id,
        outcome=records[victim].outcome,          # intent-level identical
        reason_code=records[victim].reason_code,
        bar_ref="",
        trace_digest="f" * 64,                    # content-level different
        bar_close_ms=records[victim].bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "TRACE_DIGEST_MISMATCH"


@pytest.mark.asyncio
async def test_run_fidelity_over_bars_refuses_a_blocked_row_with_an_unrecognized_reason() -> None:
    """PR #1751 finding 3b: a `blocked` row is cross-checked, never trusted on presence."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=True)
    blocked = next(i for i, record in enumerate(records) if record.outcome == "blocked")
    records[blocked] = LiveDecisionRecord(
        seq=records[blocked].seq,
        evaluation_id=records[blocked].evaluation_id,
        outcome="blocked",
        reason_code="TOTALLY_MADE_UP_GATE",       # outside the closed live-only-gate set
        bar_ref="",
        trace_digest=records[blocked].trace_digest,
        bar_close_ms=records[blocked].bar_close_ms,
    )

    result = await run_fidelity_over_bars(
        _binding(run_id="run-1"),
        provider="fake-phase",
        warmup=[],
        live=_retained(bars),
        records=records,
        captured_decisions={},
    )

    assert result.drift_count >= 1
    drift = next(d for d in result.divergences if d.classification == "drift")
    assert drift.reason_code == "UNRECOGNIZED_BLOCK_REASON"
```

Note the important property the blocked-ENTER test pins: because the fake blocked ENTER is settled `DISCARD` in **both** the recorder and the replay, every bucket after it still matches — the expected-live-effect fork never cascades into drift. And per finding 3, `MARKET_CLOSED` classifies as expected only because it is in `EXPECTED_LIVE_GATE_REASON_CODES` *and* its digest matches — the two new tests pin both refusal directions.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_fidelity.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'run_fidelity_over_bars'`.

- [ ] **Step 3: Implement**

Append to `app/services/run_replay_proof.py` (new imports: `AsyncIterator`, `Mapping`, `deque` from `collections`, `Settlement`, `strategy_evaluations`, `_includes_session_phase`, `_COMMIT_WORTHY_OUTCOMES`, `FeedHealth`, `BrokerBotBinding` (TYPE_CHECKING import mirroring `bot_trade_strategy.py:49-51` to avoid a cycle — re-verify: `run_replay_proof` importing `bot_trade_strategy` at runtime is fine since `bot_trade_strategy` does not import this module; a plain runtime import is acceptable), `now_ms_utc`):

```python
from app.broker.alpaca.clerk.sqlite.runtime import STREAM_HEALTH_REASON_CODE
from app.engine.strategy.signal_program import Settlement, trace_root
from app.marketdata.feed import FeedHealth
from app.services.bot_trade_strategy import _includes_session_phase, strategy_evaluations
from app.services.bot_trade_strategy_warmup import _COMMIT_WORTHY_OUTCOMES
from app.utils.timestamps import now_ms_utc

_OUTCOMES_BY_STAGED_KIND: dict[str, frozenset[str]] = {
    "ENTER": frozenset({"enter_intent", "entered"}),
    "EXIT": frozenset({"exit_intent", "exited"}),
}

EXPECTED_LIVE_GATE_REASON_CODES: frozenset[str] = frozenset(
    {
        # bot_trade_strategy.py: the pause gate's blocked-receipt reason.
        "PAUSED_OBSERVE_ONLY",
        # app/services/market_liveness.py:60-205 — every liveness fact reason
        # that can block an ENTER at the pre-Clerk gate. MARKET_TRADABLE is
        # deliberately absent: it never blocks.
        "MARKET_LIVENESS_UNAVAILABLE",
        "MARKET_CLOCK_UNAVAILABLE",
        "SYMBOL_HALTED",
        "SYMBOL_STATUS_UNKNOWN",
        "MARKET_CLOSED",
        "MARKET_CLOCK_UNKNOWN",
        "STATUS_STREAM_DISCONNECTED",
        # app/broker/alpaca/clerk/sqlite/runtime.py — every rejected() branch
        # that appends a pre-custody `blocked` receipt (:595, :668, :729, :752
        # plus the stream-health hold whose constant we import).
        STREAM_HEALTH_REASON_CODE,
        "MARKET_LIVENESS_BLOCKED",
        "SIMULATED_SOURCE_BAR_UNPROVEN",
        "EXIT_CUSTODY_UNPROVEN",
    }
)
"""The CLOSED set of live-only gates (PR #1751 finding 3b).

A `blocked` receipt whose reason is outside this set is classified `drift`
(`UNRECOGNIZED_BLOCK_REASON`), never trusted. When a new live-only gate is
added to the runner or Clerk intake, its reason code must be added here in
the same PR — the classifier failing closed on the new code is the reminder.
"""


@dataclass(frozen=True)
class RunFidelityDivergence:
    """One classified disagreement between the replayed math and the live record."""

    evaluation_id: str
    bar_close_ms: int
    classification: str  # "expected_live_effect" | "drift"
    reason_code: str
    replay_staged: str | None
    live_outcome: str | None
    detail: str


@dataclass(frozen=True)
class RunFidelityResult:
    compared_count: int
    match_count: int
    expected_live_effect_count: int
    drift_count: int
    # Aligned buckets whose live trace_digest was present AND matched the
    # replayed trace — the receipt's disclosure of content-level coverage
    # (digest-less legacy rows fall back to intent-kind comparison).
    digest_verified_count: int
    divergences: tuple[RunFidelityDivergence, ...]


class _RunReplayFeed:
    """In-memory feed replaying one retained stream through the shared seam.

    ``recent_closed_bars`` returns the warmup slice regardless of
    ``lookback_days`` — the exact behavior of ``_RetainedSourceBarFeed``'s
    retained branch, which is what the live run's own warmup consumed.
    Exposes no ``evaluation_mode_for``, so every bar replays in DECIDE mode
    (``bot_trade_strategy._evaluation_mode_for`` fallback); live OBSERVE_ONLY
    buckets are receipted ``blocked``/``PAUSED_OBSERVE_ONLY`` and classify as
    expected live effects.
    """

    def __init__(
        self,
        *,
        provider: str,
        symbol: str,
        warmup_bars: Sequence[MarketDataBar],
        live_bars: Sequence[MarketDataBar],
    ) -> None:
        self.feed_id = provider
        self._symbol = symbol
        self._warmup_bars = list(warmup_bars)
        self._live_bars = list(live_bars)

    @property
    def capability_account_id(self) -> None:
        return None

    async def stream_bars(self, symbol: str, *, use_rth: bool = True) -> AsyncIterator[MarketDataBar]:
        for bar in self._live_bars:
            if bar.symbol == symbol and _includes_session_phase(bar, use_rth=use_rth):
                yield bar

    async def recent_closed_bars(
        self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5
    ) -> list[MarketDataBar]:
        del lookback_days
        return [
            bar
            for bar in self._warmup_bars
            if bar.symbol == symbol and _includes_session_phase(bar, use_rth=use_rth)
        ]

    def health(self, symbol: str | None = None) -> FeedHealth:
        del symbol
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._live_bars[-1].end_ms if self._live_bars else None,
            reason="",
            active_subscription_count=1,
            observed_at_ms=now_ms_utc(),
        )


async def run_fidelity_over_bars(
    binding: BrokerBotBinding,
    *,
    provider: str,
    warmup: Sequence[RetainedSourceBar],
    live: Sequence[RetainedSourceBar],
    records: Sequence[LiveDecisionRecord],
    captured_decisions: Mapping[str, str],
) -> RunFidelityResult:
    """Replay the run's bars through the production seam, settling each stage
    with the live-recorded disposition, and classify every disagreement.

    Warmup buckets settle inside ``strategy_evaluations`` via
    ``captured_decisions`` (the FR-016 machinery) and are never yielded, so
    yielded evaluations align 1:1 with the run's own receipt sequence.
    """
    feed = _RunReplayFeed(
        provider=provider,
        symbol=binding.symbol,
        warmup_bars=[to_market_bar(bar) for bar in warmup],
        live_bars=[to_market_bar(bar) for bar in live],
    )
    pending = deque(records)
    divergences: list[RunFidelityDivergence] = []
    compared = 0
    match_count = 0
    digest_verified = 0
    async for evaluation in strategy_evaluations(
        binding, feed, captured_decisions=dict(captured_decisions)
    ):
        if evaluation.crash_recovered:
            # A warmup bucket whose receipt aged out of retention replays as
            # uncaptured; it precedes this run's live window and was already
            # settled DISCARD inside the warmup machinery. Not part of the
            # alignment sequence.
            continue
        compared += 1
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        record = pending.popleft() if pending else None
        if record is None:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="MISSING_LIVE_RECORD",
                    replay_staged=staged,
                    live_outcome=None,
                    detail="The replay produced a decision bucket the live journal never recorded.",
                )
            )
            evaluation.settle_stage(Settlement.DISCARD)
            continue
        if record.evaluation_id != evaluation.evaluation_id:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="EVALUATION_ID_MISMATCH",
                    replay_staged=staged,
                    live_outcome=record.outcome,
                    detail=f"Live journal recorded {record.evaluation_id!r} at this position.",
                )
            )
            evaluation.settle_stage(Settlement.DISCARD)
            continue
        settlement = (
            Settlement.COMMIT if record.outcome in _COMMIT_WORTHY_OUTCOMES else Settlement.DISCARD
        )
        # Content-level comparison first (PR #1751 finding 3): evaluation_id
        # hashes identity, not decision content — only the digest proves the
        # replayed trace IS the live trace. Digest-less legacy rows fall back
        # to intent-kind comparison and are excluded from digest_verified.
        replay_digest = None if evaluation.trace is None else trace_root([evaluation.trace])
        digest_checked = bool(record.trace_digest) and replay_digest is not None
        if digest_checked and record.trace_digest != replay_digest:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="TRACE_DIGEST_MISMATCH",
                    replay_staged=staged,
                    live_outcome=record.outcome,
                    detail=(
                        "Replayed trace content differs from the live-captured digest "
                        f"(live={record.trace_digest} replay={replay_digest})."
                    ),
                )
            )
            evaluation.settle_stage(settlement)
            continue
        if digest_checked:
            digest_verified += 1
        if staged is None and record.outcome == "no_action":
            match_count += 1
        elif staged is not None and record.outcome in _OUTCOMES_BY_STAGED_KIND.get(staged, frozenset()):
            match_count += 1
        elif staged is not None and record.outcome == "blocked":
            # A blocked row is cross-checked, never trusted on presence: the
            # replay staged the intent (guaranteed by this branch), the digest
            # matched (checked above when present), and the reason must be a
            # known live-only gate — anything else is drift, fail closed.
            if record.reason_code in EXPECTED_LIVE_GATE_REASON_CODES:
                divergences.append(
                    RunFidelityDivergence(
                        evaluation_id=evaluation.evaluation_id,
                        bar_close_ms=evaluation.decision_bar_close_ms,
                        classification="expected_live_effect",
                        reason_code=record.reason_code,
                        replay_staged=staged,
                        live_outcome=record.outcome,
                        detail=(
                            "The shared math staged this intent; a live-only gate "
                            "(liveness, pause, or Clerk refusal) durably refused it."
                        ),
                    )
                )
            else:
                divergences.append(
                    RunFidelityDivergence(
                        evaluation_id=evaluation.evaluation_id,
                        bar_close_ms=evaluation.decision_bar_close_ms,
                        classification="drift",
                        reason_code="UNRECOGNIZED_BLOCK_REASON",
                        replay_staged=staged,
                        live_outcome=record.outcome,
                        detail=(
                            f"Blocked reason {record.reason_code!r} is not in the closed "
                            "live-only-gate set; refusing to classify it as expected."
                        ),
                    )
                )
        else:
            divergences.append(
                RunFidelityDivergence(
                    evaluation_id=evaluation.evaluation_id,
                    bar_close_ms=evaluation.decision_bar_close_ms,
                    classification="drift",
                    reason_code="DECISION_MISMATCH",
                    replay_staged=staged,
                    live_outcome=record.outcome,
                    detail="Replayed decision and live receipt disagree with no enumerating live effect.",
                )
            )
        evaluation.settle_stage(settlement)
    for leftover in pending:
        divergences.append(
            RunFidelityDivergence(
                evaluation_id=leftover.evaluation_id,
                bar_close_ms=0,
                classification="drift",
                reason_code="UNMATCHED_LIVE_RECORD",
                replay_staged=None,
                live_outcome=leftover.outcome,
                detail=f"Live journal receipt (bar_ref={leftover.bar_ref!r}) has no replayed bucket.",
            )
        )
    expected = sum(1 for d in divergences if d.classification == "expected_live_effect")
    drift = sum(1 for d in divergences if d.classification == "drift")
    return RunFidelityResult(
        compared_count=compared,
        match_count=match_count,
        expected_live_effect_count=expected,
        drift_count=drift,
        digest_verified_count=digest_verified,
        divergences=tuple(divergences),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_fidelity.py -x -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint the touched files**

Run: `ruff check app/services/run_replay_proof.py tests/services/test_run_replay_fidelity.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add app/services/run_replay_proof.py tests/services/test_run_replay_fidelity.py
git commit -m "feat(replay-proof): disposition-faithful fidelity replay with divergence classification"
```

---

### Task 7: Receipt schema + durable per-run store

**Files:**
- Create: `app/schemas/run_replay.py`
- Modify: `app/services/run_replay_proof.py` (store functions)
- Test: `tests/services/test_run_replay_receipt_store.py` (create)

**Interfaces:**
- Consumes: `safe_path_component` (`app/broker/alpaca/paths.py:24`), `RUN_REPLAY_RECEIPTS_DIRECTORY` (Task 3).
- Produces (schemas, all snake_case, all times `int64 ms UTC`):
  - `EngineParityDivergenceModel(index: int, evaluation_id: str | None, field: str, expected: str, observed: str)`
  - `RunReplayDivergenceModel(evaluation_id: str, bar_close_ms: int, classification: Literal["expected_live_effect", "drift"], reason_code: str, replay_staged: str | None, live_outcome: str | None, detail: str)`
  - `RunReplayReceipt` — fields exactly: `schema_version: Literal[1] = 1`, `strategy_instance_id: str`, `run_id: str`, `strategy_key: str`, `symbol: str`, `provider: str`, `status: Literal["pending", "parity", "parity_with_expected_live_effects", "indeterminate", "drift", "replay_failed"]` (`indeterminate` = evidence known-incomplete or engine leg unprovable — never a proof verdict from partial evidence; PR #1751 finding 6), `bar_set_digest: str`, `retained_bar_count: int (ge=0)`, `ledger_end_seq: int | None` (run-end bound snapshotted at Stop; PR #1751 finding 4 — regeneration reuses it so run N's input never grows when run N+1 appends), `engine_parity_trace_root: str | None`, `engine_parity_compared_count: int (ge=0)`, `engine_parity_divergence: EngineParityDivergenceModel | None`, `live_compared_count: int (ge=0)`, `match_count: int (ge=0)`, `expected_live_effect_count: int (ge=0)`, `drift_count: int (ge=0)`, `digest_verified_count: int (ge=0)` (content-level coverage disclosure — buckets whose live trace digest was verified; digest-less legacy rows are the receipt's stated residual blind spot), `records_truncated: bool`, `divergences: list[RunReplayDivergenceModel]`, `program_version: str | None`, `sealed_program_hash: str | None`, `generated_at_ms: int (ge=0)`, `error: str | None = None`. `model_config = ConfigDict(frozen=True, extra="forbid")`.
  - Store: `write_run_replay_receipt(instance_dir: Path, receipt: RunReplayReceipt) -> Path` (atomic temp+`os.replace`; replaceable — pending→final and on-demand regeneration are legitimate rewrites, unlike the create-once `run_build_evidence` pattern) and `read_run_replay_receipt(instance_dir: Path, strategy_instance_id: str, run_id: str) -> RunReplayReceipt | None` (identity-checked like `read_program_build_evidence`).

- [ ] **Step 1: Write the failing test**

```python
"""Durable per-run replay receipts under live_state/<sid>/run_replay_receipts/."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.run_replay import RunReplayReceipt
from app.services.run_replay_proof import (
    read_run_replay_receipt,
    write_run_replay_receipt,
)


def _receipt(*, status: str = "pending", run_id: str = "run-1") -> RunReplayReceipt:
    return RunReplayReceipt(
        strategy_instance_id="bot-a",
        run_id=run_id,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        provider="feed-a",
        status=status,
        bar_set_digest="0" * 64,
        retained_bar_count=0,
        ledger_end_seq=None,
        engine_parity_trace_root=None,
        engine_parity_compared_count=0,
        engine_parity_divergence=None,
        live_compared_count=0,
        match_count=0,
        expected_live_effect_count=0,
        drift_count=0,
        digest_verified_count=0,
        records_truncated=False,
        divergences=[],
        program_version=None,
        sealed_program_hash=None,
        generated_at_ms=1_700_000_000_000,
    )


def test_write_then_read_round_trips_and_pending_is_replaceable(tmp_path: Path) -> None:
    instance_dir = tmp_path / "live_state" / "bot-a"

    path = write_run_replay_receipt(instance_dir, _receipt(status="pending"))
    assert path == instance_dir / "run_replay_receipts" / "run-1.json"
    assert read_run_replay_receipt(instance_dir, "bot-a", "run-1").status == "pending"

    write_run_replay_receipt(instance_dir, _receipt(status="parity"))
    assert read_run_replay_receipt(instance_dir, "bot-a", "run-1").status == "parity"


def test_read_run_replay_receipt_absent_is_none_and_foreign_identity_raises(tmp_path: Path) -> None:
    instance_dir = tmp_path / "live_state" / "bot-a"
    assert read_run_replay_receipt(instance_dir, "bot-a", "run-1") is None

    write_run_replay_receipt(instance_dir, _receipt(run_id="run-1"))
    with pytest.raises(ValueError):
        read_run_replay_receipt(instance_dir, "bot-OTHER", "run-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_receipt_store.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.run_replay'`.

- [ ] **Step 3: Implement**

Create `app/schemas/run_replay.py` with the three models exactly as specified in **Produces** (plain Pydantic v2, `from __future__ import annotations`, `ConfigDict(frozen=True, extra="forbid")` on each, `Field(ge=0)` on every count and ms field). Docstring each model in one line; `RunReplayReceipt`'s docstring: `"""Durable parity receipt for one completed run (Direction 2). All temporal fields are int64 ms UTC."""`.

Append to `app/services/run_replay_proof.py`:

```python
import os
import tempfile

from app.broker.alpaca.paths import safe_path_component
from app.schemas.run_replay import RunReplayReceipt


def _receipt_path(instance_dir: Path, run_id: str) -> Path:
    return instance_dir / RUN_REPLAY_RECEIPTS_DIRECTORY / f"{safe_path_component(run_id, 'run id')}.json"


def write_run_replay_receipt(instance_dir: Path, receipt: RunReplayReceipt) -> Path:
    """Atomically persist one run's replay receipt (replaceable: pending -> final)."""
    path = _receipt_path(instance_dir, receipt.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(receipt.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{receipt.run_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def read_run_replay_receipt(
    instance_dir: Path,
    strategy_instance_id: str,
    run_id: str,
) -> RunReplayReceipt | None:
    """Return one run's replay receipt, or an honest None when never generated."""
    path = _receipt_path(instance_dir, run_id)
    if not path.is_file():
        return None
    receipt = RunReplayReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    if receipt.strategy_instance_id != strategy_instance_id or receipt.run_id != run_id:
        raise ValueError("replay receipt belongs to another run identity")
    return receipt
```

Also add `Path` to the module's imports (`from pathlib import Path`) if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_receipt_store.py -x -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/schemas/run_replay.py app/services/run_replay_proof.py tests/services/test_run_replay_receipt_store.py
git commit -m "feat(replay-proof): RunReplayReceipt schema and durable per-run store"
```

---

### Task 8: Orchestrator — `RunReplayProofService.generate`

**Files:**
- Modify: `app/services/run_replay_proof.py`
- Test: `tests/services/test_run_replay_proof_service.py` (create)

**Interfaces:**
- Consumes: everything produced by Tasks 3–7; `SourceBarLedger`, `synthetic_account_id_for_strategy` + `paper_evidence_account_id_for_strategy` (`app.broker.alpaca.clerk.account_authority`), `get_alpaca_clerk` (`app.broker.alpaca.clerk`), `SqliteDecisionReceipts` (`app.broker.alpaca.clerk.sqlite.decision_receipts`), `BindingAuthority.runtime_for_projection` (`app/services/bot_binding_authority.py:54-56` base, :130-138 synthetic — `runtime.sqlite_repository` is the receipts repository), `BotRunRecord`, `BrokerBotBinding` (fields used: `strategy_instance_id, strategy_key, symbol, mode, use_rth, run_id, strategy_params, program_build, sealed_program`; re-verify `program_build.program_version` and `sealed_program.bot_configuration_hash` against `bot_binding_repository.py:621-641`).
- Produces:

```python
@dataclass
class RunReplayProofService:
    artifacts_root: Path
    instance_dir_for: Callable[[str], Path]
    binding_for: Callable[[str, str], BrokerBotBinding]          # (broker, sid) -> binding; raises typed runner errors
    run_record_for: Callable[[str, str], BotRunRecord | None]    # (sid, run_id)
    is_running: Callable[[str], bool]
    run_outcome_for: Callable[[str, str], BotRunOutcomeRecord | None] | None = None  # (sid, run_id); wall-clock end-bound fallback
    authority_for: Callable[[BrokerBotBinding], Any] | None = None   # BindingAuthority; None only in tests
    records_for_run: Callable[[BrokerBotBinding, str], Awaitable[LiveRunDecisionEvidence]] | None = None

    def read(self, strategy_instance_id: str, run_id: str) -> RunReplayReceipt | None: ...
    def write_pending(self, binding: BrokerBotBinding, run_id: str) -> None: ...
    async def generate(self, broker: str, strategy_instance_id: str, run_id: str) -> RunReplayReceipt: ...
```

  - Pure input-bounding helpers (PR #1751 finding 4), both module-level and unit-tested here:
    - `bounded_replay_bars(bars: Sequence[RetainedSourceBar], *, ledger_end_seq: int | None, terminal_recorded_at_ms: int | None) -> list[RetainedSourceBar]` — seq bound wins; wall-clock terminal bound is the fallback; **neither bound → `RunReplayUnavailableError`** (an unbounded replay is not evidence)
    - `refine_split_with_first_decision(warmup: list[RetainedSourceBar], live: list[RetainedSourceBar], *, first_decision_close_ms: int | None, decision_timeframe_ms: int | None) -> tuple[list[RetainedSourceBar], list[RetainedSourceBar]]` — decision-anchored warmup/live boundary (first recorded bucket's open); no-op passthrough when either input is `None`/`0`
  - `generate` semantics later tasks rely on: raises `RunReplayUnavailableError(http_status=409)` when `run_id` is the instance's current run AND `is_running(sid)`; raises `RunReplayUnavailableError(http_status=404)` when no `BotRunRecord` exists; replay input is bounded by the stored receipt's `ledger_end_seq` (snapshotted by `write_pending` at Stop) or, when absent, by the run's `BotRunOutcomeRecord.recorded_at_ms` — with neither, it refuses (409); on any *compute* failure it persists and returns a `status="replay_failed"` receipt (never raises for compute errors); otherwise persists and returns the final receipt with status derived as: `drift` if the engine leg **diverged** or `drift_count > 0`; else `indeterminate` if `records_truncated` or the engine leg was **unprovable** (`parity.error`) — known-incomplete evidence never yields a proof verdict (finding 6); else `parity_with_expected_live_effects` if `expected_live_effect_count > 0`; else `parity`.

- [ ] **Step 1: Write the failing test**

```python
"""Orchestrated receipt generation over injected evidence (no runner needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.services.bot_binding_repository import BotRunOutcomeRecord, BotRunRecord
from app.services.run_replay_proof import (
    LiveRunDecisionEvidence,
    RunReplayProofService,
    RunReplayUnavailableError,
)
from app.services.source_bar_ledger import SourceBarLedger
from tests.services.test_bot_runner import _SID, _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import _binding
from tests.services.test_run_replay_fidelity import _record_live_pass


def _run_record(started_at_ms: int) -> BotRunRecord:
    return BotRunRecord(
        run_id="run-1",
        strategy_instance_id=_SID,
        configuration_hash="0" * 64,
        launch_reason="deploy",
        started_at_ms=started_at_ms,
    )


def _outcome(recorded_at_ms: int) -> BotRunOutcomeRecord:
    return BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id="run-1",
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=recorded_at_ms,
    )


def _service(tmp_path: Path, evidence: LiveRunDecisionEvidence, *, running: bool = False,
             record: BotRunRecord | None = None,
             outcome: BotRunOutcomeRecord | None = None) -> RunReplayProofService:
    async def _records_for_run(binding, run_id: str) -> LiveRunDecisionEvidence:
        del binding, run_id
        return evidence

    return RunReplayProofService(
        artifacts_root=tmp_path / "artifacts",
        instance_dir_for=lambda sid: tmp_path / "live_state" / sid,
        binding_for=lambda broker, sid: _binding(run_id="run-1"),
        run_record_for=lambda sid, run_id: record,
        is_running=lambda sid: running,
        run_outcome_for=lambda sid, run_id: outcome,
        records_for_run=_records_for_run,
    )


@pytest.mark.asyncio
async def test_generate_produces_a_parity_receipt_for_a_faithful_run(tmp_path: Path) -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    evidence = LiveRunDecisionEvidence(
        records=tuple(records), crash_records=(), captured_decisions={}, truncated=False
    )
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    for bar in bars:
        ledger.append(bar)
    ledger.close()
    service = _service(
        tmp_path,
        evidence,
        record=_run_record(bars[0].start_ms - 1),
        outcome=_outcome(bars[-1].end_ms),  # wall-clock end bound: run ended after the last bar
    )

    receipt = await service.generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity"
    assert receipt.drift_count == 0
    assert receipt.retained_bar_count == len(bars)
    assert receipt.ledger_end_seq == len(bars)  # the applied bound is disclosed for stable regeneration
    assert receipt.digest_verified_count == len(records)
    assert receipt.engine_parity_trace_root is not None
    assert receipt.live_compared_count == len(records) > 0
    assert service.read(_SID, "run-1") == receipt  # durably persisted


@pytest.mark.asyncio
async def test_generate_refuses_the_currently_live_run(tmp_path: Path) -> None:
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    service = _service(tmp_path, evidence, running=True, record=_run_record(0))

    with pytest.raises(RunReplayUnavailableError) as excinfo:
        await service.generate("alpaca", _SID, "run-1")
    assert excinfo.value.http_status == 409


@pytest.mark.asyncio
async def test_generate_without_launch_evidence_is_a_404(tmp_path: Path) -> None:
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    service = _service(tmp_path, evidence, record=None)

    with pytest.raises(RunReplayUnavailableError) as excinfo:
        await service.generate("alpaca", _SID, "run-1")
    assert excinfo.value.http_status == 404


@pytest.mark.asyncio
async def test_generate_with_truncated_evidence_is_indeterminate_never_parity(tmp_path: Path) -> None:
    """PR #1751 finding 6: known-incomplete decision history must not prove parity."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    evidence = LiveRunDecisionEvidence(
        records=tuple(records), crash_records=(), captured_decisions={}, truncated=True
    )
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    for bar in bars:
        ledger.append(bar)
    ledger.close()
    service = _service(
        tmp_path, evidence,
        record=_run_record(bars[0].start_ms - 1),
        outcome=_outcome(bars[-1].end_ms),
    )

    receipt = await service.generate("alpaca", _SID, "run-1")

    assert receipt.status == "indeterminate"
    assert receipt.records_truncated is True


@pytest.mark.asyncio
async def test_generate_without_any_end_bound_refuses(tmp_path: Path) -> None:
    """PR #1751 finding 4: an unbounded replay input is not evidence."""
    bars = _ema_parity_bars_through_first_exit()
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    for bar in bars:
        ledger.append(bar)
    ledger.close()
    service = _service(tmp_path, evidence, record=_run_record(bars[0].start_ms - 1), outcome=None)

    with pytest.raises(RunReplayUnavailableError):
        await service.generate("alpaca", _SID, "run-1")
```

Note: `_record_live_pass` is imported from Task 6's test module — if the import feels awkward, move `_record_live_pass` into a shared helper `tests/_helpers/run_replay.py` in this step and import it in both test modules (do the move, don't copy).

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_proof_service.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'RunReplayProofService'`.

- [ ] **Step 3: Implement**

Append to `app/services/run_replay_proof.py` (new imports: `Awaitable`, `Callable` from `collections.abc`, `Any` from `typing`, `get_alpaca_clerk` from `app.broker.alpaca.clerk`, `SqliteDecisionReceipts`, `paper_evidence_account_id_for_strategy` + `synthetic_account_id_for_strategy` from `app.broker.alpaca.clerk.account_authority`, `BotRunRecord`/`BrokerBotBinding` from `app.services.bot_binding_repository`):

```python
_MAX_RECEIPT_DIVERGENCES = 50


def bounded_replay_bars(
    bars: Sequence[RetainedSourceBar],
    *,
    ledger_end_seq: int | None,
    terminal_recorded_at_ms: int | None,
) -> list[RetainedSourceBar]:
    """Bound one run's replay input at its durable end (PR #1751 finding 4).

    The ledger-sequence bound (snapshotted at Stop) wins; the run's terminal
    outcome instant is the wall-clock fallback for crashed/legacy runs. With
    neither, refuse: regenerating run N after run N+1 appended bars would
    otherwise change N's input, digest, and verdict.
    """
    if ledger_end_seq is not None:
        return [bar for bar in bars if bar.seq <= ledger_end_seq]
    if terminal_recorded_at_ms is not None:
        return [bar for bar in bars if bar.end_ms <= terminal_recorded_at_ms]
    raise RunReplayUnavailableError(
        "The run has no durable end boundary (no receipt snapshot and no terminal outcome).",
        detail="An unbounded replay input is not evidence; stop the run or repair its terminal record.",
    )


def refine_split_with_first_decision(
    warmup: list[RetainedSourceBar],
    live: list[RetainedSourceBar],
    *,
    first_decision_close_ms: int | None,
    decision_timeframe_ms: int | None,
) -> tuple[list[RetainedSourceBar], list[RetainedSourceBar]]:
    """Anchor the warmup/live boundary at the first recorded decision bucket.

    The wall-clock split can misclassify a bar that closed between launch and
    the warmup fetch; the run's own first decision receipt names its bucket
    exactly, so when both facts exist the boundary is that bucket's open
    (``bar_close_ms - decision_timeframe_ms``). Passthrough otherwise.
    """
    if not first_decision_close_ms or not decision_timeframe_ms:
        return warmup, live
    boundary_ms = first_decision_close_ms - decision_timeframe_ms
    merged = warmup + live
    return (
        [bar for bar in merged if bar.end_ms <= boundary_ms],
        [bar for bar in merged if bar.end_ms > boundary_ms],
    )


def _seal_decision_timeframe_ms(binding: BrokerBotBinding) -> int | None:
    """The seal-attested decision clock width, when this instance carries one.

    Attribute path mirrors ``bot_trade_strategy_warmup._warmup_lookback_days_for``
    (:77-79), which reads ``seal.configured_signal.clock.warmup_lookback_days``
    — re-verify ``decision_timeframe_ms`` sits on the same ``clock`` model
    before shipping; fall back to ``None`` (wall-clock split) when absent.
    """
    seal = binding.sealed_program
    if seal is None:
        return None
    return int(seal.configured_signal.clock.decision_timeframe_ms)


def ledger_account_id_for(binding: BrokerBotBinding) -> str:
    """Return the evidence namespace whose ledger retained this binding's bars."""
    if binding.mode == "dry_run":
        return synthetic_account_id_for_strategy(binding.strategy_instance_id)
    if binding.mode == "trade":
        return paper_evidence_account_id_for_strategy(binding.strategy_instance_id)
    raise RunReplayUnavailableError(
        f"Mode {binding.mode!r} retains no source-bar evidence; nothing to replay.",
        http_status=404,
    )


@dataclass
class RunReplayProofService:
    """Compute and persist one run's replay-parity receipt."""

    artifacts_root: Path
    instance_dir_for: Callable[[str], Path]
    binding_for: Callable[[str, str], BrokerBotBinding]
    run_record_for: Callable[[str, str], BotRunRecord | None]
    is_running: Callable[[str], bool]
    authority_for: Callable[[BrokerBotBinding], Any] | None = None
    records_for_run: Callable[[BrokerBotBinding, str], Awaitable[LiveRunDecisionEvidence]] | None = None

    def read(self, strategy_instance_id: str, run_id: str) -> RunReplayReceipt | None:
        return read_run_replay_receipt(
            self.instance_dir_for(strategy_instance_id), strategy_instance_id, run_id
        )

    def write_pending(self, binding: BrokerBotBinding, run_id: str) -> None:
        """Durably record that a receipt is owed before background compute starts.

        Snapshots the retained stream's terminal ``seq`` as the run's end
        bound (PR #1751 finding 4) — Stop time is the one moment "everything
        retained so far" and "everything this run observed" coincide. Bound
        resolution failures degrade to ``None`` (the terminal-outcome
        fallback still bounds generation); they must never fail Stop itself.
        """
        end_seq: int | None = None
        try:
            ledger = SourceBarLedger(
                artifacts_root=self.artifacts_root, account_id=ledger_account_id_for(binding)
            )
            try:
                provider = replay_provider_for(ledger, binding.symbol)
                stream = ledger.bars(provider=provider, symbol=binding.symbol)
                end_seq = stream[-1].seq if stream else None
            finally:
                ledger.close(checkpoint=False)
        except RunReplayUnavailableError as error:
            logger.warning(
                "Pending replay receipt written without a ledger end bound",
                extra={
                    "action": "run_replay_end_bound_unavailable",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": run_id,
                    "reason": str(error),
                },
            )
        write_run_replay_receipt(
            self.instance_dir_for(binding.strategy_instance_id),
            self._skeleton(binding, run_id, status="pending", ledger_end_seq=end_seq),
        )

    async def generate(self, broker: str, strategy_instance_id: str, run_id: str) -> RunReplayReceipt:
        binding = self.binding_for(broker, strategy_instance_id)
        if binding.run_id == run_id and self.is_running(strategy_instance_id):
            raise RunReplayUnavailableError(
                "The run is still live; stop it before generating its replay receipt.",
                detail="A live run's decision journal is still growing.",
            )
        run_record = self.run_record_for(strategy_instance_id, run_id)
        if run_record is None:
            raise RunReplayUnavailableError(
                f"Run {run_id!r} has no durable launch evidence.", http_status=404
            )
        instance_dir = self.instance_dir_for(strategy_instance_id)
        try:
            receipt = await self._compute(binding, run_record)
        except RunReplayUnavailableError:
            raise
        except Exception as error:  # compute failure becomes durable evidence, not silence
            logger.exception(
                "Run replay receipt computation failed",
                extra={
                    "action": "run_replay_receipt_failed",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": run_id,
                },
            )
            stored = self.read(strategy_instance_id, run_id)
            receipt = self._skeleton(
                binding,
                run_record.run_id,
                status="replay_failed",
                error=str(error),
                # Preserve the Stop-time end bound so a later regeneration
                # replays the same run-bounded input (PR #1751 finding 4).
                ledger_end_seq=None if stored is None else stored.ledger_end_seq,
            )
        write_run_replay_receipt(instance_dir, receipt)
        return receipt

    async def _compute(self, binding: BrokerBotBinding, run_record: BotRunRecord) -> RunReplayReceipt:
        ledger = SourceBarLedger(
            artifacts_root=self.artifacts_root, account_id=ledger_account_id_for(binding)
        )
        try:
            provider = replay_provider_for(ledger, binding.symbol)
            all_bars = ledger.bars(provider=provider, symbol=binding.symbol)
        finally:
            ledger.close(checkpoint=False)
        # Run-bounded input (PR #1751 finding 4): stored seq snapshot first,
        # terminal-outcome wall clock second, refuse when neither exists.
        stored = self.read(binding.strategy_instance_id, run_record.run_id)
        outcome = (
            None
            if self.run_outcome_for is None
            else self.run_outcome_for(binding.strategy_instance_id, run_record.run_id)
        )
        bars = bounded_replay_bars(
            all_bars,
            ledger_end_seq=None if stored is None else stored.ledger_end_seq,
            terminal_recorded_at_ms=None if outcome is None else outcome.recorded_at_ms,
        )
        if not bars:
            raise RunReplayUnavailableError(
                f"No retained source bars exist for {binding.symbol!r} within this run's bounds.",
                http_status=404,
            )
        evidence = await self._evidence(binding, run_record.run_id)
        warmup, live = split_warmup_and_live(bars, run_record.started_at_ms)
        warmup, live = refine_split_with_first_decision(
            warmup,
            live,
            first_decision_close_ms=(
                evidence.records[0].bar_close_ms if evidence.records else None
            ),
            decision_timeframe_ms=_seal_decision_timeframe_ms(binding),
        )
        decided = [bar for bar in bars if _includes_session_phase(bar, use_rth=binding.use_rth)]

        def _compute_sync() -> tuple[EngineParityResult, RunFidelityResult]:
            parity = engine_parity_over_bars(
                binding.strategy_key,
                binding.symbol,
                binding.strategy_params,
                [to_trade_bar(bar) for bar in decided],
            )
            fidelity = asyncio.run(
                run_fidelity_over_bars(
                    binding,
                    provider=provider,
                    warmup=warmup,
                    live=live,
                    records=evidence.records,
                    captured_decisions=evidence.captured_decisions,
                )
            )
            return parity, fidelity

        parity, fidelity = await asyncio.to_thread(_compute_sync)
        return self._final_receipt(binding, run_record, provider, bars, evidence, parity, fidelity)

    async def _evidence(self, binding: BrokerBotBinding, run_id: str) -> LiveRunDecisionEvidence:
        if self.records_for_run is not None:
            return await self.records_for_run(binding, run_id)
        rows = await self._receipt_rows(binding)
        return live_run_decision_evidence_from_rows(rows, run_id)

    async def _receipt_rows(self, binding: BrokerBotBinding) -> Sequence[DecisionReceiptResource]:
        if binding.mode == "dry_run":
            if self.authority_for is None:
                raise RunReplayUnavailableError(
                    "No authority selector is wired; Dry Run receipts are unreachable.",
                    http_status=503,
                )
            async with self.authority_for(binding).runtime_for_projection() as runtime:
                repository = None if runtime is None else runtime.sqlite_repository
                if repository is None:
                    raise RunReplayUnavailableError(
                        "The Dry Run synthetic authority could not be projected.",
                        http_status=503,
                    )
                return SqliteDecisionReceipts(
                    repository, strategy_instance_id=binding.strategy_instance_id
                ).retained_window()
        clerk = get_alpaca_clerk()
        repository = getattr(clerk, "repository", None)
        if repository is None:
            raise RunReplayUnavailableError(
                "The active SQLite Clerk is unavailable for replay evidence.",
                http_status=503,
            )
        return SqliteDecisionReceipts(
            repository, strategy_instance_id=binding.strategy_instance_id
        ).retained_window()

    def _skeleton(
        self,
        binding: BrokerBotBinding,
        run_id: str,
        *,
        status: str,
        error: str | None = None,
        ledger_end_seq: int | None = None,
    ) -> RunReplayReceipt:
        proof = binding.program_build
        seal = binding.sealed_program
        return RunReplayReceipt(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=run_id,
            strategy_key=binding.strategy_key,
            symbol=binding.symbol,
            provider="",
            status=status,
            bar_set_digest="",
            retained_bar_count=0,
            ledger_end_seq=ledger_end_seq,
            engine_parity_trace_root=None,
            engine_parity_compared_count=0,
            engine_parity_divergence=None,
            live_compared_count=0,
            match_count=0,
            expected_live_effect_count=0,
            drift_count=0,
            digest_verified_count=0,
            records_truncated=False,
            divergences=[],
            program_version=None if proof is None else proof.program_version,
            sealed_program_hash=None if seal is None else seal.bot_configuration_hash,
            generated_at_ms=now_ms_utc(),
            error=error,
        )

    def _final_receipt(
        self,
        binding: BrokerBotBinding,
        run_record: BotRunRecord,
        provider: str,
        bars: Sequence[RetainedSourceBar],
        evidence: LiveRunDecisionEvidence,
        parity: EngineParityResult,
        fidelity: RunFidelityResult,
    ) -> RunReplayReceipt:
        from app.schemas.run_replay import EngineParityDivergenceModel, RunReplayDivergenceModel

        crash_divergences = [
            RunFidelityDivergence(
                evaluation_id=record.evaluation_id,
                bar_close_ms=0,
                classification="expected_live_effect",
                reason_code="CANDIDATE_UNCAPTURED_AT_CRASH",
                replay_staged=None,
                live_outcome=record.outcome,
                detail=f"FR-016 crash-window evidence (bar_ref={record.bar_ref!r}).",
            )
            for record in evidence.crash_records
        ]
        all_divergences = crash_divergences + list(fidelity.divergences)
        expected = fidelity.expected_live_effect_count + len(crash_divergences)
        # Verdict ordering (PR #1751 finding 6): real drift is the loudest
        # verdict; known-incomplete evidence (truncated records) or an
        # unprovable engine leg (`parity.error`) is INDETERMINATE — partial
        # evidence never earns a proof verdict; only complete, clean evidence
        # may claim parity.
        if parity.divergence is not None or fidelity.drift_count > 0:
            status = "drift"
        elif evidence.truncated or parity.error is not None:
            status = "indeterminate"
        elif expected > 0:
            status = "parity_with_expected_live_effects"
        else:
            status = "parity"
        skeleton = self._skeleton(binding, run_record.run_id, status=status, error=parity.error)
        return skeleton.model_copy(
            update={
                "provider": provider,
                "bar_set_digest": bar_set_digest(bars),
                "retained_bar_count": len(bars),
                # Disclose the applied end bound so every regeneration — even
                # one that resolved the bound via the terminal outcome — is
                # seq-pinned from here on (stable under later appends).
                "ledger_end_seq": bars[-1].seq,
                "digest_verified_count": fidelity.digest_verified_count,
                "engine_parity_trace_root": parity.trace_root,
                "engine_parity_compared_count": parity.compared_count,
                "engine_parity_divergence": (
                    None
                    if parity.divergence is None
                    else EngineParityDivergenceModel(
                        index=parity.divergence.index,
                        evaluation_id=parity.divergence.evaluation_id,
                        field=parity.divergence.field,
                        expected=repr(parity.divergence.expected),
                        observed=repr(parity.divergence.observed),
                    )
                ),
                "live_compared_count": fidelity.compared_count,
                "match_count": fidelity.match_count,
                "expected_live_effect_count": expected,
                "drift_count": fidelity.drift_count,
                "records_truncated": evidence.truncated,
                "divergences": [
                    RunReplayDivergenceModel(
                        evaluation_id=d.evaluation_id,
                        bar_close_ms=d.bar_close_ms,
                        classification=d.classification,
                        reason_code=d.reason_code,
                        replay_staged=d.replay_staged,
                        live_outcome=d.live_outcome,
                        detail=d.detail,
                    )
                    for d in all_divergences[:_MAX_RECEIPT_DIVERGENCES]
                ],
            }
        )
```

Note: `RunReplayReceipt` is frozen — `model_copy(update=...)` is the sanctioned construction path for the final receipt (Pydantic v2 permits it on frozen models).

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_proof_service.py tests/services/test_run_replay_fidelity.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/run_replay_proof.py tests/services/test_run_replay_proof_service.py
# If Step 1 moved _record_live_pass into a shared helper, also:
#   git add tests/_helpers/run_replay.py tests/services/test_run_replay_fidelity.py
git commit -m "feat(replay-proof): RunReplayProofService orchestrates the two-leg receipt"
```

---

### Task 9: Terminal-path triggers + boot recovery — every terminal run gets a receipt

Coverage contract (PR #1751 finding 5 — this replaces the unqualified "every run" language): a run stopped through Stop gets its receipt scheduled at Stop; a run that ends via stream-end, feed death, or an in-process crash gets it scheduled from `_supervise`'s terminal branches; a run whose *process* died (pending receipt orphaned, or terminal outcome with no receipt at all) is caught by a boot-time scan hooked into `run_boot_recovery`. Only runs older than the instance's current-run pointer stay on-demand-only (POST).

**Files:**
- Modify: `app/services/bot_runner.py` (`BotTaskRegistry.__init__` at :186-300, `_stop_locked` at :836-945, `_supervise` at :1139-1199, `run_boot_recovery` at :990-1006)
- Test: `tests/services/test_run_replay_stop_trigger.py` (create)

**Interfaces:**
- Consumes: `RunReplayProofService`, `RunReplayUnavailableError` (Task 8), `supported_alpaca_paper_strategy_keys` (`bot_trade_strategy.py:610-637`), registry internals: `self._confined_instance_dir` (:1388), `self.binding_for_control` (:1098-1100 region — re-verify exact def), `self._bindings.read_run_record` / `.read_outcome` (:706, Task 3), `self._bindings.list_for_broker` (:1088-1096), `self._is_running` (referenced at :252), `self._authorities.for_binding` (:236-242), `_STOP_TIMEOUT_S` (:175).
- Produces (on `BotTaskRegistry`):
  - `self._replay_proof: RunReplayProofService` and `self._replay_receipt_tasks: set[asyncio.Task[None]]` (constructed in `__init__`)
  - `_schedule_run_replay_receipt(self, binding: BrokerBotBinding) -> None` — writes the `pending` receipt synchronously, then schedules background generation; skips (with an info log) bindings that are not trade/dry_run or whose strategy has no Signal Program
  - `_resume_pending_replay_receipts(self) -> None` — boot repair: for every alpaca binding whose current run is terminal (has a `BotRunOutcomeRecord`) and whose receipt is absent or still `pending`, re-schedule generation
  - `run_replay_receipt(self, broker: str, strategy_instance_id: str, run_id: str) -> RunReplayReceipt | None`
  - `async generate_run_replay_receipt(self, broker: str, strategy_instance_id: str, run_id: str) -> RunReplayReceipt`
  - `_stop_locked` calls `_schedule_run_replay_receipt(managed.binding)` after `replace_provisional_stop`; `_supervise` calls it in its three non-cancel terminal branches (feed death, crash, stream end); `run_boot_recovery` calls `_resume_pending_replay_receipts()` after the recovery sweep.

- [ ] **Step 1: Write the failing test**

```python
"""Stop schedules background replay-receipt generation (Direction 2)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path

import pytest

from app.services.bot_runner import BotTaskRegistry
from tests.services.test_bot_runner import _SID
from tests.services.test_candidate_uncaptured_at_crash import _binding


def _registry(tmp_path: Path) -> BotTaskRegistry:
    return BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)


def _method_calls(func, name: str) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        for node in ast.walk(tree)
    )


def test_stop_locked_schedules_the_replay_receipt() -> None:
    """AST pin, same idiom as test_signal_program_mode_parity's stream check:
    if Stop ever stops scheduling the receipt, this is the test that notices."""
    assert _method_calls(BotTaskRegistry._stop_locked, "_schedule_run_replay_receipt")


def test_supervise_schedules_the_replay_receipt_on_terminal_exits() -> None:
    """PR #1751 finding 5: stream-ended / feed-death / crashed runs owe a
    receipt too — not only operator Stops."""
    assert _method_calls(BotTaskRegistry._supervise, "_schedule_run_replay_receipt")


def test_run_boot_recovery_resumes_pending_replay_receipts() -> None:
    """PR #1751 finding 5: a process crash must not orphan `pending` evidence."""
    assert _method_calls(BotTaskRegistry.run_boot_recovery, "_resume_pending_replay_receipts")


@pytest.mark.asyncio
async def test_schedule_run_replay_receipt_writes_pending_then_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    calls: list[tuple[str, str, str]] = []

    async def _fake_generate(broker: str, sid: str, run_id: str):
        calls.append((broker, sid, run_id))

    monkeypatch.setattr(registry._replay_proof, "generate", _fake_generate)
    binding = _binding(run_id="run-1")  # trade-mode, ema_crossover_signal (a Signal Program)

    registry._schedule_run_replay_receipt(binding)

    pending = registry._replay_proof.read(_SID, "run-1")
    assert pending is not None and pending.status == "pending"
    assert registry._replay_receipt_tasks
    await asyncio.gather(*registry._replay_receipt_tasks)
    assert calls == [("alpaca", _SID, "run-1")]
    assert not registry._replay_receipt_tasks  # done-callback reaped it


@pytest.mark.asyncio
async def test_schedule_run_replay_receipt_skips_a_log_only_binding(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    binding = _binding(run_id="run-1").model_copy(update={"mode": "log_only"})

    registry._schedule_run_replay_receipt(binding)

    assert not registry._replay_receipt_tasks
    assert registry._replay_proof.read(_SID, "run-1") is None


@pytest.mark.asyncio
async def test_resume_pending_replay_receipts_schedules_terminal_runs_lacking_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot scan (PR #1751 finding 5): a terminal current run with no receipt —
    the crashed-process case — gets its generation scheduled at next boot."""
    from app.services.bot_binding_repository import BotRunOutcomeRecord

    registry = _registry(tmp_path)
    binding = _binding(run_id="run-1")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id="run-1",
        kind="CRASHED",
        reason_code="FEED_DEATH",
        recorded_at_ms=1_700_000_000_000,
    )
    monkeypatch.setattr(registry._bindings, "list_for_broker", lambda broker: [binding])
    monkeypatch.setattr(registry._bindings, "read_outcome", lambda sid, run_id: outcome)
    calls: list[tuple[str, str, str]] = []

    async def _fake_generate(broker: str, sid: str, run_id: str):
        calls.append((broker, sid, run_id))

    monkeypatch.setattr(registry._replay_proof, "generate", _fake_generate)

    registry._resume_pending_replay_receipts()

    await asyncio.gather(*registry._replay_receipt_tasks)
    assert calls == [("alpaca", _SID, "run-1")]


@pytest.mark.asyncio
async def test_resume_pending_replay_receipts_skips_runs_with_final_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.bot_binding_repository import BotRunOutcomeRecord
    from tests.services.test_run_replay_receipt_store import _receipt

    registry = _registry(tmp_path)
    binding = _binding(run_id="run-1")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id="run-1",
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=1_700_000_000_000,
    )
    monkeypatch.setattr(registry._bindings, "list_for_broker", lambda broker: [binding])
    monkeypatch.setattr(registry._bindings, "read_outcome", lambda sid, run_id: outcome)
    from app.services.run_replay_proof import write_run_replay_receipt

    final = _receipt(status="parity").model_copy(
        update={"strategy_instance_id": _SID, "strategy_key": binding.strategy_key}
    )
    write_run_replay_receipt(registry._replay_proof.instance_dir_for(_SID), final)

    registry._resume_pending_replay_receipts()

    assert not registry._replay_receipt_tasks  # nothing owed, nothing scheduled
```

Re-verify: `BrokerBotBinding` is a Pydantic model (frozen) — if `model_copy(update={"mode": ...})` trips a validator, construct the log-only binding explicitly with `mode="log_only"` instead. Also re-verify the exact `mode` literal for log-only bindings (grep `_run_log_only_bot` callers in `bot_runtime.py:137-138` — the else branch means any non-trade/dry_run mode string).

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_stop_trigger.py -x -q`
Expected: FAIL — `AttributeError: ... has no attribute '_schedule_run_replay_receipt'` (first test fails on `getsource` inspection of a call that isn't there; second on the missing attribute).

- [ ] **Step 3: Implement**

In `app/services/bot_runner.py`:

1. Imports: `from app.services.run_replay_proof import RunReplayProofService, RunReplayUnavailableError` and `from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys` (check whether `bot_trade_strategy` is already imported; add narrowly). `from app.schemas.run_replay import RunReplayReceipt` for the return annotations.
2. In `__init__`, after `self._resume_admission = ...` (re-verify around :266-280):

```python
        self._replay_proof = RunReplayProofService(
            artifacts_root=self._artifacts_root,
            instance_dir_for=self._confined_instance_dir,
            binding_for=self.binding_for_control,
            run_record_for=self._bindings.read_run_record,
            is_running=self._is_running,
            authority_for=self._authorities.for_binding,
        )
        self._replay_receipt_tasks: set[asyncio.Task[None]] = set()
```

3. New methods (place near `run_history`, :1106-1120):

```python
    def run_replay_receipt(
        self, broker: str, strategy_instance_id: str, run_id: str
    ) -> RunReplayReceipt | None:
        """Return the durable replay receipt for one run, or an honest None."""
        del broker  # the receipt file is instance-scoped; the router validated the segment
        return self._replay_proof.read(strategy_instance_id, run_id)

    async def generate_run_replay_receipt(
        self, broker: str, strategy_instance_id: str, run_id: str
    ) -> RunReplayReceipt:
        """Recompute one completed run's replay receipt on demand."""
        return await self._replay_proof.generate(broker, strategy_instance_id, run_id)

    def _schedule_run_replay_receipt(self, binding: BrokerBotBinding) -> None:
        """Direction 2: a stopping run owes a parity receipt. Never blocks Stop."""
        if binding.mode not in ("trade", "dry_run"):
            return
        if binding.strategy_key not in supported_alpaca_paper_strategy_keys():
            logger.info(
                "Run replay receipt skipped: no Signal Program",
                extra={
                    "action": "run_replay_receipt_skipped",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "strategy_key": binding.strategy_key,
                },
            )
            return
        self._replay_proof.write_pending(binding, binding.run_id)
        task = asyncio.get_running_loop().create_task(
            self._generate_replay_receipt_in_background(binding)
        )
        self._replay_receipt_tasks.add(task)
        task.add_done_callback(self._replay_receipt_tasks.discard)

    async def _generate_replay_receipt_in_background(self, binding: BrokerBotBinding) -> None:
        # When scheduled from a terminal branch of the run's own task
        # (_supervise), that task has not finished yet, so `is_running` would
        # briefly refuse generation. Wait for the supervised task to settle
        # first — bounded by the same timeout Stop uses for cancellation.
        managed = self._bots.get(binding.strategy_instance_id)
        if managed is not None and not managed.task.done():
            await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        try:
            await self._replay_proof.generate(
                binding.broker, binding.strategy_instance_id, binding.run_id
            )
        except RunReplayUnavailableError as error:
            logger.warning(
                "Run replay receipt unavailable",
                extra={
                    "action": "run_replay_receipt_unavailable",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "reason": str(error),
                },
            )

    def _resume_pending_replay_receipts(self) -> None:
        """Boot repair (Direction 2): re-schedule receipts a dead process owed.

        Covers two crash shapes: a `pending` receipt whose in-memory task died
        with the process, and a terminal run (crashed / stream-ended /
        service-shutdown) that never reached scheduling at all. Scope is each
        instance's *current* run — older runs stay on-demand via POST.
        Alpaca is the only in-container runner broker (IBKR bots are
        host-daemon-managed), so the sweep is alpaca-scoped like _supervise.
        """
        for binding in self._bindings.list_for_broker("alpaca"):
            if binding.mode not in ("trade", "dry_run"):
                continue
            if binding.strategy_key not in supported_alpaca_paper_strategy_keys():
                continue
            if self._is_running(binding.strategy_instance_id):
                continue
            try:
                receipt = self._replay_proof.read(binding.strategy_instance_id, binding.run_id)
                if receipt is not None and receipt.status != "pending":
                    continue
                outcome = self._bindings.read_outcome(binding.strategy_instance_id, binding.run_id)
            except (ValueError, OSError) as error:
                logger.warning(
                    "Boot replay-receipt scan skipped one instance",
                    extra={
                        "action": "run_replay_boot_scan_skipped",
                        "strategy_instance_id": binding.strategy_instance_id,
                        "run_id": binding.run_id,
                        "reason": str(error),
                    },
                )
                continue
            if outcome is None:
                continue  # not terminal; its own Stop/terminal path will schedule
            self._schedule_run_replay_receipt(binding)
```

(No blanket `except Exception` around `generate`: it already converts compute failures into a durable `replay_failed` receipt; anything else is a real bug that should surface through the event-loop exception handler. The boot scan's per-instance `(ValueError, OSError)` catch is the explicit corrupt-file skip — one bad instance must not block boot repair for the fleet.)

4. In `_stop_locked`, immediately after `self._terminal.replace_provisional_stop(...)` (:939-943) and before `await self._authority_for(managed.binding).release_if_unused()`:

```python
        self._schedule_run_replay_receipt(managed.binding)
```

5. In `_supervise` (:1139-1199), add the same one-line call in each of the three non-cancel terminal branches, immediately after their `finalize_*` call (PR #1751 finding 5 — stream-ended and crashed runs owe receipts too): after `finalize_crash(..., reason_code="FEED_DEATH")` in the `MarketDataFeedError` branch; after `finalize_crash(..., reason_code=type(exc).__name__)` in the generic `except Exception` branch; after `finalize_after_authority_stop(..., reason_code="BAR_STREAM_ENDED")` in the `else` branch. Do NOT add it to the `CancelledError` branch — `_stop_locked` already schedules for operator stops, and cancel-without-intent is finalized `EXITED_UNVERIFIED` whose boot scan / POST path covers it.

```python
            self._schedule_run_replay_receipt(binding)
```

6. In `run_boot_recovery` (:990-1006), after the recovery sweep completes (after `report = await self._boot_recovery.run(...)`, before the return — re-verify exact locals):

```python
        self._resume_pending_replay_receipts()
```

Caution: `_schedule_run_replay_receipt` runs while the ledger's writing run has just closed; the background `generate` opens its own read connection (WAL, 5 s busy timeout) — no coordination needed. `_stop_locked` must stay under the thermo threshold — this adds 1 line there; the ~110 new lines are new methods.

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_stop_trigger.py tests/services/test_bot_runner.py -q`
Expected: PASS. If any existing `test_bot_runner.py` stop test now schedules a real background generation, it will log `run_replay_receipt_unavailable`/write a `replay_failed` receipt harmlessly; if one *fails* from the background task outliving the loop, await drain in that test via `await asyncio.gather(*registry._replay_receipt_tasks, return_exceptions=True)` — fix in this task, do not skip.

- [ ] **Step 5: Commit**

```bash
git add app/services/bot_runner.py tests/services/test_run_replay_stop_trigger.py
git commit -m "feat(replay-proof): terminal-path receipt triggers plus boot-time pending recovery"
```

---

### Task 10: REST read/generate endpoints + OpenAPI contract regen

**Files:**
- Create: `app/routers/run_replay.py`
- Modify: `app/main.py` (router include, next to `broker_bots` at :615-618)
- Modify: `contracts/openapi/python-data-service.openapi.json` (regenerated, not hand-edited)
- Test: `tests/routers/test_run_replay.py` (create)

**Interfaces:**
- Consumes: `get_bot_task_registry`/`set_bot_task_registry` (`app.services.bot_runner`), registry facades from Task 9, `RunReplayReceipt`, `RunReplayUnavailableError`, `write_run_replay_receipt`; router helpers `_resolve_broker` / `_require_registry` / `_raise_runner_error` (`app/routers/broker_bots.py:41-73`); `PROTECTED_DATA_PLANE_READ_DEPENDENCIES` (in `app/main.py` — re-verify its import source there).
- Produces: `GET /api/brokers/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt` → 200 `RunReplayReceipt` | 404; `POST` same path → 200 final `RunReplayReceipt` (including `replay_failed`) | 404/409/503 from `RunReplayUnavailableError.http_status`. UI is out of scope (spec deliverable 4).

- [ ] **Step 1: Write the failing test**

```python
"""Endpoint tests for the per-run replay receipt (transport only)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.run_replay import router
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from tests.services.test_run_replay_receipt_store import _receipt  # the pending-receipt factory


class _FakeReadPort:
    broker_id = "alpaca"

    def capabilities(self) -> BrokerCapabilities:  # pragma: no cover - registry shape only
        raise NotImplementedError


@pytest.fixture
def api(tmp_path: Path):
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)
    set_bot_task_registry(registry)
    app = FastAPI()
    app.include_router(router)
    yield app, registry
    set_bot_task_registry(None)
    reset_broker_registry_for_testing()


@pytest.mark.asyncio
async def test_get_replay_receipt_absent_is_404(api) -> None:
    app, _registry = api
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/brokers/alpaca/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_replay_receipt_returns_the_persisted_receipt(api) -> None:
    app, registry = api
    receipt = _receipt(status="parity")
    from app.services.run_replay_proof import write_run_replay_receipt

    write_run_replay_receipt(
        registry._replay_proof.instance_dir_for("bot-a"), receipt
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/brokers/alpaca/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "parity"
    assert body["run_id"] == "run-1"
    assert body["generated_at_ms"] == 1_700_000_000_000  # int64 ms UTC on the wire


@pytest.mark.asyncio
async def test_get_replay_receipt_unknown_broker_is_404(api) -> None:
    app, _registry = api
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/brokers/ibkr/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_replay_receipt_without_launch_evidence_is_404(api) -> None:
    app, _registry = api
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/api/brokers/alpaca/bots/bot-a/runs/run-1/replay-receipt")
    assert response.status_code == 404
```

Note: the POST test drives the real registry — `binding_for_control` raises a typed runner error for the unknown instance (→ 404 via `_raise_runner_error`), which also covers the error-translation path.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/routers/test_run_replay.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routers.run_replay'`.

- [ ] **Step 3: Implement the router**

Create `app/routers/run_replay.py`:

```python
"""Per-run replay-parity receipts (transport only).

``/api/brokers/{broker}/bots/{sid}/runs/{run_id}/replay-receipt`` — Direction 2
(docs/audits/strategy-execution-research-directions-2026-08-24.md). GET reads
the durable receipt; POST regenerates it for a completed run. All business
logic lives in ``app.services.run_replay_proof`` (router-freeze discipline).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.routers.broker_bots import _raise_runner_error, _require_registry, _resolve_broker
from app.schemas.run_replay import RunReplayReceipt
from app.services.bot_runner import BotRunnerError
from app.services.run_replay_proof import RunReplayUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["run-replay"])


@router.get(
    "/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt",
    response_model=RunReplayReceipt,
    summary="Read one run's durable replay-parity receipt",
)
async def read_run_replay_receipt(
    broker: str, strategy_instance_id: str, run_id: str
) -> RunReplayReceipt:
    _resolve_broker(broker)
    registry = _require_registry()
    receipt = registry.run_replay_receipt(broker, strategy_instance_id, run_id)
    if receipt is None:
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' has no replay receipt; POST this path to generate one.",
        )
    return receipt


@router.post(
    "/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt",
    response_model=RunReplayReceipt,
    summary="Recompute one completed run's replay-parity receipt from its retained bars",
)
async def generate_run_replay_receipt(
    broker: str, strategy_instance_id: str, run_id: str
) -> RunReplayReceipt:
    _resolve_broker(broker)
    registry = _require_registry()
    try:
        return await registry.generate_run_replay_receipt(broker, strategy_instance_id, run_id)
    except RunReplayUnavailableError as error:
        raise HTTPException(
            status_code=error.http_status,
            detail={"message": str(error), "why": error.detail},
        ) from error
    except BotRunnerError as error:
        _raise_runner_error(error)
```

In `app/main.py`, import `run_replay` alongside the `broker_bots` import (re-verify the import block near the top) and include it directly after the `broker_bots` include (:615-618):

```python
# Per-run replay-parity receipts (Direction 2). Reads + recompute over live
# broker evidence — always-on data-plane control secret, like broker_bots.
app.include_router(
    run_replay.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/routers/test_run_replay.py -x -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Regenerate the committed OpenAPI contract (CI gate)**

Run: `python scripts/export_openapi_contract.py && python scripts/export_openapi_contract.py --check`
Expected: the export rewrites `contracts/openapi/python-data-service.openapi.json`; `--check` then exits 0.

- [ ] **Step 6: Commit**

```bash
git add app/routers/run_replay.py app/main.py tests/routers/test_run_replay.py ../contracts/openapi/python-data-service.openapi.json
git commit -m "feat(replay-proof): REST read/generate endpoints for run replay receipts + OpenAPI regen"
```

---

### Task 11: End-to-end proof — retained run → stop-shaped generation → classified receipt

**Files:**
- Test: `tests/services/test_run_replay_receipt_end_to_end.py` (create; `tests/services/` deliberately, not `tests/integration/` — the latter is absent from the CI fast baseline per the Direction-4 audit)

**Interfaces:**
- Consumes: everything shipped in Tasks 1–8 plus the *real* receipts path: `ClerkSqliteRepository.initialize`, `SqliteDecisionReceipts.append` (facts must mirror `bot_trade_strategy._append_decision_receipt`: `run_id`, `evaluation_id`, `reason_code`, `bar_ref`, with `intent_id=evaluation_id`), `live_run_decision_evidence_from_rows` — this is the test that proves the production facts round-trip, not just injected dataclasses.
- Produces: nothing (pure verification of the whole pipeline over real EMA bars).

- [ ] **Step 1: Write the test (it should pass immediately if Tasks 1–8 are correct — treat any failure as a real integration bug, per superpowers:systematic-debugging)**

```python
"""End-to-end: retained bars + real decision receipts -> classified parity receipt."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.engine.strategy.signal_program import Settlement, trace_root
from app.services.bot_binding_repository import BotRunOutcomeRecord, BotRunRecord
from app.services.bot_trade_strategy import strategy_evaluations
from app.services.run_replay_proof import (
    RunReplayProofService,
    live_run_decision_evidence_from_rows,
)
from app.services.source_bar_ledger import SourceBarLedger
from tests.services.test_bot_runner import _SID, _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import _binding, _PhaseFeed


async def _run_and_receipt_live_pass(receipts: SqliteDecisionReceipts, *, block_first_enter: bool) -> None:
    """Drive the shared seam exactly as run_trade_bot would and durably receipt it,
    including the Task 5b live-time trace-digest capture."""
    binding = _binding(run_id="run-1")
    blocked_once = False
    async for evaluation in strategy_evaluations(binding, _PhaseFeed(live_bars=_ema_parity_bars_through_first_exit())):
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        facts = {
            "bar_ref": f"decision-bar:fake-phase:SPY:{evaluation.decision_bar_close_ms}",
            "decision_id": evaluation.evaluation_id,
            "evaluation_id": evaluation.evaluation_id,
            "run_id": "run-1",
            "decision_bar_close_ms": evaluation.decision_bar_close_ms,
        }
        if evaluation.trace is not None:
            facts["trace_digest"] = trace_root([evaluation.trace])
        if staged is None:
            receipts.append(outcome="no_action", symbol="SPY", observed_at_ms=evaluation.decision_bar_close_ms,
                            facts={**facts, "reason_code": "NO_ACTION"}, intent_id=evaluation.evaluation_id)
            evaluation.settle_stage(Settlement.COMMIT)
        elif block_first_enter and staged == "ENTER" and not blocked_once:
            blocked_once = True
            receipts.append(outcome="blocked", symbol="SPY", observed_at_ms=evaluation.decision_bar_close_ms,
                            facts={**facts, "reason_code": "MARKET_CLOSED"}, intent_id=evaluation.evaluation_id)
            evaluation.settle_stage(Settlement.DISCARD)
        else:
            outcome = "enter_intent" if staged == "ENTER" else "exit_intent"
            receipts.append(outcome=outcome, symbol="SPY", observed_at_ms=evaluation.decision_bar_close_ms,
                            facts={**facts, "reason_code": outcome}, intent_id=evaluation.evaluation_id)
            evaluation.settle_stage(Settlement.COMMIT)


def _service(tmp_path: Path, receipts: SqliteDecisionReceipts) -> RunReplayProofService:
    async def _records_for_run(binding, run_id: str):
        return live_run_decision_evidence_from_rows(receipts.retained_window(), run_id)

    bars = _ema_parity_bars_through_first_exit()
    return RunReplayProofService(
        artifacts_root=tmp_path / "artifacts",
        instance_dir_for=lambda sid: tmp_path / "live_state" / sid,
        binding_for=lambda broker, sid: _binding(run_id="run-1"),
        run_record_for=lambda sid, run_id: BotRunRecord(
            run_id="run-1", strategy_instance_id=_SID, configuration_hash="0" * 64,
            launch_reason="deploy", started_at_ms=bars[0].start_ms - 1,
        ),
        is_running=lambda sid: False,
        # Wall-clock end bound: the run terminated right after its last bar.
        run_outcome_for=lambda sid, run_id: BotRunOutcomeRecord(
            strategy_instance_id=_SID, run_id="run-1", kind="STOPPED",
            reason_code="OPERATOR_STOP", recorded_at_ms=bars[-1].end_ms,
        ),
        records_for_run=_records_for_run,
    )


@pytest.fixture
def receipts(tmp_path: Path) -> SqliteDecisionReceipts:
    repo = ClerkSqliteRepository.initialize(account_id="PA-E2E", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    return SqliteDecisionReceipts(repo, strategy_instance_id=_SID)


def _retain_all(tmp_path: Path) -> None:
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        for bar in _ema_parity_bars_through_first_exit():
            ledger.append(bar)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_end_to_end_faithful_run_yields_full_parity(tmp_path: Path, receipts: SqliteDecisionReceipts) -> None:
    _retain_all(tmp_path)
    await _run_and_receipt_live_pass(receipts, block_first_enter=False)

    receipt = await _service(tmp_path, receipts).generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity"
    assert receipt.drift_count == 0
    assert receipt.expected_live_effect_count == 0
    assert receipt.live_compared_count > 0
    assert receipt.digest_verified_count == receipt.live_compared_count  # content-verified end to end
    assert receipt.engine_parity_trace_root is not None
    assert receipt.bar_set_digest != ""


@pytest.mark.asyncio
async def test_end_to_end_blocked_enter_is_classified_not_reported_as_drift(
    tmp_path: Path, receipts: SqliteDecisionReceipts
) -> None:
    _retain_all(tmp_path)
    await _run_and_receipt_live_pass(receipts, block_first_enter=True)

    receipt = await _service(tmp_path, receipts).generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity_with_expected_live_effects"
    assert receipt.drift_count == 0
    assert receipt.expected_live_effect_count >= 1
    assert any(
        d.classification == "expected_live_effect" and d.reason_code == "MARKET_CLOSED"
        for d in receipt.divergences
    )


@pytest.mark.asyncio
async def test_end_to_end_regenerating_run_one_after_later_appends_is_stable(
    tmp_path: Path, receipts: SqliteDecisionReceipts
) -> None:
    """PR #1751 finding 4: run N's receipt must not change when run N+1 has
    appended more bars to the same instance-scoped ledger."""
    _retain_all(tmp_path)
    await _run_and_receipt_live_pass(receipts, block_first_enter=False)
    service = _service(tmp_path, receipts)

    first = await service.generate("alpaca", _SID, "run-1")

    # Simulate run N+1: append later bars beyond run-1's terminal instant.
    bars = _ema_parity_bars_through_first_exit()
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        last = bars[-1]
        for offset in range(1, 4):
            ledger.append(
                last.model_copy(
                    update={
                        "start_ms": last.start_ms + offset * 60_000,
                        "end_ms": last.end_ms + offset * 60_000,
                        "fetched_at_ms": last.fetched_at_ms + offset * 60_000,
                    }
                )
            )
    finally:
        ledger.close()

    second = await service.generate("alpaca", _SID, "run-1")

    assert second.bar_set_digest == first.bar_set_digest
    assert second.retained_bar_count == first.retained_bar_count
    assert second.ledger_end_seq == first.ledger_end_seq
    assert second.status == first.status == "parity"
    assert second.engine_parity_trace_root == first.engine_parity_trace_root
```

Re-verify `SqliteDecisionReceipts.append`'s exact kwargs against `decision_receipts.py:519-538` (as read: `outcome, symbol, observed_at_ms, facts, intent_id, order_ref`) and `ClerkSqliteRepository.initialize` / `register_strategy_instance` against `tests/services/test_candidate_uncaptured_at_crash.py:130-135` before running.

- [ ] **Step 2: Run the tests**

Run: `DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/services/test_run_replay_receipt_end_to_end.py -x -q`
Expected: PASS (3 passed). If the parity case reports drift, debug with superpowers:systematic-debugging — the most likely culprits, in order: the `facts` shape diverging from what `live_run_decision_evidence_from_rows` parses; the run-boundary split putting bar 0 into warmup; a session-close flush bucket receipted by the recorder but not yielded on replay (or vice versa) — compare `evaluation_id` sequences before touching tolerances or classifications. If the stability case fails on `ledger_end_seq`, check that the first generation disclosed the applied bound (`bars[-1].seq`) and the second generation read it back from the stored receipt before consulting the terminal outcome.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_run_replay_receipt_end_to_end.py
git commit -m "test(replay-proof): end-to-end retained-run -> classified receipt proof"
```

---

### Task 12: Documentation + project-scope verification

**Files:**
- Create: `docs/references/run-replay-proof.md`
- Modify: `docs/architecture/adrs/0043-signal-program-build-proof-and-legacy-seal-migration.md` (one cross-reference line under §5 "Consequences"-adjacent text — an addition, not a rewrite of an Accepted ADR)
- Modify: `docs/architecture/engine-authority-map.md` (one new row — AGENTS.md "Engine and math authority" requires it in the same PR as any change introducing an engine path)
- Modify: `docs/math-sources-of-truth.md` — **no new row** (see Step 2b for the explicit rationale the AGENTS.md rule demands; only touch this file if the reviewer of the map row disagrees that no new math concept exists)

**Interfaces:** none produced; consumes the shipped feature's real names.

- [ ] **Step 1: Write `docs/references/run-replay-proof.md`**

```markdown
# Run-scoped replay proof (Direction 2)

**Spec:** `docs/audits/strategy-execution-research-directions-2026-08-24.md` (Direction 2)
**Shipped:** <PR number> · **Status:** active for every Paper (`trade`) and Dry Run stop

## What it is

Every paper/dry run retains its exact source observations in an
instance-scoped `SourceBarLedger` (`paper:<sid>` / `sim:<sid>` under
`accounts/alpaca/`) and produces a durable `RunReplayReceipt` at
`live_state/<sid>/run_replay_receipts/<run_id>.json` with two proofs:

1. **Engine parity** — `run_shadow_trace_evaluation`
   (`app/broker/alpaca/clerk/sqlite/qualification_shadow_trace.py`) over the
   run's retained stream, **bounded at the run's durable end** (the
   `ledger_end_seq` snapshot taken at Stop, or the terminal outcome's
   `recorded_at_ms` for crashed/legacy runs): BacktestEngine vs the shared
   runner seam, all-COMMIT, fails on the first divergent trace field.
2. **Run fidelity** — a disposition-faithful replay through the production
   `strategy_evaluations` generator, aligned to the run's decision receipts
   by deterministic `evaluation_id` and verified **content-by-content**
   against each receipt's live-captured `trace_digest`
   (`trace_root([trace])`), settling each stage with the live-recorded
   disposition so a legitimately-refused intent never cascades into false
   drift. `digest_verified_count` discloses coverage; digest-less rows
   (recorded before capture existed) fall back to intent-kind comparison —
   the receipt's stated residual blind spot.

## When a receipt is generated (coverage contract)

- **Operator Stop** — scheduled at Stop (`pending` written synchronously,
  compute in the background).
- **Stream end / feed death / in-process crash** — scheduled from the
  supervisor's terminal branches.
- **Process death** — the boot-recovery sweep re-schedules orphaned
  `pending` receipts and terminal current runs that never got one.
- **Older historical runs** — on demand only, via
  `POST /api/brokers/{broker}/bots/{sid}/runs/{run_id}/replay-receipt`.

## Divergence classification (deliverable 3)

| classification | reason codes | meaning |
|---|---|---|
| `expected_live_effect` | a `blocked` row whose reason is in the CLOSED set `EXPECTED_LIVE_GATE_REASON_CODES` (liveness gate, `PAUSED_OBSERVE_ONLY`, stream-health hold, Clerk pre-custody refusal) AND whose trace digest matched the replay; `CANDIDATE_UNCAPTURED_AT_CRASH` | live-only gates the mode-parity seam documents (`tests/engine/strategy/test_signal_program_mode_parity.py`); math agreed, custody legitimately refused — a `blocked` row is cross-checked, never trusted on presence |
| `drift` | `TRACE_DIGEST_MISMATCH`, `DECISION_MISMATCH`, `UNRECOGNIZED_BLOCK_REASON`, `MISSING_LIVE_RECORD`, `EVALUATION_ID_MISMATCH`, `UNMATCHED_LIVE_RECORD` | replayed math and durable live record disagree with no enumerating live effect — a real bug, treat like a failed reconciliation |

Statuses: `pending` → `parity` | `parity_with_expected_live_effects` |
`indeterminate` | `drift` | `replay_failed`. Verdict ordering: real drift is
the loudest; known-incomplete evidence (`records_truncated`) or an
unprovable engine leg yields `indeterminate` — partial evidence never earns
a proof verdict.

## Known bounds (documented, not hidden)

- **Receipt-retention truncation:** a run longer than
  `MAX_DECISION_RECEIPTS_PER_STRATEGY` decisions sets `records_truncated`
  and forces the verdict to `indeterminate` — never `parity`. The retention
  floor test pins that a normal daily run never hits this.
- **Digest coverage:** rows recorded before live-time trace-digest capture
  existed verify at intent level only; `digest_verified_count` vs
  `live_compared_count` discloses exactly how much of the run was
  content-verified.
- **Ledger capacity:** `SOURCE_BAR_STREAM_CAPACITY` (200k bars/stream) fails
  closed per #1740; a months-old instance needs a reviewed rollover before
  its next run can retain.
- **No admission coupling:** receipts are evidence out, never a gate in — the
  Paper evidence-only override is permanent by operator decision (spec §
  "Standing constraint").
```

(Replace `<PR number>` at PR time.)

- [ ] **Step 2: Add the ADR cross-reference**

In ADR 0043, at the end of §5 ("Decision receipt and custody effect are captured in one SQLite transaction, keyed by `decision_id = evaluation_id`"), append one paragraph:

```markdown
Addendum (2026-08-24, Direction 2): receipts record what was decided, never
the bars — the bars themselves are retained separately per instance by the
`SourceBarLedger`, and `docs/references/run-replay-proof.md` describes the
per-run replay receipt that joins the two after every run.
```

- [ ] **Step 2b: Update the engine/math authority registries (AGENTS.md gate — PR #1751 finding 1)**

AGENTS.md ("Engine and math authority") requires **both** registries updated in the same PR as any change that introduces an engine path. This plan introduces one (the replay/parity receipt path), so:

1. `docs/architecture/engine-authority-map.md` — append this row to "The map" table (match the existing five-column format `| Job | Owning engine (canonical) | Path / entry point | Role | Status |`) and add this PR to the "Last reviewed" line:

```markdown
| Run-scoped replay proof (per-run parity receipt for Paper/Dry Run) | `BacktestEngine` (reference leg, via `qualification_shadow_trace.run_shadow_trace_evaluation`) + the shared runner seam `strategy_evaluations` (fidelity leg) — no third engine is introduced | `app/services/run_replay_proof.py` (`RunReplayProofService`) | Evidence generation only: replays a completed run's retained bars through the two existing engines and classifies divergence against the run's decision receipts. Never a decision, admission, or promotion gate. | Active |
```

2. `docs/math-sources-of-truth.md` — **no new row, stated explicitly here per the rule:** this plan introduces no new math concept. Both proof legs re-execute *existing* canonical implementations (`BacktestEngine`, `strategy_evaluations`, the sealed programs' own math); the only "computation" added is content hashing, which reuses the already-canonical `trace_root` / `_semantic_hash` (`app/engine/strategy/signal_program.py:359-371`) and a `sha256` over canonical JSON for `bar_set_digest` — hashing for identity, not a numerical concept with a reference or tolerance. If a reviewer disagrees, the row to add would name `bar_set_digest` with canonical file `app/services/run_replay_proof.py` and its validating test `tests/services/test_run_replay_proof_assembly.py::test_bar_set_digest_changes_when_a_payload_changes` — but the default position of this plan is that no registry entry is owed, and the engine-authority-map row above is the required update.

- [ ] **Step 3: Full-suite verification (pre-push gate — PR #1751 finding 2)**

Run, in order:

```bash
ruff check app/ tests/
DATA_PLANE_CONTROL_SECRET="" python -m pytest tests/ -q
python scripts/export_openapi_contract.py --check
```

The pytest run is the **full suite, deliberately without `-k "not slow"`**: per `.claude/rules/testing.md` the full per-stack suite is the ONE pre-push gate, and the `-k "not slow"` filter (from `.claude/CLAUDE.md`'s container quick-loop) would exclude the slow-marked LEAN↔spec parity surface — exactly the surface this PR's replay proof touches. Budget the extra minutes; do not substitute the fast loop here.

Expected: ruff clean; pytest — zero failures beyond the pre-existing baseline (establish the baseline on the base branch **with the same full-suite command** per `.claude/rules/testing.md` before attributing any failure; surface pre-existing failures in the PR description); contract check exits 0. Fix anything new before proceeding.

- [ ] **Step 4: Commit**

```bash
git add docs/references/run-replay-proof.md docs/architecture/adrs/0043-signal-program-build-proof-and-legacy-seal-migration.md docs/architecture/engine-authority-map.md
git commit -m "docs(replay-proof): reference note, ADR 0043 cross-link, engine-authority-map entry"
```

- [ ] **Step 5: Before opening the PR**

Invoke the `thermo-nuclear-code-quality-review` skill (one-shot gate, per CLAUDE.md) and address every major finding in-branch. Likely watch-items: `run_replay_proof.py` size (keep under ~700 lines — if the classifier grew, extract `run_replay_fidelity.py` with the classifier + feed and keep `run_replay_proof.py` as assembly/orchestration), and `bot_runner.py` net growth (~60 lines; if flagged, move the two background-task methods into `run_replay_proof.py` as a small `ReplayReceiptScheduler`).

---

## Spec coverage self-check (Direction 2 research questions → tasks)

| Spec item | Where |
|---|---|
| RQ1: wire `SourceBarLedger` into `run_trade_bot`; retention/size economics | Tasks 1–2 (wiring), design decision (a) (economics: existing 200k fail-closed capacity, #1740 rollover, retention-floor test) |
| RQ2: per-run parity receipt on Stop/on demand via `run_shadow_trace_evaluation`, receipt in evidence dir alongside `run_build_evidence/` | Tasks 4 (engine leg), 7 (schema/store at `run_replay_receipts/`), 8 (orchestrator), 9 (Stop trigger), 10 (on demand) |
| RQ3: classify divergence (liveness gate on ENTER, Clerk rejections — the mode-parity residual gaps) instead of a bare boolean | Tasks 5, 5b, 6 (content-level `trace_digest` capture + digest-by-digest comparison; `blocked` rows cross-checked against the closed `EXPECTED_LIVE_GATE_REASON_CODES` set, never trusted on presence; crash windows; vs `drift`), design decisions (d)/(d2) |
| RQ4: where does the receipt surface | Task 10 (REST read path; UI explicitly out of scope per plan brief). Promotion-evidence wiring (Direction 3) is **deferred** — it belongs to Direction 3's chain-of-custody design, which will consume `bar_set_digest` + `engine_parity_trace_root` from this receipt |
| "Done when": deterministic replay from own retained bars, mechanical trace-root comparison, durable evidence attached to the run | Task 11 end-to-end test is the executable form of the done-when, including the regenerate-after-later-appends stability property (run-bounded input) |
| EOD trigger (spec: "on Stop/EOD") | Task 9: Stop schedules at Stop (the daily lifecycle stops bots at the bell); `_supervise`'s terminal branches schedule for `BAR_STREAM_ENDED`, feed-death, and crash exits; the `run_boot_recovery` scan covers process-death orphans (pending receipts and terminal runs lacking one). Coverage contract stated at the top of Task 9 and in `docs/references/run-replay-proof.md` |
| PR #1751 review findings 1–6 | 1: Task 12 Step 2b (engine-authority-map row + explicit no-new-math statement). 2: Task 12 Step 3 (full suite, no slow-exclusion, with rationale). 3: design (d2), Tasks 5/5b/6 (digest capture + closed gate set + coverage disclosure). 4: design (c), Tasks 7–8, 11 (`ledger_end_seq` snapshot, outcome fallback, refuse-unbounded, decision-anchored split, stability test). 5: Task 9 (terminal-branch triggers + boot scan + narrowed coverage contract). 6: Tasks 7–8 (`indeterminate` status, truncated-forces-indeterminate test) |

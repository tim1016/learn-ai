> **Status:** Archived evidence (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** This feedback is tied to the superseded paper-deployment plan.

## 1. Architecture

**Recommendation:** Build the Python `LiveEngine` as the stage-2 runtime, but treat LEAN live deploy as an optional shadow/reference run, not as the receipt target.
**Alternatives considered:** Running `lean live deploy` is faster to "a paper trade happened," but it proves LEAN's runtime, not this repo's live runtime; the useful hybrid is to keep LEAN as an external comparator while requiring the Python replay gate before any broker paper run.
**Evidence:** The current root philosophy says references are studied and eliminated as runtime dependencies (`CLAUDE.md:7-11`), and the math registry names `PythonDataService/app/engine/` as canonical for bar replay and fill models while listing LEAN as a vendored reference (`docs/math-sources-of-truth.md:44`, `docs/math-sources-of-truth.md:97`).
`BacktestEngine.run` already owns the strategy lifecycle, consolidator feed, deferred fills, order callbacks, insight scoring, and equity curve (`PythonDataService/app/engine/engine.py:84-418`), so a live receipt should validate that lifecycle under live input rather than bypass it.
The vendored LEAN tree I found is an extract under `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators`, which is enough for audit but not an in-repo live deployment surface.

## 2. Client library

**Recommendation:** Use the repo's existing `ib_async>=2.0,<3.0` dependency and broker wrapper, with a protocol/fake-client boundary for tests; do not add raw `ibapi` or a second IBKR abstraction.
**Alternatives considered:** Raw `ibapi` has the smallest third-party wrapper surface but would force callback/thread plumbing into an asyncio/FastAPI codebase; adding another wrapper would increase dependency surface without improving the parity gate.
**Evidence:** The Python rules require `async def` for I/O and warn not to mix `asyncio.run` into an existing event loop (`.claude/rules/python.md:14`, `.claude/rules/python.md:69-74`).
`ib_async` is already in `PythonDataService/requirements-light.txt` with a tight 2.x bound and broker-specific justification (`PythonDataService/requirements-light.txt:23-28`), so the plan's `ib_async>=1.0,<2.0` line is stale (`docs/ibkr-paper-deployment-plan.md:110`, `docs/ibkr-paper-deployment-plan.md:417`).
The existing `IbkrClient` already wraps `ib_async.IB`, defers imports for non-broker environments, exposes curated in-package access, and has test replacement hooks (`PythonDataService/app/broker/ibkr/client.py:123-139`, `PythonDataService/app/broker/ibkr/client.py:247-310`).

## 3. Scope cap

**Recommendation:** Keep v1 SPY-only and single-symbol so the live receipt matches the current backtest engine's actual execution domain.
**Alternatives considered:** Lifting to multi-symbol may look like a transport-only change, but the current engine explicitly does not merge multi-symbol minute streams by time, so live multi-symbol would validate a new scheduling model at the same time as broker behavior.
**Evidence:** `BacktestEngine.run` raises `NotImplementedError("Phase 1 engine supports a single symbol only")` once `ctx.symbols` has more than one symbol (`PythonDataService/app/engine/engine.py:178-184`).
The SPY strategy is parameterized but defaults to SPY specifically to keep LEAN bit-exact parity unchanged (`PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:70-77`), and the plan's assumption 3 is already aligned with that (`docs/ibkr-paper-deployment-plan.md:27`).

## 4. Fill-model parity target

**Recommendation:** Use `FillMode.NEXT_BAR_OPEN` for the fake-client replay gate, but label real IBKR paper fills as a reconciliation against that baseline, not as strict fill-price parity.
**Alternatives considered:** A new synthetic `FillMode.LATENCY_AWARE` would describe live timing more honestly, but it would create an unvalidated math/execution path before the stage-2 receipt; keep latency as a measured reconciliation field first.
**Evidence:** `NEXT_BAR_OPEN` fills at the open and start time of the minute bar after the signal bar (`PythonDataService/app/engine/execution/fill_model.py:77-81`), while live IBKR fills arrive via broker order/fill events and the plan itself notes there is no deferred-fill list in live (`docs/ibkr-paper-deployment-plan.md:256`).
The residual gap is therefore concrete: the signal is emitted after a 15-minute bar closes, but the broker market order can fill on the next print after submission rather than the next 1-minute bar's open.
`OrderEvent` already carries actual fill time, price, quantity, and direction (`PythonDataService/app/engine/execution/order.py:59-70`), so the better receipt is deterministic replay exactness plus live reconciliation that classifies price/time slippage.

## 5. Live-port guard

**Recommendation:** Keep the hard paper-port guard for v1, and if it must be loosened later, make it a separate live runner/config profile rather than an environment override on the paper runner.
**Alternatives considered:** A boolean env override is easy, but it puts the highest-risk bypass next to normal deployment config; a separate `run_live.py` or explicit `LiveConfig(mode="live")` path creates a clearer audit boundary.
**Evidence:** The existing broker settings already implement mode/port validation for 7497/4002 paper versus 7496/4001 live (`PythonDataService/app/broker/ibkr/config.py:26-29`, `PythonDataService/app/broker/ibkr/config.py:107-127`).
Order placement then re-checks `IBKR_READONLY`, `IBKR_MODE=paper`, non-live port, DU account sentinel, and `confirm_paper=true` before any contract or order is built (`PythonDataService/app/broker/ibkr/orders.py:76-138`).
If a developer overrides the guard incorrectly, the blast radius is not theoretical: `place_paper_order` reaches `client.ib.placeOrder(...)` after safety checks pass (`PythonDataService/app/broker/ibkr/orders.py:229-271`).

## 6. Sizing cap

**Recommendation:** Do not call a 10% run the native strategy receipt; run the replay gate at `1.0`, and if the real paper week is capped, generate a separate capped backtest baseline with the cap explicitly named in the reconciliation.
**Alternatives considered:** The 10% cap reduces account-noise anxiety and one-fill P&L dominance, but it changes integer share counts and can hide target-sizing drift that only appears at the strategy's native `set_holdings(SPY, 1.0)`.
**Evidence:** The strategy's documented rule and actual call are full allocation (`PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:14`, `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:184`).
`Portfolio.set_holdings` computes `target_quantity = int(target_value / price)` from total value and reference price (`PythonDataService/app/engine/execution/portfolio.py:155-168`), so changing the target fraction changes the integer quantity path, not just the risk size.
The plan already acknowledges this as a post-stage risk (`docs/ibkr-paper-deployment-plan.md:388`); I would move that acknowledgment into the receipt definition itself.

## 7. Replay parity test design

**Recommendation:** Make fake-client fill prices exact for replay, assert quantity/order/timestamp/accounting/insight invariants, and reserve cent tolerances for real IBKR reconciliation.
**Alternatives considered:** `atol=Decimal("0.01")` is reasonable for broker paper fills, but in the fake-client gate it hides bugs because both engines should consume the same `Decimal` OHLCV inputs and the same next-bar-open fill rule.
**Evidence:** The existing NEXT_BAR_OPEN regression test compares its committed baseline strings exactly for entry/exit prices, indicators, PnL, and result (`PythonDataService/app/engine/tests/test_spy_next_bar_open_validation.py:94-127`), while the plan proposes a one-cent replay tolerance (`docs/ibkr-paper-deployment-plan.md:278-286`).
Share-count quantization happens in `Portfolio.set_holdings` (`PythonDataService/app/engine/execution/portfolio.py:155-168`); it affects `fill_quantity`, not the fill price if the fake fills directly from the same bar open.
Additional assertions should include order IDs monotonic within run, submit/fill timestamps, status/direction/tag, final cash/positions/fees, force-flat/no-open-position behavior, `strategy.trade_log`, and insight counts/scores.
The specific failure mode this test will still miss is collapsed or reordered broker lifecycle events: the current broker stream polls cached trades and may collapse rapid status transitions (`PythonDataService/app/broker/ibkr/orders.py:433-451`), while a synchronous fake can make submit/fill ordering look cleaner than the live event stream.

## 8. Insight scoring cadence in live

**Recommendation:** Step `InsightManager` on every minute bar in live replay and paper capture, even though insights are emitted only on consolidated 15-minute decisions.
**Alternatives considered:** Stepping only on 15-minute bars matches the strategy's decision cadence, but it changes the scoring lifecycle: an insight whose close time lands between consolidated emissions would finalize late and at the wrong reference price.
**Evidence:** The backtest scores expired insights after every minute bar using current reference prices (`PythonDataService/app/engine/engine.py:371-374`), and `InsightManager.step` is documented as called on every minute bar (`PythonDataService/app/engine/framework/insight_manager.py:108-127`).
The strategy emits a 75-minute price insight only when the entry signal fires inside the 15-minute handler (`PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:188-208`).
If live steps per minute, it preserves backtest scoring but requires reliable minute-bar production even on no-trade minutes; if live steps per consolidated bar, insight finalization, `reference_value_final`, and summary metrics can drift from the backtest.

## 9. Reference price for `set_holdings` in live

**Recommendation:** Use the consolidated bar close as the `set_holdings` reference price for the parity receipt, and record freshest-price slippage separately rather than using it for target share calculation.
**Alternatives considered:** The freshest 1-minute close is operationally closer to order time, but it changes the integer target quantity and turns the receipt into a market-adaptive sizing test instead of a parity test.
**Evidence:** The backtest updates the portfolio reference price on every minute bar (`PythonDataService/app/engine/engine.py:223-225`) and then overwrites it with the consolidated bar close immediately before the strategy handler runs (`PythonDataService/app/engine/strategy/base.py:108-115`).
`set_holdings` then derives integer quantity from that reference price (`PythonDataService/app/engine/execution/portfolio.py:155-168`), and the consolidator emits the previous working bar only when a later rounded bar arrives (`PythonDataService/app/engine/consolidators/trade_bar_consolidator.py:67-114`).
The failure mode of consolidated close is stale sizing if SPY moves between bar emission and order submission; the failure mode of freshest close is deterministic parity drift in share counts, which loses more rigor for a stage-2 receipt.

## 10. Order ID persistence

**Recommendation:** A `.live_state.json` sidecar is pragmatic enough for v1 only if it is atomic, run-directory scoped, and ignored by git; Postgres should be the follow-up once live-order state has a real schema.
**Alternatives considered:** Persisting to Postgres upfront sounds cleaner, but this repo currently uses `EnsureCreated` rather than a migrations workflow, so adding a live-state table is cross-stack schema work rather than a cheap Python-only change.
**Evidence:** Existing broker idempotency is explicitly process-local and non-durable across restarts (`PythonDataService/app/broker/ibkr/orders.py:40-44`), so the plan is right that restart persistence needs a new mechanism (`docs/ibkr-paper-deployment-plan.md:162`, `docs/ibkr-paper-deployment-plan.md:389`).
Backend has portfolio `Orders` and `PortfolioTrades` tables configured in `AppDbContext` (`Backend/Data/AppDbContext.cs:36-47`, `Backend/Data/AppDbContext.cs:277-326`), but no live-engine state set and the app initializes schema with `context.Database.EnsureCreated()` (`Backend/Program.cs:158-168`).
I also found no `Backend/Migrations` directory, so a Postgres-first choice should be treated as a deliberate persistence feature, not a small refactor.

## Cross-cutting risks

**Recommendation:** Before implementation, update the plan for stale dependency/version facts, shared IBKR ownership, timestamp/storage rules, and the exact acceptance gates.
The plan says to add `ib_async>=1.0,<2.0`, but the repo already has `ib_async>=2.0,<3.0` with broker integration justification (`PythonDataService/requirements-light.txt:23-28`), so implementation should reuse that instead of touching requirements.
The proposed `app/engine/live/ibkr_client.py` risks duplicating the existing `app.broker.ibkr` safety boundary; the plan should state whether live engine depends on `IbkrClient` or why it intentionally creates a second wrapper (`PythonDataService/app/broker/ibkr/client.py:123-310`, `PythonDataService/app/broker/ibkr/orders.py:76-138`).
Any live run artifacts, including `.live_state.json`, order logs, and equity curves, must store boundary timestamps as `int64 ms UTC`, because the repo explicitly bans `datetime`/`DateTime`/ISO as wire or storage timestamp formats (`CLAUDE.md:12`, `.claude/rules/numerical-rigor.md:82-91`).
`live_runs/` and `.live_state.json` are not currently ignored in `.gitignore` (`.gitignore:43-47`), so the plan should add a non-code hygiene task before the first paper run.
Finally, the plan's `mypy --strict` acceptance gate needs confirmation: I found `ruff.toml` and `requirements-dev.txt`, but no `PythonDataService/pyproject.toml` or mypy config in the service root, so that gate may be aspirational rather than currently enforceable.

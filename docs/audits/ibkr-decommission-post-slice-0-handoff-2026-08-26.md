# Handoff prompt — IBKR control-plane decommission (#1813), post-Slice-0

> **SUPERSEDED by PRD #1817** — stale in six verified places (exception count,
> `safety_verdict`'s live callers, the artifacts-root "extraction" claim, the
> AST-scanner gap, the `snapshot_ibkr_object` caller count, and the Slice 1
> description). See #1817's "Further Notes" section and
> `docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md` for the
> corrected record. Retained here for history only — do not treat as current.

Paste everything below the line into a fresh session in `/Users/inkant/learn-ai`.

---

Continue issue **#1813 — Decommission the IBKR control plane, keep only the data feed**. Slice 0 (establish the feed seam, delete nothing) is done and up for review. This handoff covers what's already decided, what shipped, and what's left — read it before touching anything, so you don't re-litigate settled decisions or re-discover already-fixed gaps.

## Current state — check this first

**PR #1816** (`decommission/ibkr-feed-seam-1813` → `master`) is **open**, not merged. `gh pr view 1816` before assuming Slice 0 is done — check whether it merged, got review comments, or is still pending. If it's merged, `origin/master` now has Slice 0's changes and Slice 1 branches off `master` directly. If it's still open, decide with the operator whether Slice 1 stacks on top of `decommission/ibkr-feed-seam-1813` or waits for merge first.

As of this handoff: all CI checks green (including the newly-parallelized `Python Tests` job from PR #1815 on master, merged into this branch conflict-free), CodeRabbit review pending, zero human reviews yet.

## Read first, in this order

1. `docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md` — the original scoping authority (509 lines). Its Slice 0–6 plan is still the plan of record for what's left.
2. `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` — Slice 0's design, including the "Corrections found during implementation-grounding verification" section (two deferred items with their reasons) and the operator's mid-design instruction that shaped everything after it: *"above all keep this a simple clean up operation, if there a surface too hard to migrate, delete it."* That instruction still governs every future slice.
3. `docs/superpowers/plans/2026-08-26-ibkr-decommission-slice-0.md` — Slice 0's implementation plan, for the level of rigor/verification expected (re-verify claims against real code before trusting a doc, including this one).
4. **PR #1816's description and diff** — the actual delivered state. Don't trust this handoff's file/line references without re-checking; code moves.
5. This document's "Decisions already made" section below — do not re-ask the operator these.

## Decisions already made — do not re-ask

These were asked and answered during Slice 0. Treat them as settled unless the operator raises them again:

1. **Options chain/surface pages: kept, migrated to the feed boundary** (not retired). Slice 0 confirmed `contracts.py`/`market_data.py`/`surface.py`/`symbol_search.py` already pass the feed-boundary structural test with no code change needed. The *physical* relocation of these routes (organizational only — **no new protocol**, that was explicitly rejected) is still Slice 6's job.
2. **Tick persistence (`persistence.py`): retire entirely.** No archival requirement stated by the operator. This is Slice 4's job — not yet started.
3. **Historical IBKR custody/exposure math registrations** in `docs/math-sources-of-truth.md` and `docs/architecture/engine-authority-map.md`: **retire explicitly**, don't silently drop a registered canonical path. This is Slice 2–4 work — not yet started.
4. **External-client risk for `/api/live-runs`, `/bot-events`, `/live-instances`, symbol search, Diagnose**: proceed as planned, no extra investigation needed before touching these routes.
5. **Chart-bar seam design: the cheap relocate-only fix, not a new `ChartBar` translation type.** The operator reversed an earlier "new type" decision mid-design specifically to keep this a simple clean-up. Every future slice should default the same way when a choice arises between "new abstraction" and "relocate/rename what's there" — pick the latter unless there's a concrete correctness reason not to.
6. **Options-route protocol: organizational only, no new protocol.** Same "keep it simple" instruction as #5.

## What Slice 0 actually shipped

- `app/broker/ibkr/bar_models.py` — 4 bar-only types extracted out of the mixed `models.py`.
- `app/broker/ibkr/config.py` — `live_artifacts_root()` (note: **not** `market_data_artifacts_root` — that name was tried, found wrong in final review since its 3 callers are bot-lifecycle/binding-evidence, not market data, and renamed before merge).
- `app/services/market_data_capability_service.py` — renamed from `broker_capability_service.py`. **Storage root is byte-identical**: still `Path(settings.live_runs_root) / "_broker" / "session_capabilities"`. Don't touch this path in any future slice without the same #1811-lesson scrutiny (silent durable-state orphaning).
- `app/broker/ibkr/health.py` — prose-only rewrite of 5 of 9 `_broker_health_condition` branches, dropping account/order language. **`safety_verdict` field itself is untouched** — see deferred items below.
- `app/broker/ibkr/api_evidence.py` / `app/broker/ibkr/order_evidence.py` — a generic, non-order-specific object-reflection helper (`snapshot_ibkr_object`, `_object_snapshot`, and its private dependents) was relocated out of the retiring `order_evidence.py` into the retained `api_evidence.py`. This was a **final-review catch**, not part of the original plan — a two-hop leak (`bars.py`/`contracts.py`/`market_data.py`/`surface.py`/`symbol_search.py` → `api_evidence.py` → `order_evidence.py`) that the original structural test's flat direct-import check couldn't see.
- `PythonDataService/tests/structural/test_ibkr_feed_boundary.py` — **the acceptance-criterion proof for every future slice.** It does a genuine transitive BFS walk of the `app.*` import graph from 17 seed modules (`RETAINED_FEED_MODULES`), not just each seed's direct imports — rewritten this way specifically because the flat version missed the leak above. When you retire a coupling in Slice 3/4, **delete the corresponding line from `_ALLOWED_EXCEPTIONS`** and re-run this test — it should go green with one fewer exception, and if it doesn't, that's real signal, not a test bug.

## Deliberately deferred — the two currently-tracked exceptions

Both are named, tracked exceptions in `_ALLOWED_EXCEPTIONS` in the structural test. Closing either means deleting its exception line and confirming the test still passes with the tighter check.

- **`app.broker.ibkr.client` → `app.broker.ibkr.order_error_stream.OrderErrorEvent`.** Order-error buffering in `client.py` has a second live consumer via `orders.py:689`. Removing it before that consumer retires breaks a registered endpoint. **Closes in Slice 4** (same slice as tick-persistence retirement).
- **`app.broker.ibkr.models` → `app.broker.safety_verdict.BrokerSafetyVerdict`.** The `safety_verdict` field on `IbkrConnectionHealth` has a second live caller, `broker_session_mirror.py`. **Closes in Slice 3** (same slice as the host-daemon shutdown).

## Other tracked follow-ups from Slice 0's final review — not blocking, not yet fixed

Found during Slice 0's final whole-branch review, ruled non-blocking for that PR, left as real but low-priority debt:

- **Latent, non-firing AST-scanner gap** in the structural test's `_imported_modules()` helper: `from app.pkg import module_name as alias` and relative imports (`node.level > 0`) aren't recorded, so an edge of that shape would be invisible to the transitive walk. Checked live at Slice 0's final review: 3 such edges exist in the whole repo today, none pointing at a banned module, zero relative imports among retained-feed-reachable code — so this is not a current leak, but if you're touching this test file again for Slice 3/4's exception removals, consider hardening it (`f"{node.module}.{alias.name}"` per alias) while you're in there.
- **Naming nit in `api_evidence.py`**: after the relocation, `snapshot_ibkr_object` is a public one-line alias with zero external callers (only `order_evidence.py` imports the private `_object_snapshot` it wraps). Either have `order_evidence.py` import the public name, or drop the alias and call `_object_snapshot` directly from `evidence_response`. Cosmetic, not urgent.
- **`BarSessionPhase` is defined twice** — `app/broker/ibkr/bar_models.py` and `app/marketdata/feed.py`, identical members. Pre-existing before Slice 0 (it used to be `models.py` vs `feed.py`), not introduced by this branch, but the two definitions are now one import hop apart. Worth collapsing when Slice 6 physically relocates the options/feed routes.
- **Three redundant `BANNED_PREFIXES` entries** (`app.broker.ibkr.account_recovery`, `account_truth`, `account_truth_freshness` are all already prefix-matched by the shorter `app.broker.ibkr.account` entry). Harmless over-inclusion, not a bug — just noise if you're reading the list closely.
- **The `health.py` "recovering" branch's title/summary still say "account-evidence recovery is still running."** Intentional — the underlying evidence-recovery behavior genuinely still exists until Slice 3 retires `safety_verdict`. When Slice 3 lands, reword this branch's title/summary too (only its `remediation` line was reworded in Slice 0), and it already has a test assertion pinning the current (pre-Slice-3) `remediation` text that you'll need to update in the same PR.

## What's left — Slices 1 through 6 (none started)

Per the original audit's slice plan, refined by what Slice 0 actually learned:

- **Slice 1**: `symbol_search.py` + the orphaned frontend component. Verify against current code before assuming the audit's description is still accurate — this repo's own lesson from Slice 0 (twice) is that a plan/spec's claim can be wrong until grepped.
- **Slice 2–4**: math-registry updates (`docs/math-sources-of-truth.md`, `docs/architecture/engine-authority-map.md`) — retire the historical IBKR custody/exposure canonical-path registrations explicitly, per decision #3 above. Don't silently drop a registered canonical path; the numerical-rigor rule requires an explicit retirement note, not a silent deletion.
- **Slice 3**: remove `safety_verdict` from `IbkrConnectionHealth` (closes the second tracked exception), reword the `health.py` "recovering" branch's remaining account-language, and pair the code deletion with an **explicit supervisor stop/uninstall handoff to the operator** for the host daemon listening on TCP 8765 — this cannot be completed by editing files alone, per the original handoff's operational note.
- **Slice 4**: remove order-error buffering from `client.py` (closes the first tracked exception) and retire `persistence.py` (tick persistence) per decision #2 above.
- **Slice 6**: physically relocate the options-chain/surface routes onto the market-data boundary (organizational only, per decision #6 — no new protocol).

## Operational notes that still apply (unchanged from the original handoff)

- **41 historical `live_runs/` and 195 `live_state/` directories are evidence, not build output.** Archive or move aside; never recursively delete. Same for `artifacts/accounts/*/account_safety.json`. This repo's convention for authority files is relocate, never delete.
- **Do not delete `_broker/session_capabilities`** while Alpaca Start/Resume still reads it. Slice 0 confirmed this path is still live and byte-identical after the service rename — any future slice touching capability storage needs the same scrutiny that caught (and avoided) a #1811-class silent-orphan risk in Slice 0.
- **Every deletion PR should carry a receipt** listing each removed symbol, route, config field, test family, and contract entry, with the evidence it had no live consumer, and the archive location of anything preserved.

## Repo rules that will bite on this work

- **`origin/master` is branch-protected.** Branch and open a PR; never push directly to it.
- **Full pytest is the one pre-push gate.** Run from the host venv: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" ./.venv/bin/python -m pytest tests -q`. The empty secret matters — without it ~33 router tests 403. Slice 0's baseline: **8545 passed, 52 skipped, 5 xpassed, 0 failed** post-merge-with-master — any new failure is yours to explain.
- **Project-scope lint, not per-file**: `python3 -m ruff check PythonDataService/app/ PythonDataService/tests/` and `npx eslint Frontend/src/ --max-warnings 0`.
- **Regenerate the OpenAPI contract in the same PR as any route change**: `cd PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check` (fails loudly if stale; drop `--check` to regenerate).
- **Run the `thermo-nuclear-code-quality-review` skill yourself, before the first push that opens each PR**, and fix every major finding.
- **A flaky LEAN e2e** can fail a full-suite run: `Benchmark and performance series has N misaligned values`. Key triage on that exception, not the test name. Slice 0's full-suite runs never actually hit this flake — don't assume it's gone, just note it didn't fire this time.

## Working style that paid off on Slice 0

- **Verify claims against real code before trusting a doc — including this one and the ones it points to.** Slice 0 caught three real spec defects (a live second consumer on the order-error buffer, a live second consumer on `safety_verdict`, a storage-root divergence risk) before any code was written, purely by grepping instead of trusting the audit's summary. The final whole-branch review caught a fourth real gap (the `api_evidence.py`/`order_evidence.py` transitive leak) that five individually-clean per-task reviews had all missed, specifically because it required looking at the combined result rather than one task's diff at a time — **always run a whole-branch review after the last task, even when every task was independently approved.**
- **When a review finding conflicts with a plan's literal constraint text, rule on the constraint's evident intent, not its letter — and record the reasoning and the cost-if-wrong.** Slice 0's Global Constraints literally forbade any bucket-file edit beyond a one-line import fix; the correct fix for the final review's Critical finding required relocating a function out of a bucket file. The ruling (the constraint exists to prevent behavior drift in retiring control-plane logic, not to forbid ever correcting a misfiled non-order utility) was recorded in the SDD ledger with an explicit "cost if wrong" — do the same for any future plan-vs-finding conflict.
- **Prefer the real fix over a third tracked exception when the real fix is cheap and in-idiom.** The final review offered both a "minimum" fix (add a third exception, weaken the test's claim) and a "preferred" fix (relocate the misfiled code, keep the test's claim true). The preferred fix was taken. Exceptions are for genuine, externally-blocked deferrals (a second live consumer not yet retired) — not a shortcut around an inconvenient discovery.
- **When a smarter test claims to catch a class of bug, don't just read the new algorithm — execute it and reproduce the original violation.** The scoped re-review of Slice 0's fix round independently ran the rewritten structural test's graph-walk logic itself (not just read the diff), re-injected the removed edge, and confirmed it fires — and separately confirmed a synthetic deeper leak is also caught, ruling out an accidentally-hardcoded-depth "transitive" check. Do this for any test whose entire purpose is proving a negative (X never happens).

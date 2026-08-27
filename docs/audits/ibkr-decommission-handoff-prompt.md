# Handoff prompt — IBKR control-plane decommission (#1813)

Paste everything below the line into a fresh session in `/Users/inkant/learn-ai`.

---

Work issue **#1813 — Decommission the IBKR control plane, keep only the data feed**.

## Read first, in this order

1. `docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md` — **the scoping authority.** 509 lines, read at commit `19f01eb9`. Its slice plan (Slice 0–6) is the plan of record.
2. `gh issue view 1813` — read the body, then **the pinned comment correcting it**. The body's original numbers ("26 modules", "three files survive", six slices) are superseded by the audit. Do not work from them.
3. `AGENTS.md` — the deprecation rule that authorises this work. It permits IBKR-area changes "only when the task explicitly concerns removal, decommissioning, migration away from IBKR". This is that task, so the rule is a licence here, not a blocker.
4. `docs/architecture/adrs/0048-episode-age-policies-and-the-admission-marker-substrate.md` §4f — the decision this supersedes, and why.

Then read the closed #1811 and #1799 before designing anything that fences, leases, or migrates durable state. Six real defects are documented there, each found in review of a plausible-looking implementation.

## The operator's decision, in their words

> "IBKR is only a data feed for Alpaca. The IBKR clerk and other machinery should go away, related frontend routes also."

## The three corrections that matter most

The audit overturns the obvious reading of that decision. Internalise these before planning:

1. **`client`/`bars`/`models` is not a safe keep-list.** They are the only *directly* imported roots, but their transitive closure is **twelve** modules. Neither `app/broker/alpaca/` nor `app/services/bot_runner.py` imports IBKR at all — only `app/marketdata/ibkr_feed.py` does. A three-file checkout does not import.
2. **Three live feed paths sit outside that claim**: Broker V2 live charts and the gallery (via the IBKR bar aggregator), Alpaca Start/Resume admission (via persisted market-data capability), and the **global Angular shell**, which mounts a health banner polling `GET /api/broker/health` every five seconds.
3. **There is a live, navigable IBKR options product** at `/broker/options-chain` and `/broker/options-surface` — in the Options menu, not behind a retired redirect. Whether it survives is a **product decision for the operator**, not a consequence of retiring broker actuation.

## Start here

**Do not delete anything in your first PR.** The audit is explicit that Slice 0 — establishing and naming the data-feed seam — must precede deletion. Deleting first and refactoring after would break the live chart, the gallery, Alpaca admission, and the global health banner.

Slice 0, from the audit:
- ~~Decide the options-chain/surface question **with the operator** before anything else — it changes the keep-list.~~ **Resolved during Slice 0 brainstorming**: the options-chain and options-surface pages are retained, migrated to the market-data feed boundary rather than retired. This is no longer a blocking decision for any later slice — see `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md`'s decision log. What's left for those pages is import-boundary pinning (already done — `contracts.py`/`market_data.py`/`surface.py`/`symbol_search.py` pass the Slice 0 structural test) and Slice 6's physical relocation, organizational only, no new protocol.
- Add broker-neutral bar/chart types. Note the design gap the audit found: `MarketDataFeed` exposes minute bars, but the panel requests **five-second** bars (`panel_chart_data_source.py:72-75`). That needs a neutral five-second stream or a deliberate feed-local chart seam.
- Repoint chart projection, live chart window, live bar aggregator, bar persistence, gallery, and panel at the shared seam.
- Rehome feed capability as **market-data capability** (not broker-control capability) and extract a generic artifact root from `account_truth_artifacts_root()`.
- Split connection/feed health, reconnect, keepalive, and feed event codes away from account/order/session concerns.
- Remove order-error buffering and broker-session-event emission from `IbkrClient` — that is control-plane residue, not a feed requirement. **Deferred, not removed, in Slice 0**: both couplings have a second live consumer outside Slice 0's scope (`orders.py:689` for order-error buffering; `broker_session_mirror.py`/`broker_session_history.py`/`routers/broker_session.py` for `broker_session_events`), so removing either now would break a still-registered endpoint. Both are named, tracked exceptions in `tests/structural/test_ibkr_feed_boundary.py`'s `_ALLOWED_EXCEPTIONS`, closing in **Slice 4**.

**Slice 0 acceptance:** the retained feed imports no account/order/session module, and Alpaca Start/Resume, the panel chart, the gallery, the global health banner, reconnect, and any retained options pages all still work through the new seam.

## Operational, not just repository work

- The **host daemon is running and listening on TCP 8765** — idle, not absent. Code deletion must be paired with an explicit supervisor stop/uninstall handoff to the operator. You cannot complete this by editing files alone.
- **41 historical `live_runs/` and 195 `live_state/` directories are evidence, not build output.** Archive or move aside; never recursively delete. Same for `artifacts/accounts/*/account_safety.json`. This repo's convention for authority files is relocate, never delete.
- **Do not delete `_broker/session_capabilities`** while Alpaca Start/Resume still reads it — migrate it to the feed capability store first.

## Open questions to put to the operator, not to guess

1. ~~Do the live options chain/surface pages stay? (Blocks Slice 0.)~~ **Resolved**: yes, retained and migrated to the feed boundary — see the note under "Slice 0, from the audit" above.
2. Tick persistence (`persistence.py`) — archival intent before deletion?
3. Historical IBKR custody/exposure folds are still named canonical in `docs/math-sources-of-truth.md:92-93` and `docs/architecture/engine-authority-map.md:46-48`. Preserve as forensic math, or retire explicitly? Do not silently remove a registered canonical path.
4. The audit's bounded 3.5-hour local log window cannot rule out an external client for `/api/live-runs`, `/bot-events`, `/live-instances`, symbol search, or Diagnose. Check longer proxy retention or operator scripts before retiring those.

## Repo rules that will bite on this work

- **`origin/master` is branch-protected.** Branch and open a PR; never push to master.
- **Full pytest is the one pre-push gate.** Run it from the host venv: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" ./.venv/bin/python -m pytest tests -q`. The empty secret matters — without it ~33 router tests 403.
- **Project-scope lint, not per-file:** `python3 -m ruff check PythonDataService/app/ PythonDataService/tests/` and `npx eslint Frontend/src/ --max-warnings 0`.
- **Regenerate the OpenAPI contract in the same PR as any route change**, or CI fails. `contracts/openapi/python-data-service.openapi.json` and `contracts/data-plane-control-surfaces.json` both still publish legacy routes.
- **Run the `thermo-nuclear-code-quality-review` skill before the first push that opens each PR**, and fix every major finding.
- **A flaky LEAN e2e** can fail a full-suite run: `Benchmark and performance series has N misaligned values`, from LEAN's C# statistics engine. Key triage on that exception, not the test name — it surfaces through more than one test. Re-run the single test in isolation; if it passes, it is the flake.
- Every deletion PR should carry a **receipt** listing each removed symbol, route, config field, test family, and contract entry, with the evidence it had no live consumer — and the archive location of anything preserved.

## Working style that paid off on the prior lanes

The audit is point-in-time evidence, not authority — verify each claim against current code before acting on it. Issue bodies in this repo have repeatedly been stale, and the two most expensive defects in the preceding work were both found by reading code rather than trusting a summary. When an authority conflicts with another (AGENTS.md vs an accepted ADR, say), surface the conflict and ask — do not silently pick.

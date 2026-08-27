# Close-out plan — IBKR control-plane decommission (#1813), Slices 1–6

Authored 2026-08-26, after PR #1816 (Slice 0) merged. Supersedes the "What's
left" section of `docs/audits/ibkr-decommission-post-slice-0-handoff-2026-08-26.md`,
which is stale in several places (corrections below). The slice *content* of
`docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md` remains
the authority; this document re-batches it into three PRs and records the
decisions taken in the 2026-08-26 grilling session.

## Corrections to the handoff — verified against code, not inherited

| Handoff claim | Verified reality |
| --- | --- |
| "Two tracked exceptions" | **Three.** The extra is `app.broker.ibkr.client → app.services.broker_session_events` (closes Slice 4). |
| "Slice 1 = `symbol_search.py` + orphaned frontend component" | Wrong. The audit's Slice 1 is **account-safety UI and projection**. The handoff garbled it. |
| "`safety_verdict`'s second live caller is `broker_session_mirror.py`" | **Incomplete.** It is also populated on the *retained* `GET /api/broker/health` (3 call sites, `routers/broker.py:197–228`) and referenced by `app/broker/runtime_snapshot.py`. Removing it is a live contract change. |
| "AST-scanner gap (aliases / relative imports) is open" | **Already fixed** on master (`alias.name`, `node.level` handling present in `_imported_modules`). Drop from the backlog. |
| "`snapshot_ibkr_object` is a public alias with zero callers" | Now called internally at `api_evidence.py:147`. Nit is effectively resolved. |
| Slice 2 blocked on "extract the generic artifacts root" | **Not a blocker.** `account_truth_artifacts_root()` and Slice 0's `live_artifacts_root()` are byte-identical (`Path(settings.live_runs_root).parent`). It is a 5-call-site repoint, not an extraction. |

Still real: `BarSessionPhase` is duplicated as a `Literal` in `bar_models.py:21`
and `feed.py:34`; three redundant `BANNED_PREFIXES` entries. Both → PR-C.

## Decisions taken this session

1. **Full close-out.** All six slices; #1813 closes at the end.
2. **Three PRs**, not six: A = Slices 1+2, B = Slices 3+4, C = Slices 5+6.
   Operator's call, against the recommendation of one-PR-per-slice. Mitigation:
   clean per-slice commits inside each PR so revert stays surgical.
3. **Host daemon stopped** (PID 14707 killed 2026-08-26; port 8765 free). The
   Slice 3 operational handoff is therefore already discharged.
4. **Fleet frozen** for the duration — no concurrent live stress runs.
5. **Drop `safety_verdict`** from the retained `/api/broker/health`. Nothing
   renders it (generated OpenAPI types only). Requires OpenAPI + generated-TS regen.
6. **Retire `symbol_search` entirely** — backend module, route, `BrokerService.searchSymbols`,
   and `shared/broker-instrument-card/`. A deploy-page symbol picker is wanted but
   deferred to a separate issue (see "Deferred follow-ups").
7. **Retire `diagnostics.py`** (403 LOC, sole consumer `routers/broker.py`).
8. **Artifacts are NOT deleted.** See "Artifacts ruling".
9. **Escalation policy:** if a surface is genuinely hard to migrate, delete it and
   record it in the PR receipt with its no-live-consumer evidence. Do not block.
10. **Verification:** full pytest per PR. No live smoke run before closing #1813
    (operator's call — the first fleet unfreeze is therefore the real end-to-end test).

## Artifacts ruling — do not delete

The instruction was "delete them, if they are not used by current bots." The
condition fails for both:

- `artifacts/live_runs/_broker/` — written **2026-08-26 23:19**. Holds
  `session_capabilities/DUM284968`, the exact path Alpaca Start/Resume reads and
  that Slice 0 deliberately kept byte-identical. `connection_events.jsonl` is
  actively appended.
- `artifacts/live_state/` — written **2026-08-26 16:22**. Holds the
  `validation-spy-0824` / `validation-tsla-0824` fleet bots.

Neither is where disk is going: together they are 338M of 4.2G. The bulk is
`artifacts/runs` (2.3G) and `artifacts/lean-sidecar` (1.1G) — regenerable
backtest output, deletable as housekeeping **unrelated to #1813**.

## Where live bars come from (settled, verified)

`host_daemon.py` contains no bar plumbing at all. Live bars flow:

    IB Gateway (:4002) → polygon-data-service container → app/broker/ibkr/bars.py
    (reqRealTimeBars, 5-sec TRADES) → live_bar_aggregator → chart_projection_service

All retained feed, inside the data-plane process. Empirical proof: the Gateway
socket remained ESTABLISHED after the daemon was stopped. Removing the daemon
does not affect bars for trading bots.

---

## PR-A — Retire account-safety, Account Truth, and reconciliation

Slices 1 + 2. Closes no tracked exception.

**Do first:** repoint the 5 `account_truth_artifacts_root()` call sites
(`main.py:85,327`; `routers/account_reconciliation.py:66,79`;
`routers/broker_account_truth.py:21,31`; `routers/broker.py:373,381`) onto
`app.broker.ibkr.config.live_artifacts_root()`, then delete the account-bucket
function. Confirm the active consumers (`panel_data_source.py:55,215`,
`sqlite_roster_status.py:32,87,141`) resolve the identical path.

Backend deletions: `account_safety_access`, `account_safety_snapshot`,
`account_truth_refresh`, `account_reconciliation`, `account_event_journal`,
`account_truth_snapshot`, `journal_recovery`; `broker/ibkr/account*.py`
(`account`, `account_recovery`, `account_truth`, `account_truth_freshness`);
routers `account_reconciliation.py`, `broker_account_truth.py` (unregister in
`main.py`).

`clerk_transactions.py`: remove the `broker == "ibkr"` branches (`:101`, `:171`),
narrow the param to `Literal["alpaca"]`, delete the legacy Postgres/journal
projection readers. **No UI impact** — no component ever sets `broker: 'ibkr'`.

Frontend: delete `account-freeze-banner/`, `account-roster/`, `account-safety/`,
`account-truth-board/`, `shared/operator-blocker-list/`,
`account-desk/account-desk-directory-store.service.ts`. Remove the `'ibkr'`
option from `clerk-transaction-history.types.ts`, `account-events.types.ts`,
`broker.service.ts:247`. **Keep** `account-desk/` and
`clerk-transaction-evidence-drawer/` — active Alpaca surfaces.

Docs: explicit retirement notes for the account/custody rows in
`docs/math-sources-of-truth.md` and `docs/architecture/engine-authority-map.md`.
Never silently drop a registered canonical path.

## PR-B — Retire broker session, activity, host bridge, orders, and P&L

Slices 3 + 4. **Closes all three tracked exceptions.**

Services: `broker_session_mirror`, `broker_session_history`,
`broker_session_reconciler`, `broker_session_events`, `broker_activity_publisher`,
`broker_activity_publisher_registry`, `broker_activity_reconciler`,
`broker_activity_reconstruction`, `broker_activity_templates`,
`broker_activity_wal`, `host_capability`, `activity_evidence_matching`,
`bot_event_rejection_bridge`, `live_log_failures`. For `fleet_contamination`:
remove the retired IBKR I/O wiring only — the canonical fold already lives
separately in `app/engine/live/fleet.py` and must not be touched.

Routers: `broker_session`, `broker_activity`, `live_instances`, `live_runs`,
`bot_events` (unregister in `main.py`).

Host bridge: `engine/live/host_daemon.py`, `host_daemon_client`, and the
`daemon_auth` / lease / installer wiring. Supervisor already stopped.

`broker/ibkr`: `orders`, `order_history`, `order_previews`, `order_projection`,
`order_evidence`, `order_error_stream`, `pnl`, `persistence`, `diagnostics`,
`symbol_search`.

`routers/broker.py` split: remove account/order/P&L/evidence/diagnostics/
symbol-search routes; keep feed health/lifecycle, options data, bar snapshots.
Drop `safety_verdict` from `/health` (3 call sites), delete
`app/broker/safety_verdict.py`, update the `runtime_snapshot.py` docstring, drop
the `models.py:27` import.

`health.py`: reword the "recovering" branch's title/summary (Slice 0 reworded
only its `remediation`) and update the test assertion pinning the old text.

`client.py`: remove order-error buffering and `broker_session_events` emission.

Frontend cross-cut (deliberate — splitting it would strand a 404 caller between
PRs): delete `broker-session-mirror/`, `broker-orders/`,
`broker-operation-result/`, `services/broker-session-mirror.service.ts`,
`services/broker-connectivity.service.ts`, `shared/broker-instrument-card/`, and
`BrokerService.searchSymbols`.

Structural test: delete **all three** `_ALLOWED_EXCEPTIONS` lines. The test must
pass with zero exceptions. If it does not, that is real signal.

Docs: explicit retirement of the broker-activity verdict and fleet-contamination
registrations.

## PR-C — Consolidate the feed surface and publish the receipt

Slices 5 + 6.

- `BrokerService`: **rename** retained portions to state the seam; do not split
  into new services (standing decision — prefer relocate/rename over new abstraction).
- Delete orphan helpers `components/broker/ibkr-portal.ts`, `operator-severity.ts`.
- Collapse the duplicated `BarSessionPhase` (`bar_models.py:21` / `feed.py:34`).
- Remove the 3 redundant `BANNED_PREFIXES` entries.
- Collapse temporary compatibility modules into a documented feed API.
- Regenerate OpenAPI + generated TS. Keep the 15 `app.routes.ts` redirects
  redirect-only; do not attach behavior to retired aliases.
- **Retirement receipt**: every removed symbol, route, config field, test family,
  and contract entry, each with its no-live-consumer evidence, plus the preserved
  artifact paths.
- Close #1813.

---

## Gates — every PR

    python3 -m ruff check PythonDataService/app/ PythonDataService/tests/
    npx eslint Frontend/src/ --max-warnings 0
    cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" ./.venv/bin/python -m pytest tests -q
    cd PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check

Plus the `thermo-nuclear-code-quality-review` skill before the first push that
opens each PR (one-shot per PR; re-pushes addressing review comments do not
re-trigger it).

**Test-baseline discipline — the main methodological risk.** Slice 0's baseline
is 8545 passed / 52 skipped / 5 xpassed / 0 failed. Roughly 79 test files and
36k LOC of tests reference the retiring surfaces, so the pass count will drop
substantially and legitimately. Each PR must state its **expected** deletion
count and land on a recorded new baseline; otherwise a genuine regression hides
inside an expected drop. A failure that is not explained by a deliberate
deletion is a real failure.

Known flake to key on by exception, not test name: LEAN sidecar
`Benchmark and performance series has N misaligned values`.

## Deferred follow-ups — separate issues, not #1813

- **Deploy-page symbol picker.** The page uses a bare `<input id="deploy-symbol">`.
  Recommend building on Polygon `/v3/reference/tickers` (`polygon_client.py:934`
  already calls it) rather than reviving IBKR `reqMatchingSymbols` — deploy-time
  symbol lookup should not depend on a Gateway measured at 37.9% nightly downtime.
- **Artifacts housekeeping.** `artifacts/runs` (2.3G) + `artifacts/lean-sidecar`
  (1.1G) are regenerable and reclaimable. Unrelated to #1813.

## Accepted risks

- ~13k LOC in PR-A and PR-B each; large to review and coarse to revert.
  Mitigated by per-slice commits within each PR.
- No live smoke run before #1813 closes; the first fleet unfreeze is the real
  end-to-end test. Retained surfaces most worth checking then: Alpaca
  Start/Resume, panel chart, gallery, options chain/surface, feed banner.

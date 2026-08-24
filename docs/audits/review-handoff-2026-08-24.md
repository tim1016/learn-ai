# Independent review handoff — 2026-08-24 session

You are an independent review agent with no prior context. Your job: review
the eight commits pushed to `master` on 2026-08-24 (they were committed
direct-to-master under an explicit operator authorization, so **no PR review
and no thermo-nuclear code-quality review has happened**), and adjudicate the
nineteen findings the session produced. This document is your complete brief;
the three companion documents in this folder carry the evidence:

- `paper-ceremony-strategy-fleet-2026-08-24.md` — the day's audit (what ran,
  session results).
- `bot-launch-ops-study-2026-08-24.md` — findings F1–F19 with data and
  recommendations.
- `judgment-calls-2026-08-24.md` — 20 numbered in-session decisions and why.

## 1. What happened (one paragraph)

The operator directed a day-long paper-trading exercise: five registry
strategies were backtested on both engines, deployed as 1-share SPY bots
through a restored evidence-only override path, taken through a full
lifecycle exercise (stop/resume/pause probes), joined by validated
`deployment_validation` bots on four symbols, crash-tested (SIGKILL under
load), and stress-tested (cadenced launches to 18 concurrent bots). Two
production bugs were found and fixed in-session; nineteen findings were
documented. Day totals: 26 bots, 73 fills, −$3.98 realized paper P&L, and
three deliberately stranded 1-share positions (see F18).

## 2. The commits under review (oldest first)

| Commit | Kind | Review weight |
|---|---|---|
| `02365e82` | backend policy | **Highest.** Restores evidence-only Paper deploys (reverses part of #1746). Verify the boundary claims: the pairing gate admits `evidence_only` only when the flag event is *current*; the override accepts artifact **absence** but never **drift**; Live is untouched; accepted strategies must still *reject* a superfluous override. |
| `20338171` | frontend | Deploy drawer renders the evidence-override panel for evidence_only+paper and attaches `evidence_override` to the request. Check gating, reset-on-strategy-change, and `submissionStillCurrent`. |
| `238821c7` | backend fix | Resume concurrency-token normalization for `market-liveness-clock/-symbol` refs. Verify the central claim: normalizing the observation instant **cannot mask a real liveness change** (allowed/reason_code stay in the token). |
| `730ede96`, `acde45b1`, `03eceb1a`, `3d610c7c` | docs | Audit/study/judgment documents. Spot-check claims against receipts. |
| `ff5ed49f` | backend fix | Deploy boundary refuses `strategy_instance_id` > 25 chars (order_ref cap). Verify the guard is on the deploy **command only** (read models must keep accepting existing long-named bots) and that resume/status paths for pre-existing long bots still work. |

Review standard: the repo's thermo severity triage applies — every *major*
finding (structural regression, boundary leak, canonical-helper duplication,
policy hole) is a blocker to report prominently; minor nits are optional.

## 3. Findings to adjudicate (F1–F19)

Status key: ✅ fixed in-session (verify the fix), ⛳ open (confirm/refute,
then it becomes a tracked issue), ℹ️ informational (challenge if wrong).

| # | Finding | Status |
|---|---|---|
| F1 | Panel Resume 409'd on every attempt (token churn) | ✅ `238821c7` |
| F2 | `pause`/`continue` never presented under SQLite custody — unreachable controls | ⛳ |
| F3 | Two parallel stop surfaces (panel ~20 s vs runner 0.29 s) | ⛳ |
| F4 | Feed-readiness cold start (~45 s) blocks Resume after service start | ⛳ |
| F5 | Deploy refusal ordering shows the wrong gate first | ⛳ |
| F6–F8 | Morning items: zero-bar engine run returns success; UI flag form hard-requires QC id; flag toggle defaults to Reject | ⛳ |
| F9 | Long sid crashes on first order | ✅ `ff5ed49f` |
| F10 | Dry-run deploys 500 on virtiofs topology + leak orphan `sim:` dirs + raw error envelope | ⛳ |
| F11 | Burst deploys flap "Market Data unhealthy", masking real refusals | ⛳ |
| F12 | LIVE chart pane lags its own bot by 7–17 min, heals lazily, no staleness marker | ⛳ |
| F13 | Panel reads serialize globally (56 ms alone → 2.6 s × 10 concurrent) | ⛳ |
| F14 | Gallery snapshot heavy (5.6 s / 751 KB / all historical bots) | ⛳ |
| F15 | Action idempotency checked only after presentation | ℹ️ |
| F16 | `retire` never presented (dead vocabulary) | ⛳ |
| F17 | `prepare_safe_flatten` presents `enabled: true` but refuses execution (view action) | ⛳ |
| F18 | **Crash-held exposure has no path to flat** — SafeFlattenPlan has no executor; flatten_stop unreachable; manual trading gated; carryover disabled | ⛳ **top priority** |
| F19 | Concurrent same-clock reduces race `BROKER_SNAPSHOT_STALE` → retryable refusal escalates to bot crash | ⛳ **second priority** |

Deliverable for ⛳ items: confirm or refute with your own repro, then file
one GitHub issue per confirmed finding (title `F<N>: <short claim>`, body
citing the study section and your repro).

## 4. Live evidence you can still inspect

- **Three stranded broker positions** (SPY, QQQ, AAPL — 1 share long each)
  on paper account `PA3KWXU1C4C3` are F18's standing evidence. Do not
  attempt to flatten them; disposition is the operator's call.
- Flag-event ledger: `PythonDataService/artifacts/strategy_validation/flag_events.json`
  (today's `validated`/`evidence_only` events, actor `local:root`).
- Canary admission ledger (+ checkpoint): under
  `PythonDataService/artifacts/` — events seq 2–7 are today's pairings.
- Clerk SQLite custody + decision receipts: `production-alpaca-clerk`
  volume inside the `polygon-data-service` container; the evidence pages
  (`GET .../bots/{sid}/evidence`) are the audit-logged read path.
- Crashed-run diagnostics:
  `GET /api/brokers/alpaca/bots/{sid}/runs/current` for
  `ceremony-spy-strategy-c-0824` (F9) and `st08-dv-spy-0824` (F19).
- Session scratchpad JSONL logs (lifecycle/probes/stress/close-stats) are
  **ephemeral** — numbers from them are transcribed into the study doc;
  treat the study doc as the durable record.

## 5. Environment notes (gotchas that will bite you)

- All control-plane calls need the header `X-Data-Plane-Control-Secret`;
  fetch it with
  `podman exec polygon-data-service printenv DATA_PLANE_CONTROL_SECRET`
  into a shell variable — never print it.
- Backend host port is **5050**; the data plane is `:8000`.
- The data-plane container does **not** hot-reload —
  `podman restart polygon-data-service` after code changes (a restart also
  triggers F4's ~45 s feed warmup).
- Python tests run on the host venv: from `PythonDataService/`,
  `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/` (the
  empty secret prefix avoids ~33 router-test 403s).
- Frontend tests: scope them —
  `podman exec my-frontend npx ng test --watch=false --include='**/<name>.spec.ts'`
  (glob directories OOM the container).
- **The main checkout at `/Users/inkant/learn-ai` is shared by concurrent
  sessions.** Do not switch its branch. Use `git worktree add` for any
  branch work (a mid-session branch switch by a parallel session caused a
  mixed commit today; see judgment call and the recovery in the session
  log).
- The market is closed at review time: bots deploy and decide, but fills
  need a live session; F11/F12/F19 repros need market hours.

## 6. Suggested verification sequence

1. Read the three companion docs end to end.
2. `git log origin/master --oneline -8` and diff each code commit; review
   against §2's per-commit questions.
3. Run the targeted test files for the two fixes:
   `tests/broker/v2panel/test_panel_projection.py` (token stability) and
   `tests/broker/v2panel/test_deploy_scoped_route.py` (sid cap), then the
   full suite.
4. Static-verify F18: confirm `SafeFlattenPlan` has no executor
   (`grep -rn "SafeFlattenPlan\|reduction_plan" PythonDataService/app/`)
   and that `flatten_stop` is absent from
   `SQLITE_PANEL_LIFECYCLE_ACTION_IDS`.
5. Static-verify F19: the runner's crash escalation path for
   `AdmissionBlockedError` (see `st08-dv-spy-0824`'s diagnostic:
   `app/broker/alpaca/clerk/sqlite/uncertainty.py:581`).
6. Adjudicate the remaining ⛳ findings; file issues; write your review as
   a comment on this PR — findings-first, most severe first, with a clear
   verdict per §2 commit.

## 7. Out of scope

- Disposition of the three stranded shares (operator decision).
- Extending the deployment_validation qualification corpus beyond
  SPY/QQQ/TSLA/AAPL (a promotion task, #1730 lineage).
- The `.claude/worktrees/` and other sessions' branches
  (`ux/bots-trader-lens` etc.).

# IBKR Paper Dry Run — Operator Runbook

This runbook covers Phase D of the IBKR paper-shadow deployment
([spec](../superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md)
§12 "Phase D"). It is a one-trading-day dry run against IB Gateway in
read-only mode — no orders are placed; the goal is to verify every
piece of plumbing works end-to-end before the 15-day paper week starts.

## Prerequisites

Before kicking off the dry run, confirm:

- [ ] Phases B and C-1 are merged to master (`reconcile.py`, `run_ledger.py`,
      `pre_flight.py`, `run.py` with `init-ledger` and `pre-flight`).
- [ ] Phase C-2 is merged (artifact writers, `start` and `reconcile`
      subcommands, `--readonly` flag, intra-day fatal halt).
- [ ] Phase A operator work is done: QC audit copy committed at
      `references/qc-shadow/SpyEmaCrossoverAlgorithm.py`, plus the
      Test 1 + Test 2 QC exports under `references/qc-shadow/backtests/…`.
      Both tests must be passing on master.
- [ ] IB Gateway installed locally and configured for the **DU paper
      account** (not the live account). Confirm:
  - Gateway port: `4002` (paper) — NOT `4001` (live).
  - Account-id sentinel: starts with `DU`.
- [ ] `.env` populated with the resolved IBKR settings:
  ```
  IBKR_MODE=paper
  IBKR_PORT=4002
  IBKR_HOST=auto      # or an explicit IP if not running in container
  IBKR_CLIENT_ID=42   # spec §5: this run owns client_id=42
  IBKR_READONLY=true  # dry run only — flip to false on day 1 of paper week
  ```
- [ ] Source tree is clean within scope:
      `git status -- PythonDataService references/qc-shadow` returns
      empty. (Phase C-1's `init-ledger` will refuse otherwise.)
- [ ] NTP is reachable from the host (the pre-flight queries
      `pool.ntp.org` by default; offset must be < 1 s).
- [ ] The polygon-data-service container is running (`podman ps`).

## Step 1 — Initialize the dry-run ledger

Build the run ledger. This writes
`PythonDataService/artifacts/live_runs/<run_id>/run_ledger.json` and
records the run identity (§10) — strategy spec hash, QC audit copy
hash, account id, start-of-session UTC ms.

```bash
podman exec polygon-data-service python -m app.engine.live.run init-ledger \
  --repo-root /workspace \
  --strategy-spec-path /app/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json \
  --qc-audit-copy-path /workspace/references/qc-shadow/SpyEmaCrossoverAlgorithm.py \
  --qc-cloud-backtest-id <PASTE FROM QC CLOUD UI> \
  --account-id DU<your-paper-account> \
  --start-date-ms $(date -u -d "today 09:30 EDT" +%s000) \
  --live-config-json '{"symbol":"SPY","force_flat_at":"15:55","client_id":42}' \
  --run-root /app/artifacts/live_runs
```

Expected outcome:
- Exit 0
- Stdout: `[INIT-LEDGER] wrote /app/artifacts/live_runs/<sha>/run_ledger.json (run_id=<sha>)`

Failure modes to expect:
- **Dirty tree** → exit 1, message "dirty-tree halt: working tree has N uncommitted change(s)…". Fix: commit or stash within scope.
- **Missing strategy_spec_path / qc_audit_copy_path** → exit 2.
- **Existing run dir** → exit 2 (rare; only happens if you already initialized identical inputs today). Pass `--force` only if you genuinely want to overwrite.

Record the resulting `run_id`; you'll use it in subsequent steps.

## Step 2 — Pre-flight gate (morning gate)

Run all the `pre_flight.py` halt checks. This is the same code path
that fires every morning during paper week (§6.4).

```bash
podman exec polygon-data-service python -m app.engine.live.run pre-flight \
  --repo-root /workspace \
  --run-dir /app/artifacts/live_runs/<run_id>
```

Expected outcome — every line should be `OK`:
```
[PRE-FLIGHT] OK  clean_tree: clean tree across [PythonDataService, references/qc-shadow]
[PRE-FLIGHT] OK  run_state_intact: run_ledger.json present and parseable at …
[PRE-FLIGHT] OK  no_halt_flag: no prior-day halt flag set
[PRE-FLIGHT] OK  ntp_offset: clock drift +0.012s within 1.0s budget
[PRE-FLIGHT] all checks passed; runner may proceed.
```

Failure modes to expect:
- `FAIL clean_tree: working tree has N uncommitted change(s)…` — commit/stash and re-run.
- `FAIL ntp_offset: NTP query to pool.ntp.org failed` — check egress firewall; allow UDP 123 outbound or pick a different `--ntp-server`.
- `FAIL run_state_intact: run_ledger.json not found` — Step 1 didn't complete or `--run-dir` is wrong.
- `FAIL no_halt_flag: halt.flag set by prior day` — a prior dry-run attempt left the flag. Inspect the flag's contents (`cat run_dir/halt.flag`) before deleting.

If any check fails, **stop the dry run**. The morning gate must be green before placing any orders, even fake ones.

## Step 3 — Read-only run

Start the runner with `--readonly` (Phase C-2 flag). The runner connects
to IB Gateway, subscribes to the SPY 5-second bar stream (aggregated to
1-min, consolidated to 15-min), and runs the strategy through the day —
but never submits an order. Decisions, indicators, and bars all land in
the artifact parquets exactly as they would in a live run; the
difference is that `--readonly` short-circuits `place_order`.

```bash
podman exec polygon-data-service python -m app.engine.live.run start \
  --run-dir /app/artifacts/live_runs/<run_id> \
  --readonly
```

Run this from market open (09:30 ET) through close (16:00 ET), or for at
least one full session if doing the dry run on a non-trading day with
historical replay.

Expected runtime behavior:
- IB Gateway shows one connected client (id=42).
- `decisions.parquet` grows by one row every 15 minutes (consolidated
  bar close).
- `executions.parquet` stays empty (read-only mode).
- The strategy logs at least one ENTER and one EXIT signal during the
  session if the EMA crossover triggers.

If a halt rule trips intra-day (rare in dry-run mode), the runner stops
and writes `halt.json` under the run directory. Inspect, fix, restart.

## Step 4 — End-of-session reconciliation

After force-flat (or end of session in dry-run mode), invoke the daily
reconciliation. This compares the runner's decisions against a
synthetic QC export (real QC paper runs only land in paper week proper)
to verify the reconcile pipeline works end-to-end.

For the dry run, build a tiny synthetic QC export that mirrors the
runner's own decisions (so the comparison should classify everything as
`none`):

```bash
mkdir -p /tmp/qc-dry-run/2026-05-04
# Hand-craft indicators.csv from the runner's decisions.parquet — see
# the reconcile.py docstring for the exact column set. Or, easier: run
# the reconcile module on the runner's own output as both sides for a
# self-consistency check. Either way, no QC Cloud involvement on dry-run day.
```

Then:

```bash
podman exec polygon-data-service python -m app.engine.live.reconcile \
  --run-dir /app/artifacts/live_runs/<run_id> \
  --qc-dir /tmp/qc-dry-run/2026-05-04 \
  --docs-dir /workspace/docs/references/reconciliations/dry-run-2026-05-04 \
  --run-label dry-run-2026-05-04 \
  --day-n 0 \
  --day-date 2026-05-04
```

Expected outcome:
- All four artifacts written (`day-0.parquet`, `day-0.json`,
  `day-0.hashes.json`, `day-0.md`).
- Markdown shows `Halt triggered for next session: no` and zero
  cross-engine `engine` divergences.
- No `halt.flag` written.

Inspect the Markdown by hand. The day-0 receipt is your dry-run
deliverable; commit it to the docs tree as evidence the pipeline ran.

## Step 5 — Phases 1–7 regression check

Re-run the existing live-engine test suite on the run-start commit to
verify nothing regressed:

```bash
podman exec polygon-data-service python -m pytest tests/engine/live/ -v
```

Expected: every prior test passes; the new Phase A consumers may skip
(if QC exports aren't yet committed) but should not error.

## Success criteria

The dry run is **green** when all of:

- [ ] Step 1 wrote a ledger with a 64-char hex `run_id`.
- [ ] Step 2 emitted only `OK` lines.
- [ ] Step 3 ran for one full session without intra-day halt.
- [ ] Step 4 produced `day-0.md` showing `Halt triggered for next session: no`.
- [ ] Step 5 saw no test regressions vs the prior master baseline.
- [ ] No `halt.flag` left behind in the run directory.

## What changes for paper week

When Phase D is green and you're ready for paper week:

1. Flip `IBKR_READONLY=false` in `.env`.
2. Re-run Step 1 (a new run_id — different live_config means different
   identity; that's intentional per §10).
3. Run Step 2 every morning before market open. If it halts, do not
   start the runner.
4. Run Step 3 each day during market hours. The fatal-halt rules in
   §7 are now active — broker-state divergence stops the run and
   writes `poisoned.flag`.
5. Run Step 4 after force-flat each day. The receipt commits to git as
   `docs/references/reconciliations/spy-ema-crossover-paper-2026-XX/day-N.md`.
6. After 15 RTH days, run the week rollup (`reconcile.py` produces a
   `week.md`) and add the `live-runtime` row to
   [`docs/math-sources-of-truth.md`](../math-sources-of-truth.md).

## Where things live

| Artifact | Path |
|---|---|
| Run ledger | `PythonDataService/artifacts/live_runs/<run_id>/run_ledger.json` |
| Decisions parquet | `PythonDataService/artifacts/live_runs/<run_id>/decisions.parquet` |
| Executions parquet | `PythonDataService/artifacts/live_runs/<run_id>/executions.parquet` |
| Trades parquet | `PythonDataService/artifacts/live_runs/<run_id>/trades.parquet` |
| Day-N reconcile JSON | `PythonDataService/artifacts/live_runs/<run_id>/reconcile/day-N.json` |
| Day-N reconcile parquet | `PythonDataService/artifacts/live_runs/<run_id>/reconcile/day-N.parquet` |
| Day-N hashes sidecar | `PythonDataService/artifacts/live_runs/<run_id>/reconcile/day-N.hashes.json` |
| Day-N committed Markdown | `docs/references/reconciliations/<run_label>/day-N.md` |
| QC daily exports | `PythonDataService/artifacts/qc/<YYYY-MM-DD>/{trades,indicators}.csv` |
| Halt flag (next-session gate) | `PythonDataService/artifacts/live_runs/<run_id>/halt.flag` |
| Poisoned flag (fatal intra-day) | `PythonDataService/artifacts/live_runs/<run_id>/poisoned.flag` |

`PythonDataService/artifacts/` is gitignored (per the existing
`.gitignore`); only the daily Markdown receipts under `docs/` are
committed.

## When something goes wrong

- **Dry run halts on `dirty-tree`.** Don't `git stash` to make it pass —
  a dirty tree means `code_sha` doesn't identify the running code.
  Either commit the changes (and re-run from Step 1 with a new
  `run_id`) or revert them.
- **Dry run halts on `ntp_offset`.** Don't pass `--skip-ntp` in the
  dry run unless you're explicitly testing the skip path. Fix the
  clock or the network.
- **Reconcile sees engine-class divergence** in the dry run with
  self-consistent inputs. That's a real bug in the reconciliation
  classifier — file an issue and stop. Don't proceed to paper week.
- **IB Gateway disconnect mid-session.** The runner reconnects with a
  60-second timeout; on timeout it halts and writes a partial
  reconciliation. Resume requires a new `run_id` per §7.2 #5.

## Related docs

- [Spec — Path C deployment design](../superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md)
- [QC shadow operator workflow](../../references/qc-shadow/README.md)
- [Repo CLAUDE.md guiding principles](../../CLAUDE.md) (sovereignty over the math; references are eliminated as runtime deps)

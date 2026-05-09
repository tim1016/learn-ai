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
- [ ] The compose file mounts `./PythonDataService/artifacts:/app/artifacts:z`
      (the existing compose only mounts `./PythonDataService/app:/app/app`,
      so the runner's parquets would otherwise vanish on container
      restart and not be visible to the host-side reconcile in Step 4).
      This mount is added by the Phase C-2b compose-config PR; for now,
      add it manually to `compose.yaml` under the `python-service`
      `volumes:` block before the dry run.

## A note on where commands run

This runbook is a mix of **host-side** commands (most of it) and one
**container-side** command (Step 3 only). The reasons are:

- `init-ledger`, `pre-flight`, and `reconcile` need git access (clean-tree
  refusal, code_sha capture) and visibility into the full repo
  (`references/qc-shadow/`, `docs/references/reconciliations/`,
  `PythonDataService/artifacts/`). Only the host satisfies all of those.
- The `polygon-data-service` container compose-mounts only
  `./PythonDataService/app:/app/app:z` — `tests/`, `references/`, and
  `docs/` are NOT visible inside the container. Running the CLI from
  there fails with missing-path errors.
- The host has its own Python venv at `PythonDataService/.venv/` with
  the same dependencies as the container; activate it before each
  command.

Step 3 (`start --readonly`, the long-running broker connection) is the
exception — it runs in the container because the IBKR Gateway sidecar
is on the same network. That step uses paths under `/app/...` because
those *are* visible to the container.

```bash
# Activate the venv once per shell. All host commands below assume it's active.
cd C:/Users/inkan/Documents/learn-ai/PythonDataService
source .venv/Scripts/activate    # Git Bash on Windows
# or:  .venv\Scripts\Activate.ps1 (PowerShell)
# or:  source .venv/bin/activate  (Linux/macOS)
cd ..
```

## Step 1 — Initialize the dry-run ledger (host)

Build the run ledger. This writes
`PythonDataService/artifacts/live_runs/<run_id>/run_ledger.json` and
records the run identity (§10) — strategy spec hash, QC audit copy
hash, account id, start-of-session UTC ms.

```bash
python -m app.engine.live.run init-ledger \
  --repo-root . \
  --clean-tree-scope PythonDataService references/qc-shadow \
  --strategy-spec-path PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json \
  --qc-audit-copy-path references/qc-shadow/SpyEmaCrossoverAlgorithm.py \
  --qc-cloud-backtest-id <PASTE FROM QC CLOUD UI> \
  --account-id DU<your-paper-account> \
  --start-date-ms $(date -u -d "today 09:30 EDT" +%s000) \
  --live-config-json '{"symbol":"SPY","force_flat_at":"15:55","client_id":42}' \
  --run-root PythonDataService/artifacts/live_runs
```

Run from the repo root. The `python` invocation needs `PythonDataService/`
on `sys.path`; either run from that directory (and adjust paths) or
prepend `PYTHONPATH=PythonDataService` to the command.

Expected outcome:
- Exit 0
- Stdout: `[INIT-LEDGER] wrote PythonDataService/artifacts/live_runs/<sha>/run_ledger.json (run_id=<sha>)`

Failure modes to expect:
- **Dirty tree** → exit 1, message "dirty-tree halt: working tree has N uncommitted change(s)…". Fix: commit or stash within scope.
- **Missing strategy_spec_path / qc_audit_copy_path** → exit 2.
- **Existing run dir** → exit 2 (rare; only happens if you already initialized identical inputs today). Pass `--force` only if you genuinely want to overwrite.

Record the resulting `run_id`; you'll use it in subsequent steps.

## Step 2 — Pre-flight gate (morning gate, host)

Run all the `pre_flight.py` halt checks. This is the same code path
that fires every morning during paper week (§6.4).

```bash
PYTHONPATH=PythonDataService python -m app.engine.live.run pre-flight \
  --repo-root . \
  --clean-tree-scope PythonDataService references/qc-shadow \
  --run-dir PythonDataService/artifacts/live_runs/<run_id>
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

## Step 3 — Read-only run (container)

Start the runner with `--readonly` (Phase C-2 flag). The runner connects
to IB Gateway, subscribes to the SPY 5-second bar stream (aggregated to
1-min, consolidated to 15-min), and runs the strategy through the day —
but never submits an order. Decisions, indicators, and bars all land in
the artifact parquets exactly as they would in a live run; the
difference is that `--readonly` short-circuits `place_order`.

This command runs in the container (where the IBKR Gateway sidecar
network is reachable). The artifact parquets it writes land at
`/app/artifacts/live_runs/<run_id>/` inside the container, which is
the same path as `PythonDataService/artifacts/live_runs/<run_id>/`
on the host via the existing `./PythonDataService/app:/app/app`
compose mount.

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

## Step 4 — End-of-session reconciliation (host)

After force-flat (or end of session in dry-run mode), invoke the daily
reconciliation. This compares the runner's decisions against a
synthetic QC export (real QC paper runs only land in paper week proper)
to verify the reconcile pipeline works end-to-end.

For the dry run, build a tiny synthetic QC export that mirrors the
runner's own decisions (so the comparison should classify everything as
`none`):

```bash
mkdir -p PythonDataService/artifacts/qc-dry-run/2026-05-04
# Hand-craft indicators.csv from the runner's decisions.parquet — see
# the reconcile.py docstring for the exact column set. Or, easier: run
# the reconcile module on the runner's own output as both sides for a
# self-consistency check. Either way, no QC Cloud involvement on dry-run day.
```

Then run reconcile from the host (no IBKR access needed, and the host
has visibility into the docs/ tree where the day Markdown lands):

```bash
PYTHONPATH=PythonDataService python -m app.engine.live.reconcile \
  --run-dir PythonDataService/artifacts/live_runs/<run_id> \
  --qc-dir PythonDataService/artifacts/qc-dry-run/2026-05-04 \
  --docs-dir docs/references/reconciliations/dry-run-2026-05-04 \
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

## Step 5 — Phases 1–7 regression check (host)

Re-run the existing live-engine test suite on the run-start commit to
verify nothing regressed. Run from the host because the container only
mounts `app/` — `tests/` is not visible inside it.

```bash
cd PythonDataService
python -m pytest tests/engine/live/ -v
cd ..
```

Expected: every prior test passes; some Phase A consumers and
git-required tests may skip (the former waits on QC exports, the
latter on the `git` binary — both are valid skip conditions, not
failures).

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

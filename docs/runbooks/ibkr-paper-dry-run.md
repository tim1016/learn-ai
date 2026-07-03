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
- [ ] The polygon-data-service container is running (`podman ps`) **if**
      you want to verify Gateway connectivity through `/api/broker/health`
      and `/api/broker/diagnose` before Step 3. Step 3 itself runs
      host-side and does not require the container, but the diagnose
      endpoints are a convenient pre-flight sanity check.

## A note on where commands run

**Every step runs on the host** (host venv). Earlier revisions of this
runbook directed Step 3 to the `polygon-data-service` container; that
no longer works. IBKR Gateway enforces same-IP binding on real-time
bar subscriptions: the API client's source IP must match the Gateway's
login session IP, or the `reqRealTimeBars` call returns error 420
("Invalid Real-time Query: Trading TWS session is connected from a
different IP address"). The container connects from the WSL bridge
subnet (`10.89.0.x`) which differs from the Gateway's login IP, so RT
bars are silently rejected even though the API connection succeeds.
Running on the host means the API client and the Gateway share an IP,
and the binding check passes.

Other reasons every other step is host-side:

- `init-ledger`, `pre-flight`, and `reconcile` need git access
  (clean-tree refusal, code_sha capture) and visibility into the full
  repo (`references/qc-shadow/`, `docs/references/reconciliations/`,
  `PythonDataService/artifacts/`). Only the host satisfies all of those.
- The `polygon-data-service` container compose-mounts only
  `./PythonDataService/app:/app/app:z` — `tests/`, `references/`, and
  `docs/` are NOT visible inside the container. Running the CLI from
  there fails with missing-path errors.
- The host has its own Python venv at `PythonDataService/.venv/` with
  the same dependencies as the container; activate it before each
  command.

```bash
# Activate the venv once per shell. All host commands below assume it's
# active and that the shell's working directory is the repo root.
cd PythonDataService
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
PYTHONPATH=PythonDataService python -m app.engine.live.run init-ledger \
  --repo-root . \
  --clean-tree-scope PythonDataService references/qc-shadow \
  --strategy-spec-path PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json \
  --qc-audit-copy-path references/qc-shadow/SpyEmaCrossoverAlgorithm.py \
  --qc-cloud-backtest-id <PASTE FROM QC CLOUD UI> \
  --account-id DU<your-paper-account> \
  --start-date-ms $(date -u -d "today 09:30 EDT" +%s000) \
  --live-config-json '{"symbol":"SPY","force_flat_at":"15:55"}' \
  --run-root PythonDataService/artifacts/live_runs
```

Run from the repo root. `app` lives under `PythonDataService/app`, so the
`PYTHONPATH=PythonDataService` prefix is what makes `python -m
app.engine.live.run` resolvable from the repo root (same pattern as
Steps 2 and 4 below).

**Note on `--live-config-json`:** the JSON object must contain only
`LiveConfig` fields (`symbol`, `force_flat_at`, `consolidator_period_min`,
`run_dir`, `max_submit_latency_ms`). Do **not** include broker fields
like `client_id`, `host`, `port`, `mode` — those belong to `IbkrSettings`
and come from `.env` / env vars. The `start` subcommand (Step 3) reads
the ledger and calls `_live_config_from_ledger`, which strict-rejects
unknown keys with **exit 2** ("could not apply ledger.live_config to
LiveConfig: unknown live_config keys: ['client_id']"). The example
above is the minimum the operator should pass.

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

## Step 3 — Read-only run (host)

Start the runner with `--readonly` (Phase C-2 flag). The runner connects
to IB Gateway, subscribes to the SPY 5-second bar stream (aggregated to
1-min, consolidated to 15-min), and runs the strategy through the day —
but never submits an order. Decisions, indicators, and bars all land in
the artifact parquets exactly as they would in a live run; the
difference is that `--readonly` short-circuits `place_order`.

This command runs on the host (host venv). See "A note on where
commands run" above for why container-side `start` does not work
(IBKR error 420 — same-IP binding on RT bars).

### Optional UI start via host daemon

Instead of paste-running the `start` command by hand, launch the host
daemon once from the repo root:

```bash
./start-live-daemon.sh --background
```

`start-live-daemon.sh` passes `--env-file .env` to the daemon. The daemon
loads only `IBKR_HOST_ALLOWLIST` and `IBKR_HOST` from that file, and any
already-exported process env value wins. To prove what a local launch will
feed into the daemon policy without stopping or starting a daemon, run:

```bash
./start-live-daemon.sh --print-launch-env
```

The browser/data-plane request path validates only that `ibkr_host` is a
bare host name or IP address. The host daemon is the authority that decides
whether that host is allowed, using `IBKR_HOST_ALLOWLIST` plus `IBKR_HOST`.

On Windows/Mac podman, a direct daemon launch can stay bound to its default
`127.0.0.1:8765` because the container reaches it via
`host.containers.internal` → host loopback (gvproxy). `start-live-daemon.sh`
binds `0.0.0.0` so Linux rootless podman works too; in that topology the alias
maps to the bridge gateway, which cannot reach loopback.

The daemon authenticates every protected route with a mandatory
`X-Live-Runner-Token` shared secret (ADR 0007), so binding `0.0.0.0` is
safe. The token is auto-generated at startup to
`PythonDataService/artifacts/.host-daemon-token` (`0o600`), which the
container reads through the `./PythonDataService/artifacts:/app/artifacts`
bind mount — no manual sync. To pin your own secret instead, set
`LIVE_RUNNER_DAEMON_TOKEN` on **both** the daemon process and the
`polygon-data-service` container. A direct `curl` against the daemon now
needs `-H "X-Live-Runner-Token: $(cat PythonDataService/artifacts/.host-daemon-token)"`.

Then open `/broker/bots`, choose the bot, and use the **Host Runner** panel. It calls the
daemon at `http://127.0.0.1:8765` and starts/stops the same
`app.engine.live.run start` subprocess from the host. The run artifacts
still land under `PythonDataService/artifacts/live_runs/<run_id>/`, so
the existing observer panels remain the diagnostic source of truth.

If you want the observer UI available while the host runner owns IBKR,
keep `python-service` up with `IBKR_BROKER_ENABLED=false`; that prevents
the container lifespan client from taking an IBKR client id while still
serving `/api/live-runs`.

**Before launching through the manual CLI path, stop the
`polygon-data-service` container** so its lifespan `IbkrClient` releases
`client_id=42`:

```bash
podman compose stop python-service
```

The dry-run validates the spec § 5 single-client operating mode (one
API client owning `client_id=42`). If you leave the container up, its
lifespan client also holds an IBKR connection — operator-facing
diagnostics work, but the run no longer certifies single-client mode
and the dry-run client either collides on `client_id=42` (IBKR error
326) or is forced onto a different id, breaking the invariant
Prerequisites declared. Stop the container; restart it with
`podman compose start python-service` after Step 4 if you want the
broker REST endpoints back for post-mortem inspection.

One env-var override is required at the command line:

- **`IBKR_HOST=127.0.0.1`** — the `.env` default of `172.23.176.1` is
  the *container's* view of the Windows host bridge; from the host
  itself, Gateway listens on loopback. Pydantic-settings prioritizes
  process env vars over the `.env` file, so the inline override wins.
  `IBKR_CLIENT_ID` does **not** need an override: the `.env` default
  `42` is exactly what the run is supposed to use, and with the
  container stopped the id is free.

```bash
IBKR_HOST=127.0.0.1 \
  PYTHONPATH=PythonDataService python -m app.engine.live.run start \
  --run-dir PythonDataService/artifacts/live_runs/<run_id> \
  --readonly
```

Run this from market open (09:30 ET) through close (16:00 ET), or for at
least one full session if doing the dry run on a non-trading day with
historical replay.

**Windows note** — Phase 8's SIGINT/SIGTERM signal handlers use
`loop.add_signal_handler`, which is unsupported by Windows's asyncio
event loop. The runner emits two warnings at startup
(`Signal handler for SIGINT not supported on this event loop (Windows
host?); graceful shutdown via this signal disabled.`) and falls back
to `KeyboardInterrupt`. Operators stop the run with **Ctrl+C** in the
terminal; `asyncio.run` translates it to `CancelledError`, which
propagates through `engine.run`'s `finally` block — writers flush and
the IbkrClient disconnects cleanly. (On Linux / container hosts where
this Windows constraint doesn't apply, SIGINT/SIGTERM trigger the
graceful `shutdown_event` path instead.)

Expected runtime behavior:
- IB Gateway shows one connected client (id=42).
- `live.log` grows by one `[BAR]` heartbeat line every minute, of the
  form `[BAR] <iso-time> consolidator_emitted=<int> snapshot=<set|None>`.
  This is the operator's primary signal that the engine is alive — tail
  it (`tail -f live.log | grep '\[BAR\]'`) to confirm bars are flowing.
- `decisions.parquet` **stays empty during indicator warmup** and only
  starts growing once every indicator the strategy uses is `is_ready`.
  For `SpyEmaCrossoverAlgorithm` this requires
  ``max(EMA5=5, EMA10=10, RSI14=15) = 15`` consolidated 15-min bars —
  RSI's `is_ready` predicate requires `samples >= period + 1` (one
  extra sample for the first delta; see `app/engine/indicators/rsi.py`).
  The first row appears ≈ 3 h 45 m after a fresh-state run starts.
  Until then, `[BAR] ... snapshot=None` is normal and expected. After
  warmup, `decisions.parquet` grows by one row every 15 minutes
  (consolidated bar close).
- `executions.parquet` stays empty (read-only mode).
- The strategy logs at least one ENTER and one EXIT signal during the
  session if the EMA crossover triggers (only possible after warmup).

How to distinguish "engine alive, strategy in warmup" from "engine hung":
the `[BAR]` heartbeat above. If `live.log` shows new `[BAR]` lines every
minute, the engine is fine — `decisions.parquet` being empty just means
the strategy is still warming up. If `[BAR]` lines stop arriving, *that*
is a hang and the operator should investigate (issue #227 was a
misdiagnosis caused by the absence of this heartbeat — see issue #228).

For a meaningful single-day dry run that produces decision rows in
`decisions.parquet`, either (a) start by 05:45 ET so warmup completes
before RTH open at 09:30 ET, (b) accept that day 1 produces no decisions
and rely on the `[BAR]` heartbeat for end-to-end pipeline verification,
or (c) wait for indicator-state-persistence-across-restarts to ship
(tracked separately) so day 2+ skips warmup.

If a halt rule trips intra-day (rare in dry-run mode), the runner stops
and writes `halt.json` under the run directory. Inspect, fix, restart.

### Hydrate policy

The `start` subcommand reads / validates the prior session's indicator
state sidecar before consuming any bars. Three modes:

| Flag                      | Behavior                                                       | When to use |
|---|---|---|
| `--hydrate-policy require` (default) | Validate sidecar; exit 4 on missing/stale/mismatched/unready/non-flat | B2 dry-run gate and paper week (the default — no flag needed) |
| `--hydrate-policy optional` | Cold-start when sidecar missing/invalid; write at end-of-session | Seed day (Monday of paper week, or first-ever run) |
| `--hydrate-policy disabled` (alias: `--allow-cold-start`) | Never read sidecar; still write at end-of-session | Operator escape hatch ("I know yesterday's state is bad, warmup from scratch today") |

On exit 4 under require, inspect `<run_dir>/indicator_state_hydration.json`
for the failure reason (one of: `disabled_by_operator`, `missing`,
`schema_mismatch`, `identity_mismatch`, `calendar_stale`,
`payload_mismatch`, `indicators_unready`, `lifecycle_not_flat`).

State lives at `PythonDataService/artifacts/live_state/<strategy_key>/<symbol>_<period>m.json`
(e.g., `PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json`)
and is keyed by identity-tuple — every `run_id` reads / writes the same
file, so day-over-day continuity is automatic.

## Step 3a — Seed day (first-ever run)

The very first paper-week run (or the first-ever dry-run attempt) has no prior sidecar. Use the `optional` policy so the runner cold-starts instead of exiting 4:

```bash
IBKR_HOST=127.0.0.1 \
  PYTHONPATH=PythonDataService python -m app.engine.live.run start \
  --run-dir PythonDataService/artifacts/live_runs/<run_id> \
  --readonly \
  --hydrate-policy optional
```

The runner cold-starts (warmup takes ~3 h 45 m for `SpyEmaCrossoverAlgorithm`),
writes its first sidecar at 15:55 ET force-flat completion. From day 2 onward,
`--hydrate-policy require` (the default; no flag needed) accepts that sidecar
and skips warmup.

## Step 4 — End-of-session reconciliation (host)

After force-flat (or end of session in dry-run mode), invoke the daily
reconciliation. This compares the runner's decisions against a
synthetic QC export (real QC paper runs only land in paper week proper)
to verify the reconcile pipeline works end-to-end.

Build a tiny synthetic QC indicators export that mirrors the runner's
own decisions (so the cross-engine class on every bar is `none`):

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

### Expected outcome

- All four artifacts written (`day-0.parquet`, `day-0.json`,
  `day-0.hashes.json`, `day-0.md`).
- Markdown shows zero cross-engine `engine` divergences.
- The `Halt triggered for next session:` line will read `no` *only if*
  the strategy didn't fire any ENTER/EXIT signals during the dry-run
  window. **If the strategy did emit a signal, expect a `fill-class
  breach count=N` halt** — Step 3 ran in `--readonly` mode so no broker
  fills came back to match those intents. This is **expected behavior
  in dry-run mode, not a bug**:
  - the receipt and `halt.flag` are still well-formed and the dry run
    is a pass on the pipeline-correctness criterion;
  - delete the `halt.flag` before any subsequent `pre-flight` gate (or
    the next morning would refuse to proceed because of it).

If you want a clean `no halt` receipt as the dry-run output:
1. Either run Step 3 over a too-short window for the strategy to
   emit any signals (warmup-only); or
2. Add synthetic `executions.parquet` rows that match each ENTER/EXIT
   decision (one execution per signal, `fill_price` ≈ `intended_price`,
   `client_order_id` of the form `live-N`).

Inspect the Markdown by hand. The day-0 receipt is your dry-run
deliverable; commit it to the docs tree as evidence the pipeline ran.

A worked example committed to the repo lives at
`docs/references/reconciliations/dry-run-2026-05-09/day-0.md` — that
file shows the expected fill-class breach when a synthetic ENTER
signal has no matching execution, and is what your output should
roughly look like.

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

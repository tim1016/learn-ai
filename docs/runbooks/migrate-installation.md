# Move the installation to another Mac — operator runbook

**Status:** Procedure for a planned move of the whole dev installation (both
lanes, the registry, the database, the lake and the artifacts) from one Mac to
another, with every account flat. Recovering from a dead machine is out of
scope. Windows is tracked separately in #2272.

**Authority:** the owner decisions on
[#2151](https://github.com/tim1016/learn-ai/issues/2151) (grill 2026-09-22) and
[#2269](https://github.com/tim1016/learn-ai/issues/2269) (grill 2026-09-23);
[ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md) for the
fleet, including its
[provider decision](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16):
IBKR supplies live bars and Alpaca handles accounts and orders. The tool is
`PythonDataService/scripts/migrate_installation.py`.

## What moves, and what does not

The bundle is one file. It carries the five podman volumes
(`learn-ai_pgdata`, `learn-ai_alpaca-fleet-control`,
`learn-ai-alpaca-clerk-data`, `learn-ai-alpaca-paper-clerk-data`,
`learn-ai-alpaca-clerk-qualification-data`), plus `data-lake-volume/`,
`PythonDataService/artifacts/`, `PythonDataService/cache/` and
`PythonDataService/lean-cache/`.

It never carries a secret:

- You copy the env files by hand: `deploy/fleet/env/*.env`, the repo-root
  `.env` and `PythonDataService/.env`. Never commit them, never paste their
  values anywhere, and never send them over email or chat.
- Export **skips** every secret-shaped file inside a bundled folder and lists
  each one with a note. The three files on the owner's machine need nothing
  from you:
  - `.launcher-token`: the new host mints a fresh one (`ensure_launcher_token`).
  - `.host-daemon-token`: a retired leftover that nothing reads.
  - `.clerk-host-binding-capability`: a retired leftover that nothing reads.
  - Anything else on the list says "copy it by hand only if the new host needs
    it". Decide that before you shut the old machine down.

**Flat accounts only.** Export refuses unless every account has no open
position, no working order (a resting GTC counts), and no in-flight intent. It
never flattens or cancels anything for you.

## Before you start

On the **new** Mac:

1. Install Podman and clone the repo. Check out the same commit as the old
   machine, or a newer one. Import refuses older code, and it refuses a commit
   it does not know, so `git fetch` first.
2. Set up and log in to IB Gateway exactly as on the old machine
   ([IBKR setup guide](ibkr-setup-guide.md)). Go-live checks that every lane
   gets bars through it.
3. Leave the stack **down**. Import refuses while any container uses a bundled
   volume. If a stack already ran on this Mac, remove its containers but keep
   the volumes: `podman compose -f compose.yaml -f compose.fleet.dev.yaml down`
   (never `-v`). Import moves any existing volumes aside itself.
4. Make sure the disk has room: about three times the bundle, plus the lane
   volumes once more, plus a copy of anything already there. Import checks this before it touches anything.

On the **old** Mac:

1. Close every position and cancel every working order at the broker.
2. Make sure the clerks run this code, not an older copy. Export needs each
   lane's stop receipt to include `intent_stopped`, and a clerk still running
   older code leaves it out, so export refuses. Pull, then restart both clerks
   (their code is bind-mounted, so a restart loads it):

   ```bash
   git pull
   podman restart alpaca-live-clerk alpaca-paper-clerk
   ```

   Wait until both are healthy (`podman ps`) before you run the check below.

## 1. Old machine: check that every account is flat

From `PythonDataService/`, with the host venv and the stack running:

```bash
python -m scripts.migrate_installation export --check
```

This stops nothing and writes nothing. Exit `0` means every account is flat. On
exit `2` with `accounts_not_flat`, the output names the account and what is
still open. Close that at the broker, then check again.

## 2. Old machine: export

```bash
python -m scripts.migrate_installation export \
    --bundle ~/learn-ai-YYYY-MM-DD.tar \
    --operator <you> --change-ref migrate-YYYY-MM-DD
```

Each step prints one JSON line. In order, export:

1. Refuses a dirty checkout. `--allow-dirty-tree` overrides this, and the
   override is recorded.
2. Lists the secret-shaped files it will skip (`skipped_secret_files`).
3. Checks every account is flat **before** it stops any bot.
4. Stops every bot on every lane and writes a stop receipt on each lane's
   volume. A bot that was not running but whose saved state still said
   "running" (or "paused") is also set to stopped. It is listed in the receipt
   under `intent_stopped`.
5. Checks every account again, stops the containers, and writes the bundle.

It never drains a lane and never changes an assignment. Bots stay stopped
whatever happens next.

## 3. Copy by hand, then shut the old machine down

Copy these to the new Mac directly (for example USB or `scp`), into the same
paths in the new checkout:

- the bundle file;
- `deploy/fleet/env/*.env`;
- the repo-root `.env`;
- `PythonDataService/.env`.

Then **shut the old machine down** and leave it off. Export does not lock the
old machine, so keeping it off is your job. Never start its stack again. See
"Going back" below.

## 4. New machine: import

```bash
python -m scripts.migrate_installation import --bundle ~/learn-ai-YYYY-MM-DD.tar
```

Before it changes anything, import checks:

- the env files are present;
- the code is the same commit or newer;
- the Postgres major version matches;
- the stack is down, and there is enough disk;
- every bundle member's hash;
- the registry, markers and generations.

It then moves any existing data aside to `<checkout>-migration-aside/import-<ms>/`
(it never deletes anything), restores everything, and re-verifies. The stack
stays down.

Import also puts every lane **on hold for go-live**. It writes
`go-live-pending.json` at each clerk volume's root, and while that file exists
the lane refuses every bot start with reason code `LANE_GO_LIVE_PENDING`. Only
bot starts are held. Manual trading stays off, as it already is.

Read the `host-resolution` line. It compares where the old machine mounted each
lane's volume, and under which namespace, with the new machine. A lane appears
under `reapproval_required` only when that changed. A registry value that never
matched the old machine (Paper's `/paper-volume`) is carried over as it was and
is not reported. If a lane is listed, re-approve it before go-live:

```bash
podman exec polygon-data-service python -m scripts.manage_broker_fleet approve-endpoint \
    --control-dir /app/artifacts/fleet --clerk-id <clrk_…> \
    --endpoint-ref <endpoint ref> --base-url http://<clerk service>:8000
```

Import writes its receipt at `<aside>/import-receipt.json`. If it stops
part-way (`restore_incomplete`), do not start the stack. The message and
`<aside>/import-incomplete.json` name exactly what changed and where the
preserved data is. Fix the cause and run import again.

## 5. New machine: bring the stack up

```bash
./restart.sh
```

The clerks boot on the restored volumes. Every bot is stopped, and every lane
still refuses bot starts until go-live.

## 6. New machine: go-live

```bash
python -m scripts.migrate_installation go-live \
    --operator <you> --change-ref migrate-YYYY-MM-DD
```

Go-live runs three steps in this order:

1. **Bars on every lane.** Each lane asks its own IB Gateway for recent
   *historical* SPY bars, so this works outside market hours too. A lane passes
   only if at least one real bar comes back. If any lane fails, nothing is
   released:

   | Reason | Meaning | Fix |
   |---|---|---|
   | `ibkr_gateway_unreachable` | The lane has no IB Gateway connection, or it lost connectivity (code 1100) | Start or log in to IB Gateway, then check `IBKR_HOST`/`IBKR_PORT` ([setup guide](ibkr-setup-guide.md)). Never disable the IBKR feed. |
   | `ibkr_no_bars` | Connected, but no bars came back | Check the Gateway's market-data permissions and session |
   | `ibkr_bar_check_timed_out` / `ibkr_bar_check_failed` | The request timed out or was refused | Read the named cause; retry |

2. **Your confirmation.** Only once every lane has passed does go-live ask you,
   on the terminal, to type exactly `the old machine is off`. Anything else
   refuses (`old_machine_off_not_confirmed`) and releases nothing.
3. **Release.** Every lane removes its hold and writes a receipt under
   `go_live_receipts/` on its volume.

The lane enforces both steps itself. A release refuses without the
confirmation words, and it also refuses unless that lane passed a bar check in
the last 15 minutes. If you took longer than that to type the confirmation,
run go-live again.

If a release fails part-way, the output names the lanes already released and
the ones still held. Running go-live again is safe: a lane that is already
released just records another release.

## 7. Start the bots yourself

Go-live starts nothing. Open each account's **Bots** tab and start the bots
you want, one by one, as usual.

## Going back

**Going back is a reverse migration** with the same checks: export on the new
machine, import on the old one, then go-live on the old one. That applies even
if the new machine never went live. Its bundle carries the hold, and the
import writes a fresh one.

**Never restart the stale old copy.** Its volumes stopped at export time, while
the accounts, the registry and the broker have moved on. Starting it puts a
second writer, with out-of-date custody evidence, on the same accounts.

## Where the evidence lives

| What | Where |
|---|---|
| Export stop receipts (who, what stopped, `intent_stopped`) | `lane_stop_all_receipts/` on each lane volume (travels in the bundle) |
| Bundle manifest (commit, identities, `skipped_secret_files`, old host's mounts) | `manifest.json`, the bundle's first member |
| Import receipt / partial receipt | `<checkout>-migration-aside/import-<ms>/` |
| Go-live hold | `go-live-pending.json` at each clerk volume's root, until go-live |
| Go-live release receipts (bars proven, confirmation, marker released) | `go_live_receipts/` on each lane volume |

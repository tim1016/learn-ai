# Alpaca accounts: setup, launch, removal, and recovery

This is the operator entry point for the local two-lane fleet. Run terminal
commands from the repository root. Stop at any failed command and use the
troubleshooting table; a healthy container alone does not mean a usable account.

**Permanent provider boundary: IBKR supplies live market data. Alpaca supplies
account reads, orders, and execution reports.** Paper and Live Shadow both need
IB Gateway. Do not buy or enable Alpaca market data, disable IBKR to hide an
outage, or restore the retired IBKR bot-control pages. This is the owner's
2026-09-16 decision, recorded in [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16).

## Choose the procedure

| What you want | Follow |
|---|---|
| Open the roster or diagnose a disabled launch | [Check the current accounts](#1-check-the-current-accounts) |
| Find account details, Trader/Operator views, or buy/sell history | [Navigation guide](#find-the-roster-account-details-and-transactions-in-the-menu) |
| Add/replace credentials for the account already assigned to a lane | [Account setup](#2-account-setup) |
| Add an additional account alongside existing accounts | [Provision another lane](#3-provision-another-lane) |
| Recover Paper after a developer reset or activate a fresh Paper account | [Offline Paper activation](#4-offline-paper-activation) |
| Enable a strategy and launch Paper or Live Shadow | [Launch a bot](#5-launch-a-bot) |
| Remove a saved profile, disable a lane, or remove an account | [Removal](#6-removal) |
| Understand a refusal | [Troubleshooting](#7-troubleshooting) |

A **profile** is saved configuration. A **lane** is a dedicated worker and its
persistent volume, serving one effective account. A **bot** is a strategy
instance on that account. Archiving a profile or stopping a container does not
close the brokerage account or erase its trading records.

| Intended behavior | Alpaca mode / credential slot | Expected authority | Real orders? |
|---|---|---|---|
| Paper | `paper` / `default` | `real_paper` | Alpaca paper orders only |
| Live Shadow | `live` / `live` | `shadow` | None; fills are simulated |
| Real-money Live | `live` / `live` | `real_live` | Only after separate cutover and arming |

**Live Shadow does not require real-money custody activation or arming.** If you
want Shadow, stop at `authority: shadow`. Real-money graduation is a separate
procedure governed by [ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md).

## 1. Check the current accounts

1. Open IB Gateway and sign in to the data session. The committed local topology
   expects **Gateway Paper, port 4002**. Keep its API read-only. A Live Alpaca
   account can consume this IBKR Paper-session data; the two brokers' account
   modes are independent. The IBKR login still needs its existing live-data
   entitlement. If using a different Gateway/TWS session, change `IBKR_MODE`
   and its matching port together; never bypass the account sentinel.
2. Open the app at <http://localhost:4200/brokers/alpaca>. Select the intended
   account card to open its workspace, then the **Bots** tab for its roster.
3. In a terminal, run the checks for the lane you intend to use:

   ```bash
   python3 scripts/alpaca_onboarding_gates.py status --mode paper --require roster
   python3 scripts/alpaca_onboarding_gates.py status --mode paper --require deploy --symbol SPY
   ```

   For Live Shadow, replace `paper` with `live`. Replace `SPY` with the exact
   symbol you intend to trade. The deploy check requires `--symbol` and checks
   that symbol's IBKR data and warmup; an account connection alone is insufficient.
   These commands read state;
   they do not start a bot, enable trading, or place orders. They print the
   account, authority, exact roster URL, and any deployment blockers.

**Done:** the roster check says `OK`, and the launch check says `OK` when you
want to deploy. Paper must say `real_paper`; Live Shadow must say `shadow`.
`last_apply_refusal` can describe an earlier rejected change while the existing
account remains ready. Review it before assuming a newly selected profile took
effect. A per-bot Resume decision can still refuse an old or incompatible seal;
symbol-scoped deploy readiness does not certify every existing bot. The command
refuses if Paper is not serving `real_paper`, or Live is not serving `shadow` or
`real_live`, even when you override `--container`. For Shadow, verify the printed
authority is specifically `shadow` before proceeding.

If you just restarted, allow about a minute for the prior lease to expire and
for the new worker to confirm its binding, then run the checks again.

### Find the roster, account details, and transactions in the menu

Open **Alpaca** in the top navigation bar and choose **Accounts** — the one
Alpaca menu item (ADR 0064 Decision 2); it lists every registered account and
does not automatically choose Paper or Live for you. Selecting an account card
opens that account's workspace: one account header over **Overview**, **Bots**,
**Gallery**, and **Configuration** tabs.

| What you want to see | Click path |
|---|---|
| Paper bot roster | **Alpaca → Accounts → Paper card → Bots tab** |
| Live account bot roster (including Shadow bots) | **Alpaca → Accounts → card marked Live → Bots tab** |
| Account balances and details | **Alpaca → Accounts → intended account card**; opens on the **Overview** tab |
| Current holdings and today's buys/sells | On that account's **Overview** tab, the **Trader / Operator** switch defaults to **Trader**; read **Current positions** and **Activity** |
| Orders, executions, and their status | On that account's **Overview** tab, switch to **Operator**, then **Transaction history**; select **Today**, **30D**, or **60D**, then **View details** on a row |
| Longer account history | On that account's **Overview** tab, **Trader**, choose **30D** or **60D** for the portfolio chart and **Transaction history** |
| Connection or order-recovery evidence | On that account's **Overview** tab, switch to **Operator**, then expand **Broker connection** or **Order custody & recovery** |
| Strategy gallery and deploy targets | **Alpaca → Accounts → intended account card → Gallery tab** |
| Saved credentials and account configuration | **Alpaca → Accounts → intended account card → Configuration tab** |

The account workspace opens on **Overview**, whose **Trader / Operator** switch
covers that account's own positions, activity, and transaction history. Each
bot has its own page too, reached from the **Bots** or **Gallery** tab, with the
same **Trader / Operator** switch scoped to that bot. **Activity** reports
broker events; **Transaction history** includes order status and execution
evidence so you can distinguish a requested buy/sell from a completed fill. A
strategy decision that never created an order belongs in that bot's own page,
reached from the **Bots** tab.

The **Live** badge identifies the real-money account endpoint. Check its
**Authority** as well: **Shadow** means simulated fills and no real orders from
that authority. Shadow transaction history shows simulated Clerk records;
**Trader → Today → Activity** shows broker-reported events on the real account.
Opening a roster or desk does not arm a bot.

If the desk says it cannot reach Alpaca, the current UI hides both perspective
tabs until its account read succeeds. This is a loading/identity problem, not
another menu you need to find. Likewise, a transaction-history error is not an
empty trading history; retain the error text when diagnosing it.

## 2. Account setup

Use an existing lane only for its already assigned account, or an unassigned
new lane. Changing a saved profile does not transfer an account assignment.

### Put the credentials in the correct place

| Lane | Container | Secret file | Credential variable names |
|---|---|---|---|
| Paper | `alpaca-paper-clerk` | `deploy/fleet/env/paper.env` | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` |
| Live / Live Shadow | `alpaca-live-clerk` | `deploy/fleet/env/live.env` | `ALPACA_CREDENTIAL_LIVE_KEY_ID`, `ALPACA_CREDENTIAL_LIVE_SECRET_KEY` |

1. Obtain the appropriate existing account's API credentials from the Alpaca
   dashboard. Edit only that lane's gitignored secret file. Preserve its fleet
   identity and transport-token entries. Use the matching `.env.example` only
   when creating a missing file; do not overwrite an existing file with it.
2. Restrict the file's permissions, then recreate **only that lane**. Example
   for Paper (substitute the Live file/container for Live Shadow):

   ```bash
   chmod 600 deploy/fleet/env/paper.env
   COMPOSE_ARGS=(--file compose.yaml)
   if [[ -f compose.fleet.dev.yaml ]]; then COMPOSE_ARGS+=(--file compose.fleet.dev.yaml); fi
   if [[ -f compose.override.yaml ]]; then COMPOSE_ARGS+=(--file compose.override.yaml); fi
   podman compose "${COMPOSE_ARGS[@]}" config --services
   podman compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate alpaca-paper-clerk
   python3 scripts/alpaca_onboarding_gates.py credential-match --mode paper
   ```

   The services listing must include your lane. **Restart does not reload an
   env file; recreate does.** The file list above matches `restart.sh` and
   includes a local override if present. Do not print the full rendered Compose
   configuration: it contains secrets.
3. Check that neither the committed overlay nor your local override disables
   the data feed. Every lane needs `IBKR_BROKER_ENABLED: "true"` and
   `IBKR_READONLY: "true"`; client IDs must differ (default Live `1`, Paper `2`).
   `environment:` overrides an `env_file` value, and the last Compose file can
   override an earlier one. After changing these settings, recreate again.

### Verify, approve, stage, and apply

1. In the account desk, open the selected lane's **Configuration** page.
2. Create/select the profile with the correct mode and credential slot.
3. Click **Verify account**. Compare the returned account number and Paper/Live
   mode with the account you intended. This is a read-only broker check.
4. Click **Approve this account**. This records the account pin. Saving a profile
   or merely verifying it does not replace approval.
5. **Stage** the approved revision, then **Apply** it. Restart that worker:

   ```bash
   podman restart alpaca-paper-clerk
   ```

6. Run the roster check in section 1. If a fresh/reset Paper account reports
   `ACTIVATION_REQUIRED` or `developer_reset_reactivation_required`, continue
   with section 4. A newly bound Live Shadow lane should use `shadow`; do not
   perform the Paper ceremony against it.

**Done:** the intended profile/revision/account is effective, the current
worker confirms it, and `status --require roster` passes. A saved profile,
Apply receipt, or green container health check alone is insufficient.

## 3. Provision another lane

An additional simultaneously served account needs a **new container, a new
named custody volume, a new fleet identity, separate secrets, and another IBKR
client ID**. Never copy an existing lane's identity/volume marker, reuse its
custody volume, or put two accounts in the same worker.

The committed local overlay currently declares two lanes. Adding a third is a
host configuration task, not the **New profile** button. The following example
adds a second **Paper** lane named `alpaca-paper-two`. Use unused names and an
unused IBKR client ID. Do not rerun the historical migration or token rotation
from the older fleet installation notes.

### A. Back up and issue a fresh lane identity

```bash
NEW_SERVICE='alpaca-paper-two'
NEW_VOLUME='learn-ai-alpaca-paper-two-data'
CEREMONY_ROOT="/$NEW_SERVICE-volume"
SAFE="$HOME/.fleet-recovery/enroll-$NEW_SERVICE-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SAFE"
chmod 700 "$SAFE"
umask 077
IMAGE=$(podman inspect polygon-data-service --format '{{.ImageName}}')
podman run --rm --network none -e POLYGON_API_KEY= \
  -v learn-ai_alpaca-fleet-control:/app/artifacts/fleet -v "$SAFE:/backup" \
  "$IMAGE" python -m scripts.manage_broker_fleet backup-registry \
  --control-dir /app/artifacts/fleet --backup-dir /backup/registry
podman volume create "$NEW_VOLUME"
podman run --rm --network none -e POLYGON_API_KEY= \
  -v learn-ai_alpaca-fleet-control:/app/artifacts/fleet \
  -v "$NEW_VOLUME:$CEREMONY_ROOT" \
  "$IMAGE" python -m scripts.manage_broker_fleet provision \
  --control-dir /app/artifacts/fleet --broker alpaca --label 'Paper Two' \
  --volume-root "$CEREMONY_ROOT" --attestation-id "$NEW_VOLUME" \
  --deployment-namespace compose:learn-ai > "$SAFE/enrollment.json"
```

Stop on any error. Do not reuse an existing nonempty volume. Open the private
`enrollment.json` in your editor, never paste it into chat or commit it. Create
`deploy/fleet/env/paper-two.env` from the Paper example and fill this mapping:

| Enrollment JSON field | New lane env variable |
|---|---|
| `clerk_id` | `FLEET_CLERK_ID` |
| `worker_key` | `FLEET_WORKER_KEY` |
| `agent_service_token` | `FLEET_AGENT_SERVICE_TOKEN` |
| `coordinator_service_token` | `FLEET_COORDINATOR_SERVICE_TOKEN` |

Add that account's Paper API keys and run `chmod 600 deploy/fleet/env/paper-two.env`.
In `deploy/fleet/env/coordinator.env`, add a JSON entry keyed by the **new clerk
ID** to each corresponding map: `agent_service_token` goes in
`FLEET_AGENT_SERVICE_TOKENS_JSON`, and `coordinator_service_token` in
`FLEET_COORDINATOR_SERVICE_TOKENS_JSON`. Preserve the existing entries. If a
local override still declares these same keys under `environment:`, update that
effective source or remove its duplication; it overrides the env file.

### B. Declare its service and approve its endpoint

Inside the existing `services:` mapping in `compose.fleet.dev.yaml`, add:

```yaml
  alpaca-paper-two:
    <<: *alpaca-clerk-agent
    container_name: alpaca-paper-two
    env_file:
      - path: ./deploy/fleet/env/paper-two.env
        required: true
    volumes:
      - ./PythonDataService/app:/app/app:z
      - alpaca-paper-two-data:/app/artifacts/alpaca_clerk
    environment:
      <<: *alpaca-clerk-agent-env
      FLEET_AGENT_ENDPOINT_REF: alpaca-paper-two-agent
      FLEET_WORKER_SERVICE: alpaca-paper-two
      FLEET_COMPOSE_FILES: compose.yaml,compose.fleet.dev.yaml
      IBKR_CLIENT_ID: "3"
```

Inside the existing `volumes:` mapping in that same file, add:

```yaml
  alpaca-paper-two-data:
    name: learn-ai-alpaca-paper-two-data
    external: true
```

Set the issued clerk ID (an identifier, not a credential), then approve:

```bash
NEW_CLERK_ID='PASTE_THE_ISSUED_CLERK_ID'
podman exec polygon-data-service python -m scripts.manage_broker_fleet approve-endpoint \
  --control-dir /app/artifacts/fleet --clerk-id "$NEW_CLERK_ID" \
  --endpoint-ref alpaca-paper-two-agent --base-url http://alpaca-paper-two:8000
COMPOSE_ARGS=(--file compose.yaml --file compose.fleet.dev.yaml)
if [[ -f compose.override.yaml ]]; then COMPOSE_ARGS+=(--file compose.override.yaml); fi
podman compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate python-service
podman compose "${COMPOSE_ARGS[@]}" up -d --no-deps alpaca-paper-two
python3 scripts/alpaca_onboarding_gates.py credential-match --mode paper \
  --container alpaca-paper-two --env-file deploy/fleet/env/paper-two.env
```

### C. Bind its account

Continue with section 2's UI verification/approval steps on **Paper Two**.
For activation in section 4, substitute the new container, volume, and env file;
every host `capture-evidence` call needs `--container alpaca-paper-two` (or the
offline helper while stopped) and `--env-file deploy/fleet/env/paper-two.env`.
The status command needs `--container alpaca-paper-two`. The existing Live and
Paper lanes must retain their own assignments and volumes throughout.

**Done:** the new card has its own identity and account, its roster check passes,
and the pre-existing account cards still pass. [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md)
remains the authority for enrollment and isolation.

## 4. Offline Paper activation

Use this only for a **fresh, flat Paper account**, or a Paper account moved
aside by the explicit developer reset. It preserves the old authority and
creates/activates the new generation. For a non-flat account, unknown orders,
corruption, or ordinary recovery, stop and use the
[recovery procedure](alpaca-sqlite-clerk-recovery-and-cutover.md).
Do not reset a database just to make a roster load.

The account must have no positions and no open orders. The capture command
checks the broker response, status, account identity, positions, and orders;
an HTTP error response never counts as an empty account.

### A. Set the values and capture fresh broker evidence

Replace the account below with the verified Paper account number. Keep this
terminal open so subsequent commands use the same variables.

```bash
ACCOUNT_ID='YOUR_PAPER_ACCOUNT_NUMBER'
CLERK='alpaca-paper-clerk'
CUSTODY_VOLUME='learn-ai-alpaca-paper-clerk-data'
ROOT='/app/artifacts/alpaca_clerk'
CAPTURE="broker_captures/activation-$(date +%Y%m%d-%H%M%S)"
RECOVERY='alpaca-paper-recovery'
BACKUP_DIR="$HOME/.fleet-recovery/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
python3 scripts/alpaca_onboarding_gates.py capture-evidence \
  --mode paper --account-id "$ACCOUNT_ID" --phase initialize --capture-dir "$CAPTURE"
```

**Done:** capture says the expected account is flat, has no open orders, and
retains all captures. Capture commands need host access to Alpaca's execution
API; they never submit orders.

### B. Stop the writer, back up the entire volume, and open an offline helper

```bash
podman stop "$CLERK"
podman volume export "$CUSTODY_VOLUME" --output "$BACKUP_DIR/paper-volume.tar"
chmod 600 "$BACKUP_DIR/paper-volume.tar"
test -s "$BACKUP_DIR/paper-volume.tar"
IMAGE=$(podman inspect "$CLERK" --format '{{.ImageName}}')
podman run -d --name "$RECOVERY" --network none --volumes-from "$CLERK" \
  -e POLYGON_API_KEY= -e IBKR_LIVE_RUNS_ROOT="$ROOT/live_runs" \
  "$IMAGE" sleep infinity
```

**Done:** the normal worker is stopped; the backup is nonempty; the helper has
no network. Do not start a second writer or delete the named volume.
If the initialize evidence is already more than two minutes old, recapture it
with `--container "$RECOVERY"` before the next command.

### C. Initialize, make a verified backup, then prepare a fresh plan

The roster inventory requires a real `live_state` directory, even when there
are no bots. Create it under this exact lane root; a missing directory is not
accepted as proof of an empty roster.

```bash
podman exec "$RECOVERY" mkdir -p "$ROOT/live_state"
podman exec "$RECOVERY" python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root "$ROOT" --account-id "$ACCOUNT_ID" cutover-initialize \
  --runner-artifacts-root "$ROOT" --broker-evidence "$ROOT/$CAPTURE/init-evidence.json" \
  --max-evidence-age-ms 120000
podman exec "$RECOVERY" python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root "$ROOT" --account-id "$ACCOUNT_ID" backup
python3 scripts/alpaca_onboarding_gates.py wal-checkpoint \
  --mode paper --container "$RECOVERY" --account-id "$ACCOUNT_ID"
python3 scripts/alpaca_onboarding_gates.py capture-evidence \
  --mode paper --container "$RECOVERY" --account-id "$ACCOUNT_ID" \
  --phase plan --capture-dir "$CAPTURE"
podman exec "$RECOVERY" python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root "$ROOT" --account-id "$ACCOUNT_ID" cutover-plan \
  --runner-artifacts-root "$ROOT" --broker-evidence "$ROOT/$CAPTURE/plan-evidence.json" \
  --max-evidence-age-ms 120000 --output "$ROOT/$CAPTURE/cutover-plan.json"
podman exec "$RECOVERY" cat "$ROOT/$CAPTURE/cutover-plan.json"
```

Review the exact account, generation, flat broker evidence, verified backup,
runner inventory, and proposed changes. A fresh/reset account should have no
unexplained positions, orders, or bot inventory. **Do not delete SQLite WAL/SHM
files by hand.** The checkpoint command checks that SQLite completed the flush.
If initialization says an authority already exists, do not delete it or rerun a
reset; inspect its verification and recovery state.

### D. Apply the reviewed plan within its two-minute lifetime

Copy the plan's `confirmation_token` into the variable below. It is a one-plan
confirmation value, not an Alpaca API credential.

```bash
CONFIRMATION_TOKEN='PASTE_THE_REVIEWED_PLAN_TOKEN'
podman exec "$RECOVERY" python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root "$ROOT" --account-id "$ACCOUNT_ID" cutover-apply \
  --runner-artifacts-root "$ROOT" --broker-evidence "$ROOT/$CAPTURE/plan-evidence.json" \
  --max-evidence-age-ms 120000 --plan "$ROOT/$CAPTURE/cutover-plan.json" \
  --confirmation-token "$CONFIRMATION_TOKEN"
```

**Done:** the command returns an activation receipt for the intended generation.
If the plan expired or inputs changed, capture fresh **plan** evidence, run
`cutover-plan` again, review again, and apply the new token. Do not extend the
freshness limit or reuse the old token. Do not repeat initialization.

### E. Close the helper and verify the actual worker

```bash
podman stop "$RECOVERY"
podman rm "$RECOVERY"
podman start "$CLERK"
python3 scripts/alpaca_onboarding_gates.py status --mode paper --require roster
```

Only the temporary helper container is removed; **no volume is removed**.
Keep the full-volume backup, verified backup, captures, and cutover receipt.
If startup says the account pin is missing, complete **Verify account → Approve
this account → Stage → Apply** in section 2 and restart the worker. If Apply was
previously refused, reissue it after fixing the stated cause. Never treat
“restart required” as proof that another restart can repair a reset fence.

## 5. Launch a bot

1. Select the account card and open **Bots → Deploy strategy**.
2. Choose a strategy. **Accepted evidence** means its validation passed; it
   does not mean this account has permission to run it.
3. If access is Off, use **Review & enable Paper** or **Review & enable Shadow**.
   Review the exact strategy, account, and evidence, then confirm. This records
   permission only; it does not launch anything. Repeat per strategy/account.
4. Choose **Paper** on the Paper lane or **Shadow** on the Live Shadow lane.
   Enter a unique bot name, symbol, and intended sizing. Wait until every
   admission gate is ready. Click **Deploy paper bot** or **Deploy shadow bot**
   when you intend to start the run.
5. Confirm the new bot appears in its account's roster and reports its runner
   state and data feed. For an existing stopped bot, select it and use **Resume**
   only after its own admission diagnosis is clear.

Permissions live in the lane volume at `canary_admission/events.json`, alongside
custody state. Recreating a container preserves them. Do not place the ledger
in the container's disposable `/app/artifacts/canary_admission` directory.
An account permission uses the actual account number; the internal
`shadow:<account-number>` custody namespace resolves the same account permission.
A Shadow-only legacy grant does not authorize real custody. Arming is separate.

To inspect permissions without launching anything:

```bash
podman exec alpaca-paper-clerk python -m scripts.manage_canary_admission status
```

To revoke **one strategy's future admission** (replace the values; use the Live
container for Shadow):

```bash
podman exec alpaca-paper-clerk python -m scripts.manage_canary_admission revoke \
  --program YOUR_PROGRAM_KEY --account-id YOUR_ACCOUNT_NUMBER \
  --reason 'Operator removed this strategy permission'
```

Revocation is not a Stop command and does not close an existing position. Manage
an already-running bot through its own controls before treating it as stopped.

## 6. Removal

### Remove a saved profile

On the lane's **Configuration** page, archive the unused profile. If the app
refuses because it is selected, effective, or bound to a bot, follow that
refusal; do not edit the profiles database. Archiving a profile does not remove
the lane, its account assignment, its custody records, or its brokerage account.

### Take an account lane out of use without deleting its history

1. Review every bot and any working orders on that account. Use the bot controls
   to stop activity according to its exposure policy. Stopping infrastructure
   must not be used as a substitute for an order/position decision.
2. Record the lane's service name, account, named volume, and status. Preserve
   the credentials privately if you intend to restore access.
3. Stop only that service, for example `podman stop alpaca-paper-clerk`.
4. Export its stopped named volume as in section 4B. Keep its fleet assignment
   and volume intact. Remove/disable the service's automatic startup declaration
   in the local topology if you need it to stay offline across stack restarts;
   simply stopping it does not prevent a later `restart.sh` from starting it.
5. Expect its card to become unavailable; this is **disabled**, not permanently
   retired. Restore it by reinstating the same service/volume/identity and
   starting that service, then check roster readiness.

### Permanently retire or reassign a served account

The ADR 0063 drain ceremony now ships. A served lane is retired through the
host CLI, in order:

```bash
# 1. Close the door. The command prints the drain deadline — an absolute
#    instant derived from the deployment duration and the trading calendar.
.venv/bin/python -m scripts.manage_broker_fleet drain \
  --control-dir <coordinator-control-root> --clerk-id <clerk-id>

# 1b. Watch for the lane's next heartbeat before proceeding. A live lane
#     learns it is drained from the heartbeat's lifecycle answer (or the
#     typed registration refusal, if it restarts), tombstones its own
#     confirmation evidence, and refuses new bot starts — after which no
#     coordinator outage can boot that binding back up (#2155). A lane
#     already unreachable at drain time never learns: treat its volume as
#     part of this retirement and never restart it; do not wait for it.

# 2. Wait out the printed deadline, then release each assigned account under
#    a bounded attribution (who acted, and the incident/change record naming
#    why — the old fixed proof phrase is deleted and never a substitute).
.venv/bin/python -m scripts.manage_broker_fleet release-assignment \
  --control-dir <coordinator-control-root> \
  --broker alpaca --account-id <canonical-account-id> \
  --expected-generation <observed-generation> \
  --operator <named-operator> --change-ref <restricted-record>

# 3. Retire. Until the lane-quiet provider ships (#2154) the plain retire
#    refuses naming the outstanding item; force-retire is the named exit —
#    deadline-bound, operator-attributed, and it settles any lost dispatches
#    as outcome-unknown in a durable ledger beside the registry.
.venv/bin/python -m scripts.manage_broker_fleet force-retire \
  --control-dir <coordinator-control-root> --clerk-id <clerk-id> \
  --operator <named-operator> --change-ref <restricted-record>
```

Lane-to-lane reassignment remains blocked until #2154's lane-quiet
confirmation ships — #2155's resurrection hole is closed for every lane
that learns its drain, but a lane unreachable for the entire drain keeps
unmarked evidence, and without lane quiet the coordinator cannot tell that
residual population from a quiet one; whole-machine migration is the
preferred lane move. Do not delete registry rows, reuse the
old volume for another account, or run `down -v`.

Closing the actual brokerage account is a separate action in Alpaca, outside
this application. This runbook deliberately does not claim that a saved-profile
archive or a stopped container has closed it.

## 7. Troubleshooting

| Message or symptom | Meaning | Recovery and completion check |
|---|---|---|
| Account card opens but roster does not | Custody may be unavailable, or an account request may have failed | Run `status --require roster`; follow any startup refusal. If it succeeds but the menu still fails, reload the app and retain the failing URL/error for diagnosis; do not reassign the account to fix a link. |
| `developer_reset_reactivation_required` | Paper authority was deliberately moved aside | Section 4; preserve old generation and backup. |
| `ACTIVATION_REQUIRED` on fresh Paper | Account verified but custody not activated | Section 4, then check roster readiness. This is not required for Live Shadow. |
| Account pin missing/mismatch | Verified account was not approved, or credentials changed | Verify the intended number, approve, stage/apply, restart; never pin the wrong returned account. |
| Apply refused because bots remain bound | The proposed change would replace an account still used by bots | Keep the current binding. Inspect its bot list and the exact refusal; do not force a profile swap. |
| Shared market-data feed not installed | IBKR disabled on that worker | Restore enabled/read-only settings in every effective overlay, then recreate. |
| Feed disconnected | Gateway logged out, wrong host/port/mode, or duplicate client ID | Restore Gateway and matching settings; unique IDs per lane. Wait for reconnect. |
| Market liveness unknown/stale | No fresh, usable symbol evidence yet | Keep Gateway connected and open the bot panel. IBKR must deliver live data; frozen/delayed data or missing entitlement does not count. A symbol halt needs a genuine resume report. |
| Accepted strategy, access Off | Validation and account permission are separate | Section 5's reviewed enable flow. |
| Account ready, old bot Resume blocked | That bot's seal, custody, build, or permission has its own refusal | Read its admission diagnosis. Do not bypass it with account-level readiness. |
| Stale cutover plan/evidence | The review window expired | Fresh plan capture → new plan → review → apply within two minutes. |
| SQLite WAL/SHM refusal | Offline database has uncheckpointed sidecars | Stop the writer; use the verified checkpoint command, never manual removal. |
| Permissions disappear after recreate | Ledger stored outside persistent lane volume or wrong volume mounted | Confirm the volume and ledger path; re-enable through reviewed proof, never a source-code allowlist. |

## Repair record: 2026-09-16

The Paper developer reset left its old authority inactive; its new approved
profile and generation-2 cutover restored the roster. The fleet overlay had
incorrectly disabled the retained IBKR feed; both lanes now retain enabled,
read-only connections. Direct Alpaca stock-data status consumption was replaced
by IBKR status/live-trade evidence. Strategy permissions now survive container
recreation, and Shadow Resume resolves the same account permission as setup.
Failed custody startup now records an Apply refusal instead of leaving a
misleading “restart required” receipt indefinitely.

The IBKR status mapping follows the vendor's [tick-type definitions](https://interactivebrokers.github.io/tws-api/tick_types.html#halted):
49 reports halt state; generic 233 supplies RTVolume trade timestamps. Missing
initial not-halted ticks are normal outside a TWS watchlist. Missing status
alone never authorizes trading: the fallback requires a real-time trade within
five seconds and no latched halt; delayed/frozen/stale observations refuse.

Implementation checks: `scripts/test_alpaca_onboarding_gates.py`, worker-lifecycle
regressions, durable permission regressions, IBKR liveness regressions, and the
fleet topology guard. These protect behavior; operational readiness must still
be checked against the running accounts with section 1.

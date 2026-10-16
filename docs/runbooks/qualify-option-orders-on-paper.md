# Qualify option orders on a Paper account

This is the operator procedure for qualifying option orders this system sends to
Alpaca. The run records below cover a bought call, a debit spread and the
owner's dashboard credit-order sign check on Paper, plus flat and overlapping
credit-spread bot passes and a flat-account iron-condor pass. The iron-condor
overlap and isolated short-leg buy-back still need their checks.
The overlapping credit entries' buying-power comparisons remain unknown.
This runbook sets one Paper
account aside, turns one temporary setting
on for its Clerk, runs the orders through their whole life, sets what Alpaca
did beside what the Clerk worked out, and ends with the setting off again or
with a capability promoted on the evidence. Run terminal commands from the
repository root. Stop at any failed command and use the troubleshooting table.

The kit that makes the evidence is [ADR 0032, amendment 2](../architecture/adrs/0032-broker-contract-v2-and-verbatim-capture.md)
(what the capture journal records) and
[`scripts/option_qualification_bundle.py`](../../PythonDataService/scripts/option_qualification_bundle.py).
What is being checked is
[ADR 0076](../architecture/adrs/0076-option-structures-on-the-money-path.md)
decisions 26 to 28, under PRD #3095 part D and issue #3103.

**Permanent provider boundary: IBKR supplies live market data. Alpaca supplies
account reads, orders, and execution reports.** The Paper lane needs IB Gateway
for its option quotes. Never change `IBKR_BROKER_ENABLED`, `IBKR_READONLY` or a
Gateway client ID to make an experiment work, and never turn on Alpaca market
data. This is the owner's 2026-09-16 decision, recorded in
[ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16).

**Three settings, one touched.** `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED` is
the only one this runbook turns on, for one Paper lane, for the experiment.
`ALPACA_OPTION_BUYING_POWER_JUDGED` stays off: with it off the Clerk still
records both readings of Alpaca's option buying power for every entry, and the
recorded pairs are judged afterwards, offline, by the same arithmetic.
`ALPACA_LIVE_OPTION_BOTS_ENABLED` is never touched: Live option bots stay off
whatever Paper shows, until the owner says otherwise separately.

**Nothing is sent without the owner's word at that moment.** Sections 1 to 4
read and configure. Section 5 is the go-ahead, as its own step. Neither this
runbook, nor the PRD, nor an earlier "yes" supplies it.

## Choose the procedure

| What you want | Follow |
|---|---|
| Pick the account and see that it can run option orders | [1. Choose the account](#1-choose-the-account) and [2. What must be true first](#2-what-must-be-true-first) |
| Turn the temporary setting on for one Clerk, and see that its journal carries the times | [3. The temporary setting](#3-the-temporary-setting) |
| See that Deploy's own check reads that setting | [4. Check the setting reaches Deploy](#4-check-the-setting-reaches-deploy) |
| Ask for the go-ahead | [5. The owner's go-ahead](#5-the-owners-go-ahead) |
| Send the orders and see them through | [6. The experiments](#6-the-experiments) |
| Keep what each step showed | [7. Record each step and make the bundle](#7-record-each-step-and-make-the-bundle) |
| Understand a difference between Alpaca and the Clerk | [8. Classify a difference](#8-classify-a-difference-before-anything-is-changed) |
| End the experiment | [9. Tear down](#9-tear-down) |
| Switch a capability on for Paper | [10. Promote what the evidence shows](#10-promote-what-the-evidence-shows) |
| Know what Paper cannot tell you | [11. What Paper cannot show](#11-what-paper-cannot-show) |
| Understand a refusal | [12. Troubleshooting](#12-troubleshooting) |

A **bundle** is the folder `scripts/option_qualification_bundle.py bundle`
writes: the comparison records with their offline judgment, the bot's option
orders and executions, the journal's lines of a window with the time each
request began and ended, and a one-page summary that names anything it could
not read. A **recording** is a journal line sanitised for commit. Both are
made offline: they read the Clerk's store and its journal, contact no broker
and need no credential.

## 1. Choose the account

The experiment needs an account where nothing else can move the figures it
compares. Two things can be true of the existing Paper lane, `alpaca-paper-clerk`
(`deploy/fleet/env/paper.env`):

- **No bot on it matters.** Every bot on it is a test, stopped, and flat. Use it.
- **A bot the owner needs is on it, or its positions matter.** Do not use it.
  Add a second Paper lane with its own Paper account, volume, identity and env
  file by [section 3 of the account runbook](add-an-alpaca-account.md#3-provision-another-lane)
  (its example is `alpaca-paper-two` and `deploy/fleet/env/paper-two.env`),
  verify and approve its account, and use that.

Either way, name the lane once and keep this terminal open:

```bash
LANE='alpaca-paper-clerk'                  # or alpaca-paper-two
ENV_FILE='deploy/fleet/env/paper.env'      # or deploy/fleet/env/paper-two.env
ACCOUNT_ID='YOUR_PAPER_ACCOUNT_NUMBER'     # the number the lane's card shows
EVIDENCE="$HOME/.fleet-recovery/option-qualification-$(date +%Y%m%d)"
mkdir -p "$EVIDENCE"
chmod 700 "$EVIDENCE"
python3 scripts/alpaca_onboarding_gates.py status --mode paper --require roster --container "$LANE"
```

The account must be **Paper**, authority `real_paper`. A Live account, and a
Live Shadow, are not this runbook's: it never sends an order to either. The
account has no position and no working order, and the experiment names one
test bot with the narrowest structure and the fewest contracts that exercise
each step. A flat account is read at Alpaca's dashboard as well as on the
Clerk's roster: a position or an order that no bot of this system placed is
exactly what makes a comparison unknown.

A note on fees, so the account is not blamed for it: the first fee row Alpaca
posts for a day with an option fill cannot be attributed to a bot yet, and by
the standing fee rule that stops new entries for every bot on the account
until it is resolved. Paper may post none. This is one more reason the account
is set aside.

**Done:** the lane is named, its roster check says `OK` with authority
`real_paper`, Alpaca's dashboard shows the account flat with no working order,
and no bot the owner needs is on it.

## 2. What must be true first

Check each, and write the answer in the step log (section 7).

1. **The account's option level.** One bought call or put needs level 2. A
   spread, a credit spread or an iron condor needs level 3. Deploy's money
   review states what Alpaca reported and refuses by name when it is too low or
   not known (`OPTION_DEPLOY_APPROVAL_TOO_LOW`, `OPTION_DEPLOY_APPROVAL_UNKNOWN`);
   a level Alpaca did not state is never read as approval. Raise the level at
   Alpaca, not here.
2. **The Clerk's option quote line budget.** It is `IBKR_OPTION_LINE_BUDGET`,
   zero until the owner sets it, because every option line comes out of the
   IBKR allowance that the stock quotes of every bot draw on
   ([ADR 0076 decision 9](../architecture/adrs/0076-option-structures-on-the-money-path.md)).
   With zero, no structure can be priced and Deploy refuses it (`OPTION_WINDOW_OVER_LINE_BUDGET`).

   ```bash
   podman exec "$LANE" printenv IBKR_OPTION_LINE_BUDGET
   ```

   Deploy's review says how many lines the structure can need and how many it
   counts free. Setting the budget is the owner's decision, in that lane's env
   file and by recreate (section 3's commands), inside the allowance IBKR grants
   the username. It never involves the provider settings above.
3. **Regular hours.** An option bot is only ever started for regular hours,
   and its orders are sent in the regular session. Begin a step with enough of
   the session left for the whole life of its orders, and do not choose a
   contract that expires today: the entry cutoff before an expiry-day close, or
   the close itself, would end the step.
4. **IB Gateway is signed in** on the Paper port and its data is live. Run the
   deploy check for the underlying you will trade (section 1 of the account
   runbook):

   ```bash
   python3 scripts/alpaca_onboarding_gates.py status --mode paper --require deploy --symbol SPY --container "$LANE"
   ```
5. **The lane runs the kit's code.** Two things must be the kit's: the capture
   journal's hook, which writes the times and an option order's body, and the
   bundle script. Which is true depends on the compose overlay the lane runs
   under:
   - **`compose.fleet.yaml`** (the production topology) bakes `app/` and
     `scripts/` into a read-only image. A lane built before the kit merged has
     the old hook, which records no times and no body, and nothing in that
     image can be mounted over it from here. **The image must be rebuilt**, with
     `--build`, from a checkout that has the kit:

     ```bash
     podman compose --project-name learn-ai-fleet -f compose.yaml -f compose.fleet.yaml --profile fleet \
       up -d --build --no-deps --force-recreate "$LANE"
     ```

   - **`compose.fleet.dev.yaml`** bind-mounts `./PythonDataService/app`, so the
     lane runs the checkout's `app/` once it is recreated or restarted. Only the
     scripts come from the image; section 7 has the way to run the bundle
     script without a rebuild, which works under this overlay only.

   Do not rely on this step alone: section 3 step 5 reads a fresh journal line
   from the running lane before anything is sent. The bundle script's presence
   is the other half:

   ```bash
   podman exec "$LANE" test -f /app/scripts/option_qualification_bundle.py && echo present
   ```

**Done:** the level, the line budget, the hour, the Gateway and the kit are each
confirmed and written down. None of them is guessed from a green container.

## 3. The temporary setting

`ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED` defaults to off. While it is off a
Paper Clerk refuses a plan of several legs by name, and says a Dry Run takes
it, so the experiment cannot reach a spread otherwise. It is one line in that
lane's env file, for that Clerk only. A lane has its own env file, so nothing
else changes.

1. Check the line is not already there, then add it. `grep -c` exits `1` when
   it finds nothing, which is the result wanted here, so that status is let
   through and any other (`2`: the file is missing or unreadable) still stops
   you:

   ```bash
   grep -c '^ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=' "$ENV_FILE" || [ $? -eq 1 ]
   ```

   It must print `0`. If it prints `1` the line is already there: do not add a
   second, and read its value with the container check in step 4 before going
   on. Then add it:

   ```bash
   printf 'ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=true\n' >> "$ENV_FILE"
   chmod 600 "$ENV_FILE"
   ```

2. **Recreate** the lane. A restart does not reload an env file; the container
   keeps the environment it was created with. These are the commands of the
   `compose.fleet.dev.yaml` topology; under `compose.fleet.yaml` use the
   `--project-name learn-ai-fleet -f compose.yaml -f compose.fleet.yaml
   --profile fleet` context of section 2 step 5, with `--build` there.

   ```bash
   COMPOSE_ARGS=(--file compose.yaml)
   if [[ -f compose.fleet.dev.yaml ]]; then COMPOSE_ARGS+=(--file compose.fleet.dev.yaml); fi
   if [[ -f compose.override.yaml ]]; then COMPOSE_ARGS+=(--file compose.override.yaml); fi
   podman compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate "$LANE"
   ```

3. **Wait for the lease.** A restart inside 30 seconds of the last one can
   crash-loop the new process on an execution lease the old one did not release,
   and it mends itself once the lease expires. Allow about a minute, do not
   recreate again inside it, then run the roster check from section 1. If it is
   still failing after two minutes, stop and use the
   [recovery runbook](alpaca-sqlite-clerk-recovery-and-cutover.md); do not reset
   anything to make a roster load.

4. Read what the container carries, never what the file says (`environment:`
   outranks `env_file:`, and the last compose file outranks the earlier ones):

   ```bash
   podman inspect "$LANE" --format '{{range .Config.Env}}{{println .}}{{end}}' \
     | grep -E '^(ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED|ALPACA_OPTION_BUYING_POWER_JUDGED|ALPACA_LIVE_OPTION_BOTS_ENABLED|IBKR_BROKER_ENABLED|IBKR_READONLY|IBKR_OPTION_LINE_BUDGET)='
   ```

   Expect `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=true`, the judgment and the
   Live setting absent or `false`, `IBKR_BROKER_ENABLED=true` and
   `IBKR_READONLY=true`. Anything else: stop, and put the file back as it was.

5. **Read a fresh journal line from the running lane, before the go-ahead.** The
   journal is what the whole experiment is judged by, and a lane that runs
   older code writes lines with no times and no order bodies. This is found out
   only after orders are sent unless it is looked for now. The Clerk reads the
   account every few seconds, so a new `account` line appears without anything
   being sent. After the lane is up again, and after the wait in step 3:

   ```bash
   podman exec "$LANE" sh -c 'tail -n 1 "/app/artifacts/alpaca_clerk/broker_captures/alpaca/account/$(date -u +%Y-%m-%d).jsonl"' \
     | grep -c '"began_at_ms"'
   ```

   It must print `1`. `0` means the line was written by code without the kit,
   or no line is new: stop, do not ask for the go-ahead, and rebuild as section
   2 step 5 says for this overlay. Do not paste the line anywhere: it holds the
   account's figures.

**Done:** the container carries the one line, the other two settings are off,
and a fresh journal line carries `began_at_ms`: all read from the running lane.

## 4. Check the setting reaches Deploy

The Clerk reads the setting where it is asked, in its own process. Whether the
answer Deploy gives for this lane's account is that process's answer, in the
fleet, has not been verified. Check it before anything depends on it.

1. Open the Paper account's workspace and click **Deploy a bot** in its header.
2. Build a structure of two legs (a vertical spread on the underlying) in the
   structure builder, choose **Paper**, and look at the money review's
   **Can't deploy yet** list.
3. The list must not hold a *Structure* row whose text names
   `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED` and says it is off (reason code
   `OPTION_SEVERAL_LEGS_SETTING_OFF`). The same structure on a Dry Run is the
   control: a Dry Run takes it whatever the setting says.

If that row is there while step 4 of section 3 showed `true`, the setting does
not reach Deploy's check from this env file. **Stop.** Do not edit another
service's env file to get past it. Write the row's text and the container's value
in the step log, tell the owner, and tear down (section 9). That is a finding
about the fleet, not a failed experiment.

Other rows are what section 2 and the troubleshooting table are for. A row
about quote lines, option approval or buying power is a precondition, not a
defect.

**Done:** the Deploy review of a several-leg structure on this account has no
row for the setting being off, and the step log says so with the date.

## 5. The owner's go-ahead

Everything above sent nothing. What follows sends orders to Alpaca's Paper
account.

**Ask the owner, in words, now:** which account, which test bot, which
structures, how many contracts, and the largest exposure the experiment may
reach (the narrowest structure, one contract, and a budget just above the one
entry's commitment that Deploy's review shows). Wait for a clear yes to that.
Write the answer, who gave it and the time in the step log.

- A yes from an earlier day, an earlier step, the PRD, an issue, or this
  runbook is not the go-ahead.
- The go-ahead covers the exposure stated. A larger one, or another account,
  is asked again.
- Without it, stop here and tear down.

**Done:** the owner's answer to that question, with its time, is in the step log.

## 6. The experiments

One test bot, one structure at a time, in this order:

| # | Shape | Money | Account level |
|---|---|---|---|
| A | One bought call or put | Pays a debit | 2 |
| B | A spread that pays | Pays a net debit | 3 |
| C | A spread that receives | Receives a net credit | 3 |
| D | An iron condor | Four legs, receives a credit | 3 |

Each shape goes through **two passes**: first on the flat account of section 1,
so that what moved can only be the order under test, then **with overlapping
positions on purpose** (a second structure on the same underlying and expiry,
held while the first is, so that Alpaca nets them and the account's requirement
can be less than the bot's own commitment; the bot does not spend that saving,
and the comparison records the account's requirement before and after to show
it).

**The sign comes first.** A multi-leg order's net limit price carries its sign:
Alpaca reads a positive value as a debit and a negative one as a credit, and a
credit must never be sent as a debit. Before a bot trades a structure that
receives (shapes C and D), confirm the sign from Alpaca's own answers: the
answer to shape B's order (positive, paid), and the answer to one received
order of one structure that the owner places by hand at Alpaca's Paper
dashboard, inside the stated exposure of section 5 (an order from outside to
the Clerk; the journal's `orders` file records how Alpaca reports its net
price and its fill). Write both readings in the step log. Only then may C or D
run as a bot.

That hand-placed order is **its own step, a sign check, and not a pass of any
shape**. Take it between B and C, once B's overlapping pass has been closed and
the account reads flat again (section 1). Place it, read its answer, and
**close whatever it filled at once, by hand, at Alpaca**, then read the account
flat again (no position, no working order) and acknowledge what the account's
Home lists of it as orders from outside. It is never left open to serve as
an overlapping position: an overlap is made on purpose, by a bot's own
structure, in each shape's second pass, and a leftover hand-placed fill would
make C's flat-account pass something other than flat. Make its bundle like any
step's; the Clerk compares only a bot's entries, so it has no comparison, and
it is named in the step log as a sign check.

After adding both closing legs at Alpaca's dashboard, recheck both position
intents: **Buy to Close** the sold contract and **Sell to Close** the bought
contract. On October 16, adding the second leg reset both selectors to opening
intents, even though the first single-leg ticket had said **Buy to Close**.
Correct the selectors before submitting the whole-spread close.

**A step** is: deploy the test bot with the structure and the four order terms,
let it enter, see the entry through to its end, close it, and see the money
come back. For each shape:

1. **Rehearse as a Dry Run** on the same account: it sends nothing, shows the
   contracts that would be picked and the money review, and costs the same
   quote lines. A rehearsal that cannot be priced will not be priced on Paper.
2. **Deploy as Paper** (section 5 of the account runbook: the strategy must be
   allowed on the account, and an option bot on Paper needs the owner's written
   acceptance on the Deploy page). Use the structure builder, one structure, and
   a budget that covers one entry as Deploy's review prices it. Write the
   four order terms in the step log: the widest bid-ask gap a leg may have, the
   entry wait in seconds (1 to 30), the cost limit, and the expiry-day close
   lead.
3. **Make the bot enter.** The entry comes from the bot's strategy, not from
   you. A strategy that cycles quickly is the shipped Deployment Validation
   program, which enters after two green minute bars from 09:45 ET, exits three
   bars after its entry fills, and flattens at 15:45 ET; any other is the
   owner's choice. Be ready to write what you see as it happens.

### The lifecycle to exercise, for each shape

Paper decides whether an order fills, and when. A case that does not occur is
**not** forced and is **not** counted as shown: it goes into the unverified
list of section 11 with the attempts made.

| Case | How it is reached | What to see |
|---|---|---|
| Accepted and unfilled until the deadline's cancel | Set the entry wait to its shortest, or deploy while the quote is moving away; Paper may then not fill inside the wait, and nothing here makes it | The order rests at Alpaca; at the entry's deadline the Clerk cancels the parent (and so every leg); the cancel is proven; the bot's reservation is released |
| Filled and closed | The ordinary run | One entry order, each leg filled; later one closing order for the whole structure, filled; the account flat |
| Filled in part, then cancelled | Several contracts a structure. Paper fills eligible orders in part at random, about one time in ten, so this may not be seen | The filled contracts are held; the rest is cancelled at the deadline; the entry reads as a part fill, never as a whole one |
| A cancel that races a fill | Shortest entry wait, limit at the ask, repeated. It cannot be made to happen | The order ends as the book of both Alpaca and the Clerk agree; no contract is lost or counted twice |
| A Clerk restart with an entry resting | `podman restart "$LANE"` (not a recreate: the setting is already on) at once after the entry is sent, with the longest entry wait. The restart is likely to outlast the wait, so what is tested is then recovery of an entry whose deadline passed while the Clerk was down | After boot the entry is found at Alpaca, cancelled if unfilled, and proven; nothing is sent twice |
| A Clerk restart with a position held | `podman restart "$LANE"` while the structure is held | The position is found, the bot's holding is the same, its scheduled close is still owed |
| The release of the reservation and of buying power | After every ending above | The bot page's **This bot's money** card has its free budget back; the account header's free figure agrees; Alpaca's option buying power returned to where it was, or the comparison says how far it did not |

After a restart wait about a minute before the next one (the lease, section 3),
and watch the roster before calling it broken.

### Closing a whole structure, and buying back one sold leg, checked apart

These are two experiments and are written down as two.

- **The whole structure.** Let the bot's own exit, its scheduled close or the
  bot's **Flatten** close it. The Clerk sends one closing order for the held
  contracts. Record whether Alpaca funded it, what it did to the account's
  buying power, and that the account is flat after.
- **One sold leg alone.** This system never sends one for a bot. With a
  *spread that receives* held and nothing else on the account, the owner
  buys to close the one sold contract at Alpaca's Paper dashboard, for one
  contract, as a limit order at its ask. Record whether Alpaca accepts it, what
  its refusal says if it refuses (Alpaca's support documents that buying to
  close a short option does not always release enough margin to fund the order
  itself), the account's option buying power before and after, and the cost at
  the quotes. The Clerk sees this order as one from outside; it holds that bot's
  own order in the contract until it settles or the owner has reviewed it, and
  the account's Home says what needs you. After it, close the rest of the
  structure with the bot's **Flatten**.

**Done:** each shape has been tried on the flat account and then overlapping, and
every row of the lifecycle table is either observed, with its step and bundle
named in the step log, or listed as not observed, with why.

## 7. Record each step and make the bundle

**What to write at each step**, in one step log you keep with the bundles: the
step name and shape; the time you started and ended (int64 ms UTC, below); the
test bot's id; the structure, its legs and the four order terms; what you did
and the time; what the bot page's **This bot's money** card showed (budget,
free, reserved) before and after; what the account header and **Account
details** showed (cash,
option buying power, equity); every Deploy row you saw; what Alpaca's
dashboard showed for positions and orders at the end; and the lifecycle case
the step was meant to show and whether it did.

The account, positions, orders, executions, fee rows and activities Alpaca
answered are in the capture journal already, each line with the time its
request began and the time its answer had been read whole, and an option
order's own body as sent. The bundle collects them with the Clerk's own record
of the comparison.

Take the time before the step and note it:

```bash
STEP='B1-debit-spread-flat'
BOT='THE_TEST_BOT_ID'
FROM_MS=$(( $(date -u +%s) * 1000 ))
```

After the step has ended and the entry has been compared (the bot page's
**Alpaca option buying power** card lists each entry's comparison; one is
recorded about a minute or two after the outcome is proven, and at the latest
ten minutes past the entry's deadline), make the bundle:

```bash
TO_MS=$(( $(date -u +%s) * 1000 ))
OUT="/app/artifacts/alpaca_clerk/qualification/$STEP"
IMAGE=$(podman inspect "$LANE" --format '{{.ImageName}}')
podman run --rm --network none --volumes-from "$LANE" -e POLYGON_API_KEY= \
  -v "$PWD/PythonDataService/scripts:/app/scripts:ro,z" "$IMAGE" \
  python -m scripts.option_qualification_bundle bundle \
  --artifacts-root /app/artifacts/alpaca_clerk --account-id "$ACCOUNT_ID" --bot "$BOT" \
  --from-ms "$FROM_MS" --to-ms "$TO_MS" \
  --capture-dir /app/artifacts/alpaca_clerk/broker_captures --out-dir "$OUT"
podman cp "$LANE:$OUT" "$EVIDENCE/$STEP"
cat "$EVIDENCE/$STEP/summary.md"
```

**Never run the bundle with `podman exec` in the lane.** The lane carries a
768 MiB memory cap under both fleet compose files: on 2026-10-14 the command run
there was killed (exit 137), took the lane's main process with it
(`OOMKilled`), and the restart ended both bots' runs. The one-off container
above has its own memory, shares the lane's volumes (which include the
bind-mounted `app/`), mounts this checkout's scripts, and has no network at all.

The window is `[from, to)`. The command only reads the Clerk's store (read-only
and query-only: it takes no lease) and the journal, and writes the one new
folder. It exits `0` when everything was read, `3` when it wrote the folder
with pieces it could not read (the summary names each under **Not read**), and
`2` when it was refused and wrote nothing. A folder that is not new or empty is
refused; so is one inside the Clerk's folder or the journal.

Under `compose.fleet.yaml` `app/` is baked into the image: an image without the
kit has no `rejudge` or `utc_day`, and the script fails with an ImportError only
after orders were sent. That is why section 3 step 5 checks before the
go-ahead, and why the image is rebuilt there instead.

**Read the summary before anything else.** It states what was compared, what
was unknown and why, any discrepancy, and what could not be read. An entry the
Clerk recorded as *unknown: judgment not switched on* is judged offline in the
bundle by the same rows the Clerk applies (`rejudge`): it writes nothing to the
Clerk's books, raises no notice and holds no bot. An unknown for another reason
stays unknown, with its reason: it was not a clean pair, and is never
promoted to a result.

**A bundle holds the account number and the owner's order identifiers. It is
evidence to read and is never committed.** What is committed are recordings,
made from it by the same script, sanitised by the rules the Paper capture
script applies (UUIDs, the account number, client order ids and order refs
become sentinel values, secret-like keys are redacted, and an authorization
frame becomes a placeholder):

```bash
cd PythonDataService
POLYGON_API_KEY= DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.option_qualification_bundle \
  recordings --bundle-dir "$EVIDENCE/$STEP" --distinct \
  --only account activities option_contracts orders positions stream \
  --out-dir tests/fixtures/alpaca/option_paper_recordings/$STEP
```

A journal of one afternoon is about 10 MB of the same answers polled again (one
page-through of the account's activities is three polls of about 30 KB each, and
the Clerk reads the account every few seconds). `--distinct` drops a poll, or a
page-through, that asked the same and got the same answer as the polls kept just
before it; the attribution counts what was dropped. `--only` leaves out the
families a reader of option orders does not need (the clock's polls); it names
the ones it left out. Both are choices made when the recording is made, not
gaps in the journal, and a recording made without them is the whole journal.

Recordings are added **beside** `tests/fixtures/alpaca_documented/` and the
tests that mutate an answer into a hostile one. Nothing there is removed or
swapped out: a recording shows one account on one day, and those cover states
Paper never produced.

Only JSON can have its keys told, so only JSON is written. A line whose body is
opaque text, a bare JSON string or not UTF-8 is **not sanitised, and is left
out**: it is named in the output and in the attribution, and nothing of it is
written. The journal gaps the source bundle itself recorded are carried into
the attribution too. Either one makes the command exit `3`, and a recording set
with an exit of `3` is read through, and its gaps accepted by the owner, before
anything in it is committed.

**Done:** every step has a bundle whose summary's **Not read** section is either
empty or explained in the step log, and the recordings of the steps worth
keeping are made and read through before they are committed.

## 8. Classify a difference before anything is changed

A difference is any comparison that is not *consistent* (*over-reserved*,
*under-reserved and unexplained*), any sign or time that is not what the plan
said, and any lifecycle case that ended differently at Alpaca and on the
Clerk's books. Three quantities are set beside each other for an entry and are
not assumed equal: the bot's **commitment**, the account's **incremental
requirement** (the requirement after the entry less before, recorded by the
Clerk) and Alpaca's **observed change in option buying power**. Premium,
working-order holds and fees are explained as separate terms.

Classify it by cause, one of these, before anything is changed:

1. a premium sign (paid taken for received, or the reverse);
2. a duplicate or a missing fill;
3. a reservation released late (a figure read before Alpaca had moved it);
4. a fee posting;
5. an exercise or an assignment;
6. an account offset (Alpaca netted this position against another);
7. the payoff rule itself.

How:

- Compare exact cents only once the quantities and the times are aligned: the
  journal's `began_at_ms` and `ended_at_ms` say when each figure was asked and
  answered, and the comparison record says which readings were taken when.
- **No tolerance is introduced.** A difference inside the bot's set-aside is
  *over-reserved* and holds nothing; it is classified, not rounded away.
- Only a payoff-rule error (7) changes the payoff rule. Then its fixture is
  regenerated from the evidence and [ADR 0076](../architecture/adrs/0076-option-structures-on-the-money-path.md)
  is amended. A premium sign, a late release or an account offset is **never**
  fixed by rewriting the payoff rule.
- Do not change a setting, a tolerance or a fixture to make a result agree.

**Done:** each difference has one named cause and the bundle and journal lines
that show it, written in the step log; nothing was changed to make it agree.

## 9. Tear down

When the experiment ends, with or without a promotion:

1. **Stop** the test bot, then **Flatten** whatever it still holds, from the
   bot's own controls. Stopping a bot is not proof it is flat.
2. Confirm at Alpaca's dashboard and on the roster that the account has no
   position and no working order, and that no outside order is waiting for the
   owner. A contract still held is closed before anything else.
3. If nothing is promoted, **remove the line** from `$ENV_FILE`, recreate the
   lane (section 3's commands), and read the container again: the setting must
   be absent or `false`. Then Deploy must refuse a several-leg structure again,
   as section 4's control: a temporary setting that is still on is a rollout by
   accident.
4. If a second lane was added only for this, leave it at rest or remove it by
   [section 6 of the account runbook](add-an-alpaca-account.md#6-removal), as
   the owner chooses. Do not delete its volume.
5. Keep the bundles, the step log and the owner's go-ahead. Commit the
   recordings only.

**Done:** the account is flat with no working order, the lane's container carries
only the settings that were promoted, and the evidence is kept.

## 10. Promote what the evidence shows

Each capability is switched on for Paper **only if its own evidence is whole**,
and the others stay off. The owner decides each, in words, as its own step, after
reading the bundles. Whole means: for that capability, each experiment in
section 6 ran on a flat account and then overlapping; every bundle's
**Not read** is empty or explained; every difference is classified and none is
open; every row of the lifecycle table is observed or is on the unverified list
and the capability does not depend on it.

| Capability | Switched by | Whole when |
|---|---|---|
| One bought option on Paper | Nothing: a Paper Clerk takes one bought call or put already | Shape A: the contract reference read journaled; the submit's body journaled with the terms sealed at Deploy; the order's answer, its fill and its close agree between Alpaca and the books; an unfilled entry cancelled and proven at its deadline; the reservation and Alpaca's buying power released |
| Several-leg entries | `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=true` in the Paper lane's env file | Shapes B, C and D: each submit's body shows the legs, ratios, sides, intents and a net limit whose sign is the plan's (a credit sent negative, a debit positive); the parent's answer and each leg's are read whole; each leg's fills attributed to its own contract; a resting parent cancelled at its deadline with every leg proven; a restart with an entry resting recovered |
| Several-leg closes | The same setting; a close is one order | A whole structure closed as one order, filled, the account flat, for each of B, C and D; the buy-back of one sold leg alone, as its own result, funded or refused |
| The comparison's judgment | `ALPACA_OPTION_BUYING_POWER_JUDGED=true` in the same file | Alpaca has been **seen** to move the figure by an order's cost: the recorded pairs of whole fills on a flat account judge consistent offline in every shape, and each that did not is classified (section 8) with its cause removed or accepted by the owner |

Promoting is: the owner's word for that capability; the one line in that lane's
env file; a recreate and a read of the container (section 3); the check of
section 4 where it applies; the recordings committed; and the decision record
amended ([ADR 0076](../architecture/adrs/0076-option-structures-on-the-money-path.md))
with what was shown, what could not be observed and what stays unverified.
After a judgment promotion a judged, unexplained shortfall holds that one bot's
new entries and raises a notice on the account; it holds no other bot.

**`ALPACA_LIVE_OPTION_BOTS_ENABLED` is not part of this.** Live stays off
whatever Paper shows. Before any Live decision, assignment, exercise and expiry
must have been exercised on an account, and that is not this runbook.

**Done:** the owner's word for each promoted capability is written down with its
time, anything not promoted is off, and the amendment lists what stays unverified.

## 11. What Paper cannot show

Write these into the amendment as unverified; do not let a clean run read as
proof of them.

- **Regulatory fees on the trade day.** Paper posts non-trade `FEE` rows the
  morning after, dated the trading day. A same-day run cannot check its fee
  estimates. The 2026-10-15 comparison of the previous day's rows found that
  Alpaca's OCC and OPT TAF charges exceed this model's estimates (#3306); see
  the run record below. Check the posted rows before qualifying fee accounting.
- **Assignment, exercise and expiry.** No event for them arrives on the trade
  stream, and a later activity row arrives a day after. Nothing in a same-day
  experiment exercises that path.
- **Any lifecycle case that did not occur.** A part fill, a cancel that races a
  fill, an unfilled entry that Paper filled anyway: each is listed with the
  attempts made, not counted.
- **Whether the trade stream carries option fills.** If it does not, the account
  stops taking entries at its first one, which the Deploy review's notes warn of.
  A step that saw it records it.
- **How the figure moves.** Whether Alpaca's option buying power falls by an
  order's cost, and when it moves for an order that rests or is cancelled,
  is what ADR 0076 decision 26 waits for. A recorded pair shows one account on
  one day.

**Done:** every item above that applies to what was run is written into the
amendment as unverified, with the attempts made, and none of them is stated as
shown.

## 12. Troubleshooting

| Message or symptom | Meaning | Recovery and completion check |
|---|---|---|
| Deploy row `OPTION_SEVERAL_LEGS_SETTING_OFF`, naming the setting | The Clerk that answers does not see the setting | Section 3 step 4: read the container. If it says `true`, stop (section 4): that is a finding. If not, the lane was restarted, not recreated, or a later compose file overrides it. |
| Deploy row `OPTION_DEPLOY_APPROVAL_TOO_LOW` or `_UNKNOWN` | The account's option level does not cover the structure, or Alpaca did not state it | Raise the level at Alpaca (2 to buy, 3 for several legs), then review again. Not known is never approval. |
| Deploy row `OPTION_DEPLOY_BUYING_POWER_SHORT` | Alpaca's option buying power is short of one entry | Raise the account's buying power at Alpaca or choose a narrower structure. The review states both figures. |
| Deploy row about *IBKR quote lines* (`OPTION_WINDOW_OVER_LINE_BUDGET`) | This Clerk's option line budget is zero, or too small for the window | Section 2 step 2. The owner sets `IBKR_OPTION_LINE_BUDGET` for that lane and recreates; or a narrower structure. Never a provider setting. |
| Deploy row `OPTION_DEPLOY_CONTRACT_REFERENCE_NOT_READ`, or the contract is not listed | Alpaca's contract reference did not answer, or does not list the picked contract | Wait and review again, or pick a structure on another expiry. The answer is in the journal's `option_contracts` file. |
| Deploy row `OPTION_DEPLOY_NOT_PRICED` | No entry of the structure can be priced yet | Check the Gateway and the underlying's live data; review again in a few seconds. |
| The Clerk answers 503 right after a restart | The lease trap: the previous process's execution lease had not yet expired | Wait up to a minute; do not restart again inside it. Then the roster check. |
| Section 3 step 5 prints `0`: a fresh `account` journal line has no `began_at_ms` | The lane runs code from before the kit: under `compose.fleet.yaml` the image is stale, under `compose.fleet.dev.yaml` the checkout's `app/` is not the kit's | Do not ask for the go-ahead. Rebuild with `--build` (production overlay) or recreate from a checkout that has the kit (dev overlay), wait for the lease, and read a fresh line again. |
| Bundle script fails with an ImportError (`rejudge`, `utc_day`) | The code in the image is older than the script | Rebuild the image (section 2 step 5); mounting only the scripts works under `compose.fleet.dev.yaml` only. |
| Bundle exit `3`, **Not read:** the Clerk's books | No `clerk.db` for that account id under the artifacts root | Check `--artifacts-root` and `--account-id` (the number the lane's card shows). The journal part was still written. |
| Bundle **Not read:** the bot | The id is not a bot on that account's books | Use the bot id on the bot's page. |
| Bundle **Not read:** the capture journal | No `alpaca` journal under the capture directory | `--capture-dir` must be the lane's `BROKER_CAPTURE_DIR`. |
| Bundle **Not read:** a journal line | A line that is not a journal line it can place in time | Keep it as it is; the journal is append-only and is never edited. Note it in the step log. |
| Bundle refused: *lies inside*, or *not empty* | The folder is the Clerk's, the journal's, or already holds a bundle | Choose a new folder. |
| Deploy row *IBKR quote lines: no lines free for another options bot* while a Dry Run of an option bot runs | A running Dry Run holds every option quote line of the budget, so a Paper option bot has none | Stop the rehearsal bot, review again after a few seconds (the lines reopen), then deploy. |
| Deploy refused: *The account changed before Deploy ... waiting to price this structure* | The money review on the page is older than about a minute, or the quote lines had only just reopened | Refresh money on the Deploy page and deploy inside the minute. Nothing was sent. |
| Recordings refused: *still carries the account id* | A sanitised line still holds the account id (as a key, for one) | Nothing was written. Read the line in the bundle, tell the owner, and do not commit it. |
| Recordings exit `3`, **Not recorded:** a line, *not sanitised, so it is left out* | The line's body is opaque text, a bare JSON string or not UTF-8: only JSON can be sanitised | Nothing of it was written. Leave it out; the attribution names it. Do not paste it into a fixture by hand. |
| Recordings exit `3`, **Not recorded:** a piece the *source bundle could not read* | The bundle that the recordings come from lost journal evidence | The attribution carries it forward. Make the bundle again from the journal if the cause is mended, or have the owner accept the gap. |
| An entry's comparison reads *unknown* for a reason other than judgment off | The pair was not clean (another order moved, cash moved, a reading failed or was stale, the outcome was not proven) | It stays unknown with its reason in the bundle. Repeat the step on a quieter account; never promote an unknown. |
| A notice that Alpaca took more than the bot set aside | Judgment is on for this Clerk (it must not be) and a pair judged *under-reserved* | Read section 3 step 4. The bot's own page has the owner's review; do not review it until the cause is classified. |
| Fees unresolved: every bot on the account waits | The first posted fee row of an option day cannot be attributed yet | Read the account's Home line for it. This is why the account is set aside. Record it as a finding. |
| Deploy says *Outcome unknown* (HTTP 503 `clerk_routing_outcome_unknown`), or 409 *Deployment budget is unavailable: Account reconciliation is proving fresh broker truth* | The lane's event loop was too busy to answer inside the coordinator's read, or its account reconciliation had not reached a verdict. Open bot pages poll `/evidence` and `/money` every few seconds on the same loop (1 to 5 s gaps, 10+ per minute, 2026-10-14) | Close the bot pages and the Deploy tab you are not using, wait a minute, then look at the roster before pressing Deploy again: the create may have reached the lane and the bot may exist (the lane's log has `POST .../bots 201`). Pressing Deploy again never starts a second bot. |
| Deploy says *Deployment is blocked until Clerk channels are installed and healthy*, or admission refuses `CLERK_CUSTODY_BROKER_UNREACHABLE` | A channel sample or one Alpaca read failed (a restart in the last two minutes, a network blip, one timed-out read) | Wait a minute and refresh money. It cleared on its own every time on 2026-10-14. |
| After a Clerk restart every bot is *Needs attention - ended without a clean exit* and a held position shows *No bot is managing* | A restart ends every bot run; nothing resumes the strategy. The position and its holding are found unchanged; only the end time's sale is still owed | Reload the bot page (a command opened before the restart is refused with *lane was rebound*), then **Flatten**: one closing order. Deploy again for a new bot. |
| `reduce blocked: STREAM_HEALTH_HOLD` with a several-leg position held | A several-leg parent's `fill` frame could not be mapped (fixed in #3173) and marked the stream's evidence unhealthy | Pull the fix and restart the lane; the hold is re-sampled. |
| Fees unresolved after a spread: *Account fill coverage is incomplete*, every fee *account-unattributed*, and the spread's legs held only from the order lookup (no execution named) | A spread filled before #3177 was deployed: its legs' executions came only in the parent's `fill` frame, which was not credited | Pull the fix and restart: spreads after it are credited when the frame arrives. For legs already booked: stop the lane, copy its account store, boot the lane's own image with a wrapper that hands the parent's recorded `fill` frame (the capture journal's `stream` file) to the consumer, then start the lane again. Each leg's execution is credited and the population is explained (done once on 2026-10-14; `test_a_parents_fill_frame_that_arrives_after_the_legs_were_read_by_lookup_explains_their_cumulative`). Check the account's Home no longer lists the fee evidence. |
| A bot's entry is refused `RECONCILIATION_IN_PROGRESS` ("Account reconciliation is proving fresh broker truth") two seconds after another bot's entry on the same bar | Two bots decided on the same signal in the same second; the second's decision reached the lane while the first's order was being proven. Under a lagging lane (event loop unserved 1 to 4 s) it happened 2 of 2 times on 2026-10-14 | Nothing was sent; the bot decides again on the next bar. For an overlapping pass, wait for a bar where one bot holds and the other enters (it came a minute later). Close the bot pages first. |
| An entry's comparison reads *unknown, FIRST_READING_FAILED* (or *CASH_MOVEMENT_UNREAD*) although Alpaca answered in under 100 ms | The first reading's 1.5 s bound ran out while the lane's event loop was stalled: the account call began late and its answer was discarded | Not a discrepancy and never promoted. Close the bot pages and repeat the step; the lane's log has "the account's option buying power could not be read" at the same second. |
| A stopped bot holds a two-leg structure and its warning says *Use Flatten to close this position* with no Flatten button; the bot page's Sell answers *Flatten is offered for a stopped bot holding one position* | The stranded-position warning offers Flatten only when the bot holds exactly one position, and a safe flatten sends one single-contract exit per leg, never one `mleg` order | Do not close the legs one by one at Alpaca. The Clerk closes the structure itself at its scheduled close, 10 minutes before the session's close for a held sold leg (page: *This system closes the position itself at ...*), as one order. Seen on 2026-10-14: sent 15:50:00 ET, filled 0.17 seconds later. |

## Repair record: 2026-10-13

First written for #3102. Before it no runbook covered option bots or option
orders on Paper, and the capture journal could not show an exchange's two
times, an option order's own terms or a read of the option contract reference.

What the kit changed: the journal's line carries when the request began and
when its answer ended, an option order's submit keeps the body that was sent,
and the option contract reference has a family of its own
([ADR 0032, amendment 2](../architecture/adrs/0032-broker-contract-v2-and-verbatim-capture.md));
`rejudge` judges a record written with judgment off, offline, with the Clerk's
own rows; the bundle script writes the evidence of a window and the recordings
made from it.

Stated here and **not verified when this was written**, each for the first run
to settle: that the setting set in a lane's env file reaches Deploy's own check
in the fleet (section 4); the rebuild command under `compose.fleet.yaml`
(section 2 step 5) and the command that runs the bundle without a rebuilt
image under `compose.fleet.dev.yaml` (section 7); the controls an order from
outside is reviewed with, which
this runbook names by what they show; the sign Alpaca reports for a received
order (section 6); and how Alpaca moves its option buying power (section 11).
Nothing in this runbook had been run against Alpaca when that was written.

## Run record: 2026-10-13, shape A (#3103)

The first run, on a flat Paper account at option level 3: one bought SPY call,
Deployment Validation, four round trips over two bots (every order a limit
buy to open filled inside its 20 second wait, every exit a market sell to
close). What it settled and what it did not:

- **Settled.** An option order is sent as the body the writer produces and
  Alpaca answers it read whole (`tests/fixtures/alpaca/option_paper_recordings`,
  read by `tests/broker/alpaca/test_paper_option_recordings.py`); the trade
  stream carries option fills (pending_new, new, fill) and each fill is also an
  activity row under the same execution id; the bundle runs from the one-off
  container under `compose.fleet.dev.yaml`; the lane carried the several-leg
  setting from its env file after a recreate.
- **Not shown.** No FEE or CFEE row was posted for these option fills within the
  window, which on Paper shows nothing about this system's fee model. Cash read
  above equity by about the sale proceeds for a few seconds after each exit
  (section 8, a figure read before Alpaca had moved it).
- **Found, fixed.** Every entry's comparison read *unknown, the first reading was
  not ready* (4 of 4): the "before" account read was cancelled when the contract
  listing answered first (#3136, fixed in #3137). No pair was therefore judged
  in this run; a run after #3137 is what shows whether one can be.
- **Procedure facts.** A Dry Run holds every quote line; a Deploy review older
  than about a minute is refused; a Deployment Validation bot re-enters on every
  two green bars and a Stop sells nothing, so stop it when it is flat.
  `alpaca_onboarding_gates.py status --require deploy` refuses an account that
  has no defaults for new bots, whatever the line it prints; read the Deploy page.
- **Not run.** The overlapping pass of shape A, every lifecycle row but *filled
  and closed*, shapes B to D, the hand-placed sign check and the buy-back of one
  sold leg. None of them is stated as shown.

## Run record: 2026-10-14, shape A with the fix, the overlapping pass and shape B (#3103)

The second day, on the same flat Paper account (option level 3), after #3137 and #3171.

- **Settled.** The first entry after #3171 has a complete pair (`A4-long-call-flat-fix-landed`, read by
  `tests/broker/alpaca/test_paper_option_recordings.py`): a resting limit order holds its limit price
  (and a cent) out of option buying power with cash untouched; the fill moves cash and option buying
  power together by the premium and a $0.03 clearing fee. Offline the pair judges *over-reserved*
  by exactly the difference between the limit and the fill plus the unspent half of the fee
  provision. Overlapping long calls net nothing (a bought contract holds no requirement), and the
  Clerk refuses to judge either entry (*another order moved*). A restart with a position held keeps
  the position and ends the run (see Troubleshooting). One closing order closed a one-leg structure
  by Flatten. A bull call spread was sent as one `mleg` order with a **positive** net limit (a
  debit), filled as one order, and the account fell by the debit alone with a requirement of $0.
  The same spread was closed as **one `mleg` market order**, filled, and Alpaca stated the credit
  as a **negative** net price on the order and on the fill (`B2-bull-call-spread-clean-pass`).
  Two bots held the same two contracts at once for two minutes (`B3-overlap-spread`): the account
  showed one position of two of each leg, each parent frame carried its own legs' executions, and
  all eight leg fills were credited from the stream (exact, none from the order lookup, no
  stream-health hold, no uncertainty left open). Offline both complete pairs judge *over-reserved*
  by exactly the limit less the fill ($1.00) and the unspent half of the fee provision ($0.05).
  A restart with a spread held kept the position and the bot's holding; the run ended, and the
  Clerk's own scheduled close (10 minutes before the session's close, owed by what is held) was
  sent at 15:50:00 ET as one `mleg` market order and filled 0.17 seconds after it was sent, at a
  net -2.43, the account flat (`B4-spread-restart-scheduled-close`). The entry's comparison read *unknown, the
  Clerk restarted before the second reading*, as it should.
- **Not observable here.** An entry that rests until the deadline's cancel: Paper filled a limit at
  the ask 0.5 s after accepting it, even with the shortest 1 s wait (and its stream answer reached
  the Clerk 1.7 s later). A cancel that races a fill, a part fill, a restart with an entry resting.
- **Fees.** Paper does post regulatory and clearing fees for option fills, the morning after the
  trading day, as non-trade `FEE` activities dated the trading day: for eight option orders, eight
  `OCC Clearing Fee` rows of -$0.03 plus ORF -$0.12, OPT REG -$0.03, OPT TAF -$0.02 and CAT -$0.01
  (-$0.42). The $0.03 clearing fee also leaves cash at the fill.
- **A figure that lags.** After a sell to close Alpaca's option buying power stayed under cash
  ($2.95, $8.93 and $5.88 on three occasions) until the next order restated it. The next entry's
  *before* reading is that much low and its fall reads smaller than its cost: the pair judges
  *over-reserved* for a reason that is not the entry's (section 8, a figure that had not moved).
- **Found, fixed.** The spread's parent `fill` frame carries no top-level `execution_id` (its legs
  carry theirs), so the adapter refused it, the stream's evidence went unhealthy and the account's
  stream-health hold blocked the spread's exit (#3173). A follow-up (#3174) covers the parent's
  frames opening an outcome-unknown episode until the next pass.
- **Found, fixed, repaired.** A filled spread then read as an outstanding intent and held Deploy
  (#3176). Its legs' executions came only in the parent's frame, which was not credited, so the
  four leg fills were held from the order lookup and every fee on the account read unattributed
  and refused every entry (#3177 credits each leg from the frame). The four already booked were
  repaired once by handing the two recorded parent frames to the lane's own consumer with the
  account's store copied first (see Troubleshooting); the account then showed free to deploy.
- **Found, not fixed.** With two bots on one signal the second was refused
  `RECONCILIATION_IN_PROGRESS` twice in two tries, and two entries read *unknown*, one
  `FIRST_READING_FAILED` and one `CASH_MOVEMENT_UNREAD` (a 1.5 s bound that ran out in a stalled
  loop; Alpaca's own fall was premium plus $0.05 for the second). The lane's event
  loop was unserved 1 to 4 s about 150 times in 30 minutes, with a browser window polling a bot
  page every few seconds; the refusals sent nothing and moved no money. Running the bundle in the
  lane killed the lane (section 7). A stopped bot holding a two-leg structure is offered **no
  Flatten**, though its warning says "Use Flatten to close this position": the warning offers it only
  for one position, and the flatten executor sends one single-contract exit per leg (see
  Troubleshooting). The scheduled close is what closed it.
- **Procedure facts.** Bot pages poll heavily and slow the lane (see Troubleshooting); watch a step
  from the capture journal's `orders` file instead (`option_orders` in the bundle) with the pages
  closed. Each deploy needs a fresh money review inside about a minute and the acknowledgement ticked
  again after any refused attempt.
- **Not run.** Shapes C and D and the hand-placed sign check of a *received* price (it is the owner's,
  at Alpaca's dashboard); the buy-back of one sold leg.

## Run record: 2026-10-15, the spread fee check (#3225)

The Paper journal's activity feed now carries 42 distinct `FEE` rows dated
2026-10-14 for 38 distinct option-leg fills in 24 orders: 10 one-leg orders and
14 two-leg `mleg` orders. The fee rows have no order identifier. One $0.03 OCC
row for each leg fill is therefore an **inferred**, not directly linked,
per-order attribution; the account-day totals are directly observed. The
underlying bundles and order identifiers stay offline.

| Fill time (ms UTC) | Legs | OCC model, order alone | OCC posted, inferred | Posted less model |
|---:|---:|---:|---:|---:|
| 1791985682018 | 1 | $0.03 | $0.03 | $0.00 |
| 1791985860867 | 1 | $0.03 | $0.03 | $0.00 |
| 1791989771937 | 1 | $0.03 | $0.03 | $0.00 |
| 1791989942113 | 1 | $0.03 | $0.03 | $0.00 |
| 1791990665693 | 1 | $0.03 | $0.03 | $0.00 |
| 1791991012782 | 1 | $0.03 | $0.03 | $0.00 |
| 1791992823371 | 1 | $0.03 | $0.03 | $0.00 |
| 1791992827020 | 1 | $0.03 | $0.03 | $0.00 |
| 1791993001228 | 1 | $0.03 | $0.03 | $0.00 |
| 1791993001335 | 1 | $0.03 | $0.03 | $0.00 |
| 1791993543721 | 2 | $0.05 | $0.06 | +$0.01 |
| 1791995484539 | 2 | $0.05 | $0.06 | +$0.01 |
| 1791999844075 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792000021098 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792000503610 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792000681153 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792000803224 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792000983546 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792001043546 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792001106712 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792001220716 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792001283968 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792002543815 | 2 | $0.05 | $0.06 | +$0.01 |
| 1792007400459 | 2 | $0.05 | $0.06 | +$0.01 |

Across the account day, `settle_option_session` rounds OCC once and predicts
$0.95; Alpaca posted 38 × $0.03 = $1.14, a $0.19 difference. The model
predicts no OPT TAF on this date; Alpaca posted $0.06 for 19 sold contracts.
ORF ($0.57), CAT ($0.02) and OPT REG/SEC ($0.11) agree exactly. Total posted
fees were $1.90 against the model's $1.65, $0.25 more. The two model differences
are tracked separately in #3306; the qualification does not change the model.

At the account's Home read before the next experiment, there were no running
bots or held positions and no fee notice was visible. Home still displayed
`Fees not posted yet` of $0.36. The deploy check initially reported an IBKR
disconnect and an exposure hold, then the Gateway reconnected and the hold
cleared; the remaining `Profile defaults pre-fill future deployments only`
row is not a fee notice. Recheck Home and the account's posture immediately
before the owner's hand-placed sign check.

## Run record: 2026-10-15, the credit sign check (#3225)

The owner authorized Paper-only orders on 2026-10-15. The Alpaca dashboard was
not signed in, so this step used the **Paper trading API**, with the endpoint
fixed to `https://paper-api.alpaca.markets`, rather than the dashboard action
section 6 describes. It was an outside order, not a bot pass. Before submission
Alpaca reported option level 3, no position and no working order; IBKR supplied
live quotes. The order sold one SPY Oct 16 776 call and bought one 777 call. The
$1-wide spread's maximum Paper loss at the $0.14 credit was $86 before fees.

| Paper order | Limit sent | Alpaca's accepted limit | Alpaca's filled net | Leg fills |
|---|---:|---:|---:|---|
| Earlier shape B bull call spread, bought (`B1-bull-call-spread-flat`) | +$2.14 | +$2.14 | +$2.13 | Bought lower strike, sold higher strike |
| Outside bear call spread, opened at 1792080283912 ms UTC | −$0.14 | −$0.14 | −$0.14 | Sold 776 at $0.60, bought 777 at $0.46 |
| Outside bear call spread, closed immediately | Market | — | +$0.16 | Bought 776 at $0.61, sold 777 at $0.45 |

The opening order filled at 1792080283975 ms UTC; the single closing `mleg`
order filled at 1792080284687 ms UTC. At 1792080286961 ms UTC, Alpaca again
reported **no positions and no working orders**. Cash and option buying power
both read $99,926.65, down $2.10 from the preflight $99,928.75. The difference
is the $2.00 round-trip spread loss plus $0.10 of Paper cash fee effect. The
private API order answers and bundle stay outside Git. The bundle
`3225-credit-sign-check` says `Not read: the bot`, as expected for an outside
order; its stream, activity and account journal were sanitized into
`tests/fixtures/alpaca/option_paper_recordings/3225-credit-sign-check` and
checked by `test_paper_option_recordings.py`.

The outside `mleg` parents have no single symbol or side. The Clerk contained
both as unrecordable outside orders and held new entries. After the exact broker
orders and flat account were reviewed, **Reconcile now** succeeded and both
orders were acknowledged in the Paper Activity recovery control. The hold
cleared; #3309 tracks the representation gap. The short leg's Alpaca `FILL`
activity says `sell_short`, which the fee population reader currently refuses.
Home then reported incomplete fee-bearing fill coverage and held Paper bot
entries. This is a deliberate fail-closed rule, so no side was relabeled and no
fee model was loosened. #3308 tracks the outside-option fee gap. The API sign
and fill are shown; the dashboard-specific action is unverified. Shapes C and
D cannot be called qualified until the fee evidence and admission gate are
repaired and their bot passes run.

Two Dry Run launches checked the form and live IBKR selection without sending
another Paper order. For shape C, `spy-dv-20261015-1210` selected a one-contract,
$1-wide bear call spread (sell 774 call, buy 775 call), with a quoted $24 credit
and $76.10 set aside from $90 simulated cash. It stopped flat before a
two-green-bar signal; it placed no order. For shape D,
`spy-dv-20261015-1215` selected an iron condor with $1 wings (buy 765 put,
sell 766 put, sell 773 call, buy 774 call). Its money review showed a $47
credit and $53.18 set aside from $70 simulated cash. The IBKR feed remained
continuous, but the three decision bars at 11:17, 11:18 and 11:19 local time
produced no entry. It stopped at 11:19:26, released all $70 and showed no
simulated position, order or fill. These launches show structure resolution,
budgeting and a flat stop, **not** an entry, exit or overlap pass of C or D.
The Paper account still had zero positions and zero open orders on a direct
broker read before teardown.

With the Paper account flat and both rehearsals finished, the temporary
`ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=true` line was removed from the
gitignored Paper lane env file and only `alpaca-paper-clerk` was recreated. The
running container now has the several-leg setting absent, the buying-power
judgment and Live option settings absent, `IBKR_BROKER_ENABLED=true`,
`IBKR_READONLY=true` and the unchanged 84-line option quote budget. Its health
and roster returned ready, and a fresh account journal line retained
`began_at_ms`. The coordinator's Paper `budget-preview` read of a two-leg
structure returned `OPTION_SEVERAL_LEGS_SETTING_OFF`, saying a Dry Run still
takes it. The Mac locked before the same row could be re-read in the Deploy
screen; the coordinator read is the Deploy form's backing check. Nothing was
promoted; several-leg Paper orders and Live option bots remain off.

## Run record: 2026-10-16, the credit spread's posted fees (#3225)

The October 16 journal contains four distinct leg fills and eight `FEE` rows
dated 2026-10-15 for the outside credit-sign round trip. It proves the fees
were present at capture, not when they first became visible. The OCC rows'
`created_at` values name October 15. Each OCC row also names an `execution_id`
that matches one fill activity's ID suffix; that fill names its leg order.
The reader test proves all four links and their $0.03 charges. This supplies
direct per-leg clearing-fee evidence, beyond the earlier count-based allocation.

| Component | Canonical model, four fills | Alpaca posted | Posted less model |
|---|---:|---:|---:|
| OCC clearing | $0.10 | $0.12 (four $0.03 rows) | +$0.02 |
| OPT TAF | $0.00 | $0.01 | +$0.01 |
| ORF | $0.06 | $0.06 | $0.00 |
| CAT | $0.01 | $0.01 | $0.00 |
| OPT REG/SEC | $0.01 | $0.01 | $0.00 |
| Total | $0.18 | $0.21 | +$0.03 |

The model comparison uses `fees_for_option_fill` and `settle_option_session`
over the four previously recorded fills and the two recorded contract
multipliers. This is another observation of #3306's OCC and OPT TAF difference,
classified as a fee posting; no rate, rounding rule or tolerance was changed.
The sanitized activity recording is
`tests/fixtures/alpaca/option_paper_recordings/3225-credit-fees-20261016`, read by
`test_paper_option_recordings.py`. Its offline bundle has the expected
`Not read: the bot` because the sign check was an outside order; it supplies
no bot entry comparison. It has no unread journal evidence or unsanitized body.

At the preflight beginning at 1792160737448 ms UTC, Alpaca reported level 3,
zero positions, zero open orders, and $99,926.54 cash and option buying power.
The roster had no running or held bot. Home and the protected money read were
ready with no fee admission refusal, $99,926.18 free to deploy and $0.36 still
shown as fees not posted yet. The separate $2.68 historical outside-fill
disagreement remains disclosed under #3311. IB Gateway reconnected; the Paper
Clerk retained read-only IBKR market data and its 84-line option budget.
Several-leg Paper orders, buying-power judgment and Live option bots were off.

## Run record: 2026-10-16, the owner's dashboard credit sign check (#3225)

The account began flat with no working order or running bot. The owner placed
one SPY October 19 $774/$775 call credit spread at Alpaca's Paper dashboard:
sell one lower call, buy one higher call. The first limit, `-0.33`, was accepted
and rested without a fill. It was cancelled at the dashboard; both legs had
zero filled quantity and Alpaca again reported no position or working order.
This shows a manual cancellation, not the bot's entry-deadline cancellation.

The owner submitted a fresh limit at `-0.15`, which filled at `-0.15`: the
short leg at $0.51 and the long leg at $0.36. The owner then submitted one
whole-spread market close with both closing intents. It filled at `+0.18`:
the short call bought back at $0.55 and the long call sold at $0.37. Alpaca's
dashboard showed the close filled; its API reported no positions and no open
orders at 1792165638173 ms UTC. Alongside shape B's positive debit limit, this
completes the separate dashboard sign check required before C and D.

Cash and option buying power went from $99,926.54 to $99,923.44. The recording
test pins the $3.10 cash decrease separately from the $3.00 net premium cost
and the additional $0.10 cash reduction. That difference is an observation,
not a final posted-fee attribution; the new fill day's fee check remains due.

The Clerk quarantined the three outside parent orders under the existing
#3309 gap. Each was reviewed against Alpaca and acknowledged after the account
was flat; recovery then reported no active hold. The unrelated $2.68 historical
outside-fill discrepancy remains #3311. No capability was promoted and Live
option bots and buying-power judgment stayed off.

The recording is `tests/fixtures/alpaca/option_paper_recordings/3225-dashboard-credit-sign-check-20261016`,
read by `test_paper_option_recordings.py`. Its private bundle covers
1792165220000 through 1792165705503 ms UTC. Its only unread piece is the
expected absence of a bot named `outside-dashboard-sign-check`: these were
outside orders, with no bot buying-power comparison. No journal evidence was
unread or left unsanitized. This is the sign check, not a C or D Paper bot pass.

## Run record: 2026-10-16, shape C on a flat account (#3225)

After the dashboard sign check and the owner's fresh go-ahead, Deployment
Validation ran one $1-wide SPY October 19 bear-call spread with $90 allocated.
The account began flat. The terms were an 8% maximum leg bid-ask gap, a
20-second entry wait, a whole-structure cost limit of 10% of allocated cash,
and a 45-minute expiry-close lead. IBKR supplied the live data; only the
temporary several-leg Paper setting was enabled. Buying-power judgment and
Live option bots remained off.

The strategy entered after two consecutive green bars. Its one `mleg` limit
order sold the $770 call and bought the $771 call at `-0.31`; both legs filled
once, at $1.25 and $0.94. Three decision bars later, its one `mleg` market
close bought the short at $1.21 and sold the long at $0.89, a `+0.32` debit.
The bot was stopped flat at 1792168878820 ms UTC, before another entry.

| Reading | Cash | Option buying power |
|---|---:|---:|
| Before entry | $99,923.44 | $99,923.44 |
| Spread held | $99,954.39 | $99,854.39 |
| After close | $99,922.34 | $99,922.34 |

The complete entry pair judges **over-reserved** offline: the bot set aside
$69.10, the account requirement grew by $100, and Alpaca's option buying power
fell by $69.05. The received $31 premium explains the $69 requirement net of
premium; the $0.05 difference between commitment and the observed fall is the
unused part of the $0.10 entry fee provision. This is classified as a fee
posting, not a payoff-rule error. No tolerance or calculation was changed.
The round trip's $1.10 cash decrease is separate from the bot's $1.18 loss
including estimated fees; final posted fees for October 16 remain due.

The bundle covers 1792167934490 through 1792168902914 ms UTC, with two orders,
one complete comparison and nothing unread. Its sanitized recording is
`tests/fixtures/alpaca/option_paper_recordings/3225-C1-credit-flat-20261016`,
read by `test_paper_option_recordings.py`: writer round trips, signed fills,
per-leg execution attribution, held positions and return of buying power.
This records the flat pass only; it does not establish an overlap, a resting
entry's deadline cancellation, a partial fill, a cancel/fill race or a restart.

## Run record: 2026-10-16, shape C with two bots holding at once (#3225)

Two Deployment Validation bots each received $90, with the same structure and
terms as the flat pass. The first bot initially entered at `-0.31` and closed
at `+0.30` before the second produced an entry signal; that cycle is not an
overlap. The account was flat at 1792169383683 ms UTC, the beginning of the
recorded overlap window.

Both then entered the $770/$771 call spread at `-0.28`, each as one limit
order. The fills were 3.389 seconds apart. Alpaca showed two short $770 calls
and two long $771 calls while each bot held one of each. Both exited three
decision bars later, each as one market order at `+0.28`, with all eight leg
executions attributed from their respective parent frames. The bots were
stopped flat at 1792169916190 and 1792169916870 ms UTC. Alpaca reported no
positions or open orders at 1792169935373 ms UTC; cash and option buying power
both read $99,923.04.

| Reading | Cash | Option buying power |
|---|---:|---:|
| Before the overlapping entries | $99,923.24 | $99,923.24 |
| First spread held | $99,951.19 | $99,851.19 |
| Both spreads held | $99,979.14 | $99,779.14 |
| Both closed | $99,923.04 | $99,923.04 |

Each filled entry added a $100 requirement and $27.95 cash: its $28 credit
less the observed $0.05 cash fee effect. The equal opening and closing net
premiums left a $0.20 round-trip cash reduction across the four orders. These
same-direction spreads received no account offset. Final posted fees remain
due; the cash observations do not substitute for them.

**Neither entry has a clean buying-power comparison.** The first records
`OTHER_ORDER_MOVED`: its readings span both entries, a $200 requirement change
and $144.10 buying-power fall against its own $72.10 commitment. The second
records `FIRST_READING_NOT_READY`: its before-reading had not finished when
its order was sent. Both remain unknown offline. They prove that these
readings cannot isolate each entry, not that the payoff rule or reservation
is wrong; nothing was changed to force a judgment.

Both private bundles cover 1792169383683 through 1792169917088 ms UTC and
leave nothing unread. One sanitized copy of their shared account journal is
`tests/fixtures/alpaca/option_paper_recordings/3225-C2-credit-overlap-20261016`,
read by `test_paper_option_recordings.py`. It preserves both bots' orders,
separate leg executions, the combined positions, and released requirement.
The overlap and whole-spread closes are shown. A clean overlap buying-power
judgment and the other unobserved lifecycle cases remain unverified. No
capability was promoted; the temporary several-leg Paper setting was removed
and the Paper lane recreated after both bots stopped flat.

## Run record: 2026-10-16, shape D on a flat account (#3225)

The owner approved one Paper Deployment Validation bot, one SPY iron condor
with $1 wings and one contract per leg, and $70 allocated. Its completed Dry
Run had already exercised entry and exit. The order terms matched shape C:
8% maximum leg bid-ask gap, 20-second entry wait, 10% whole-structure cost
limit and 45-minute expiry-close lead. IBKR remained enabled and read-only
with 84 option quote lines. Buying-power judgment and Live option bots stayed
off; only the temporary several-leg Paper setting was enabled.

The October 19 contracts entered as one `mleg` limit order at `-0.52`:

| Leg | Entry | Whole-structure close |
|---|---:|---:|
| Buy $758 put | $1.67 | Sell at $1.65 |
| Sell $759 put | $1.86 | Buy at $1.86 |
| Sell $768 call | $1.56 | Buy at $1.56 |
| Buy $769 call | $1.23 | Sell at $1.21 |

Three decision bars after entry, the strategy sent one market close for all
four legs, filled at `+0.56`. All eight executions were attributed to their
own leg orders. The bot stopped flat at 1792170909790 ms UTC. Its $70 budget
released $65.66 after $4 of premium loss and $0.34 of estimated fees; no
position or entry reservation remained. Alpaca's dashboard also showed no
open positions.

| Reading | Cash | Option buying power |
|---|---:|---:|
| Before entry | $99,923.04 | $99,923.04 |
| Condor held | $99,974.94 | $99,874.94 |
| After close | $99,918.84 | $99,918.84 |

The complete entry pair judges **over-reserved** offline: $48.18 committed,
$100 account requirement added, $48.10 fall in option buying power. The $52
credit leaves $48 of requirement net of premium. The remaining $0.08 is the
unused part of the $0.18 entry fee provision against the observed $0.10 cash
charge, classified as fee posting. The round trip's $4.20 cash reduction is
not the final posted fee total; October 16's posted fee check remains due.
No tolerance or calculation was changed.

The private bundle covers 1792170561661 through 1792170921343 ms UTC, with two
orders, one complete comparison and nothing unread. The sanitized recording
is `tests/fixtures/alpaca/option_paper_recordings/3225-D1-condor-flat-20261016`,
read by `test_paper_option_recordings.py`. The same writer, signed-fill and
per-leg activity tests now cover both two- and four-leg structures; the new
recording also pins the contracts, held positions and returned buying power.

**Overlap was not attempted.** Deploy reserves up to 84 candidate quote lines
for this four-leg plan, so two such bots require 168 against the configured
84. The owner approved only the single-bot pass. The quote allowance was not
raised and the gate was not bypassed. This is a precondition for the missing
overlap, not evidence that it works or that the admission check is defective.
The isolated short-leg buy-back and unobserved lifecycle cases also remain
unverified. Nothing was promoted. The temporary several-leg setting was
removed, the Paper lane recreated, and Deploy again refused the four-leg
plan because that setting was off.

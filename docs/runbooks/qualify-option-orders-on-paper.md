# Qualify option orders on a Paper account

This is the operator procedure for the first option orders this system sends to
Alpaca. Nothing in this repository has sent one: every option path was proven
on stand-ins and on Alpaca's published examples. This runbook sets one Paper
account aside, turns one temporary setting on for its Clerk, runs the orders
through their whole life, sets what Alpaca did beside what the Clerk worked
out, and ends with the setting off again or with a capability promoted on the
evidence. Run terminal commands from the repository root. Stop at any failed
command and use the troubleshooting table.

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

1. Check the line is not already there, then add it:

   ```bash
   grep -c '^ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED=' "$ENV_FILE"     # 0
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
podman exec "$LANE" python -m scripts.option_qualification_bundle bundle \
  --artifacts-root /app/artifacts/alpaca_clerk --account-id "$ACCOUNT_ID" --bot "$BOT" \
  --from-ms "$FROM_MS" --to-ms "$TO_MS" \
  --capture-dir /app/artifacts/alpaca_clerk/broker_captures --out-dir "$OUT"
podman cp "$LANE:$OUT" "$EVIDENCE/$STEP"
cat "$EVIDENCE/$STEP/summary.md"
```

The window is `[from, to)`. The command only reads the Clerk's store (read-only
and query-only: it takes no lease) and the journal, and writes the one new
folder. It exits `0` when everything was read, `3` when it wrote the folder
with pieces it could not read (the summary names each under **Not read**), and
`2` when it was refused and wrote nothing. A folder that is not new or empty is
refused; so is one inside the Clerk's folder or the journal.

If section 2 found the bundle script missing from the image **and the lane runs
under `compose.fleet.dev.yaml`**, run it in a one-off container that mounts this
checkout's scripts, shares the lane's volumes (which include the bind-mounted
`app/`), and has no network at all. Under `compose.fleet.yaml` this does not
work: `app/` is baked into the image, an image without the kit has no `rejudge`
or `utc_day`, and the script fails with an ImportError only after orders were
sent. That is why section 3 step 5 checks before the go-ahead, and why the
image is rebuilt there instead:

```bash
IMAGE=$(podman inspect "$LANE" --format '{{.ImageName}}')
podman run --rm --network none --volumes-from "$LANE" -e POLYGON_API_KEY= \
  -v "$PWD/PythonDataService/scripts:/app/scripts:ro,z" "$IMAGE" \
  python -m scripts.option_qualification_bundle bundle \
  --artifacts-root /app/artifacts/alpaca_clerk --account-id "$ACCOUNT_ID" --bot "$BOT" \
  --from-ms "$FROM_MS" --to-ms "$TO_MS" \
  --capture-dir /app/artifacts/alpaca_clerk/broker_captures --out-dir "$OUT"
```

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
  recordings --bundle-dir "$EVIDENCE/$STEP" \
  --out-dir tests/fixtures/alpaca/option_paper_recordings/$STEP
```

Recordings are added **beside** `tests/fixtures/alpaca_documented/` and the
tests that mutate an answer into a hostile one. Nothing there is removed or
swapped out: a recording shows one account on one day, and those cover states
Paper never produced. A recording whose line could not be sanitised is named in
the output and left out.

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

- **Regulatory fees.** Paper charges none, so a missing fee row does not show
  that this system's fee model is right. Fee accounting stays unverified here.
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
| Recordings refused: *still carries the account id* | A sanitised line still holds the account id (as a key, for one) | Nothing was written. Read the line in the bundle, tell the owner, and do not commit it. |
| An entry's comparison reads *unknown* for a reason other than judgment off | The pair was not clean (another order moved, cash moved, a reading failed or was stale, the outcome was not proven) | It stays unknown with its reason in the bundle. Repeat the step on a quieter account; never promote an unknown. |
| A notice that Alpaca took more than the bot set aside | Judgment is on for this Clerk (it must not be) and a pair judged *under-reserved* | Read section 3 step 4. The bot's own page has the owner's review; do not review it until the cause is classified. |
| Fees unresolved: every bot on the account waits | The first posted fee row of an option day cannot be attributed yet | Read the account's Home line for it. This is why the account is set aside. Record it as a finding. |

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
Nothing in this runbook has been run against Alpaca.

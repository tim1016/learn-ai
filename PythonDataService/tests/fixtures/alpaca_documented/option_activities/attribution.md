# Fixture attribution — assignment, exercise and expiry activities (documentation examples)

- **broker:** alpaca
- **reference_kind:** `documentation_example`. **Not a recording.** To be replaced by a recording from an Alpaca account the first time one is assigned, exercises or lets an option expire.
- **read on:** 2026-10-06
- **regenerate:** `python -m scripts.fixture_generators.alpaca_documented_option_activities` (from `PythonDataService/`)

## Sources

| File | Page | Page last updated |
|---|---|---|
| `documented_activities.json` → `trading_api` | Non-Trade Activities for Option Events, https://docs.alpaca.markets/docs/non-trade-activities-for-option-events | 2025-09-24 |
| `documented_activities.json` → `broker_api` | Options Trading Overview, https://docs.alpaca.markets/docs/options-trading-overview (the sections Exercising, Assignment and Expiration) | 2026-09-16 |
| `documented_activity_types.json` → `reference_*` | Retrieve Account Activities, https://docs.alpaca.markets/reference/getaccountactivities-2 (the OpenAPI schemas `ActivityType` and `NonTradeActivities`). Retrieve Account Activities of Specific Type, https://docs.alpaca.markets/reference/getaccountactivitiesbyactivitytype-1, carries the same two schemas; the generator stops if they differ | 2026-05-27 |
| `documented_activity_types.json` → `account_activities_page_option_activity_types` | Account Activities, https://docs.alpaca.markets/docs/account-activities (the Activity Types table) | 2026-05-25 |

Each list of activities is keyed by the heading it sits under on its page and
holds the rows of that heading's one code block, in the page's order. Nothing
is edited.

**`broker_api` is a page of a different product.** Options Trading Overview
belongs to Alpaca's Broker API, which partners use to run accounts for their
own customers; this repo trades through the Trading API. Its examples are
kept because they are the only ones Alpaca publishes of an exercised put, an
assigned put and an expired put, and the only ones in which each row has an
id of its own. They show the same activity codes and the same fields as the
Trading API page, plus `account_id`. They are not proof of what a Trading API
account is sent.

## What the pages state, and where it is used

From Options Trading (https://docs.alpaca.markets/docs/options-trading, last
updated 2026-09-16):

- "Options assignments are not delivered through websocket events. To check
  for assignment activity (non-trade activity, or NTA events), you’ll need to
  poll the REST API endpoints."
- "There are new non-trade activities (NTAs) entry types for options, however
  the schema stays the same. These NTAs reflect exercise, assignment, and
  expiry."
- "On PAPER NTAs are synced at the start of the following day. While your
  balance and positions are updated instantly, NTAs on PAPER will be visible
  in the Activities endpoint only the next day".

From Non-Trade Activities for Option Events:

| Code | The row carries | In the examples |
|---|---|---|
| `OPEXC`, an exercise | `id`, `activity_type`, `date`, `net_amount`, `description`, `symbol`, `qty`, `status` | `symbol` is the contract's OCC symbol. `qty` is in contracts and negative. `net_amount` is "0" |
| `OPASN`, an assignment | the same eight | `symbol` is the OCC symbol. `qty` is in contracts and positive. `net_amount` is "0" |
| `OPEXP`, an expiry | the same eight | `symbol` is the OCC symbol. `qty` is negative: every example is a bought option expiring. `net_amount` is "0" |
| `OPTRD`, the share trade | the same eight and `price` | `symbol` is the **underlying's**. `qty` is in shares, positive when bought and negative when sold. `price` is a share's. `net_amount` is the cash, negative when paid |

- "The exercise event (OPEXC) is applicable to 2 contracts, and the
  corresponding trade (OPTRD) represents 200 of the underlying shares being
  purchased at a per-share amount of $150 (strike price)."
- "When a contract expires OTM, the Alpaca system will flatten the position
  and no further action is taken."

From the reference (`NonTradeActivities`): an activity's `status` is
`executed`, `correct` or `canceled`; `group_id` is an "ID used to link
activities who share a sibling relationship"; `created_at` and `currency` are
fields of an activity. From its parameters: `category` takes
`non_trade_activity`, `page_size` is 1 to 100, and `page_token` is the id of
the last row of the page before.

## Where two pages disagree, and which was taken

| Subject | One page | The other | Taken |
|---|---|---|---|
| **The code of an exercise** | `OPEXC`: the reference's `ActivityType` list on both activity endpoints, Non-Trade Activities for Option Events, and the Broker API's Options Trading Overview | `OPXRC`: the Activity Types table of the Account Activities page, which lists neither `OPEXC` nor `OPTRD` | **Both are read as an exercise.** The read asks for no code by name, so neither spelling can be refused |
| The price on the share trade of the exercised calls | The Trading API example: `"price": "90"` | The sentence under it: "a per-share amount of $150 (strike price)", and its own `net_amount` of -30000 for 200 shares | The strike. The example is kept as printed; the comparison with the contract's terms names its price as a difference |
| Whether a share trade is listed as an activity type | The reference lists `OPTRD` | The Account Activities page's table does not | It is read |
| An activity's fields | The examples carry `description` and, on `OPTRD`, `price` | The reference's `NonTradeActivities` schema lists neither | `price` is read, because the example and the sentence under it give it a meaning. `description` is not read |
| The id of an event and of its share trade | The Trading API examples give all five rows one id, the id of the reference's dividend example | The Broker API examples give each row its own | Nothing is taken from it. An event and a share trade are each kept once by id among their own kind, so a shared id would lose neither |

## What these examples do not show

No recording exists for any of this. Until one does, it is unverified.

- **A sold option expiring.** Every `OPEXP` example has a negative `qty`. That
  a sold one is positive is inferred from the assignment's sign and is not
  stated anywhere.
- **A partial assignment**, and whether an assignment of several contracts
  arrives as one row or several.
- **What links a share trade to its event.** No example row names the other.
  `group_id` is in the reference's schema and in no option example.
- **A row that is `correct` or `canceled`**, and what either does to the row
  it concerns.
- **The time of day.** A row has a `date` only. `created_at` is in the
  schema and in no option example.
- **When a live account's rows become readable.** Only Paper's delay is
  stated.
- **That the non-trade category returns these rows**, and in what order next
  to fee and dividend rows of the same date.
- **`OPCA` and `OPCSH`**, an option corporate action and a cash deliverable.
  The reference lists the codes and no Trading API page shows a row.
- **An event with a net amount other than "0".** All nine event rows carry
  "0". A row that states cash of its own is refused as unreadable.
- **Fees on the share trade**: whether `net_amount` is ever net of a fee.
- **An exercise or assignment the account could not settle**: the Options
  Trading page says a position the account cannot afford to exercise is sold
  before expiry, which would arrive as fills and not as these activities.

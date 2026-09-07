# Attaching a live Alpaca account — what it would take

**Status:** Scope memo, resolved. The owner chose rung 2 on 2026-09-07; the decision is [ADR 0059](adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md) (Accepted 2026-09-07). This memo stays as the evidence base it cites. No code written.
**Date:** 2026-09-07
**Question asked:** *"I would like to attach my live trading account in such a way that MFA is enabled on my account on Alpaca."*

This memo answers the MFA half factually, then prices the attach half honestly. It
argues against [ADR 0042](adrs/0042-sealed-signal-and-account-scoped-custody-authorities.md)
and lives next to it deliberately.

---

## 1. The MFA half, answered

**MFA is already on, and it is not the control you want it to be.**

Alpaca requires MFA before an account can use the Trading API at all — paper or
live. If paper keys work today, MFA is enabled. There is nothing to turn on and
nothing for this repo to build.

MFA gates **dashboard sign-in**. It does not gate API requests. This repo
authenticates with a header key pair and nothing else
([`client.py:150`](../../PythonDataService/app/broker/alpaca/client.py)):

```python
TradingClient(
    api_key=settings.api_key_id,
    secret_key=settings.api_secret_key,
    paper=settings.is_paper,
    raw_data=True,
)
```

There is no interactive session and no MFA challenge in that path.

| MFA does | MFA does not |
|---|---|
| Stop someone with your password from signing in | Protect a key that has already been issued |
| Stop them minting a **new** live key | Challenge, throttle, or log an API order |
| Stop dashboard-initiated funds movement | Limit what an attached key can do |

So: keep MFA on — it is strictly good and it guards the key-minting page. But
**a leaked live API secret is fully exploitable with MFA enabled.** MFA cannot be
the reason live attachment is safe.

### The control that *does* match the intent

Alpaca's per-key **Access Controls** are the real second axis. A key can be minted
as *Read only*, *Full access*, or *Custom* with per-scope levels (Read & Write /
Read only / No Access) across Accounts, Funding, Admin, Crypto, Rebalancing,
Trading, Journaling, Data, Reporting, SSE.

**A live key can read accounts and positions while being structurally unable to
place an order.** That is the hinge of everything below.

---

## 2. Where the lock actually is

The blocker is not MFA and not one toggle. It is 13 deliberate paper-only sites:

| Layer | Site | Effect |
|---|---|---|
| Config | `broker/alpaca/config.py:64` | `_enforce_paper_only` **raises** — client never constructs |
| Ingestion | `broker/alpaca/adapter.py:183` | hardcodes `account_mode="paper"` |
| Bot wire contract | `schemas/broker_bots.py:347` | `Literal["paper"]` |
| Clerk | `clerk/active_authority.py:220` | `LIVE_ACCOUNT_REFUSED` |
| Clerk | `clerk/sqlite/manual_order_runtime.py:114` | `LIVE_ACCOUNT_REFUSED` |
| Clerk | `clerk/sqlite/historical_execution_recovery.py:137` | `LIVE_ACCOUNT_REFUSED` |
| Clerk | `clerk/sqlite/runtime.py:202` | refuses non-paper |
| Clerk | `clerk/sqlite/account_operator_posture.py:323` | refuses non-paper |
| Clerk | `clerk/sqlite/cutover.py:126,696` | `Literal["paper"]` + runtime check |
| Clerk | `clerk/sqlite/dev_reset.py:108` | refuses non-paper |
| Deploy | `services/broker_v2_panel/panel_deploy.py:65` | refuses live deployment |
| Admission | `services/run_admission.py:219` | ADR 0054 corpus gate keys off paper |

And it is decided doctrine, not accumulated drift:

- **ADR 0042:** *"Future real-money Live remains unreachable."*
- **PRD FR-035:** *"No schema, route, UI action, or composition root makes real-money [trading reachable]."*
- **ADR 0021:** live identity is *"a hard, fail-closed block independent of the deploy form."*
- **ADR 0011:** exists specifically because a hardcoded *"Paper trading mode — no real money at risk"* banner was a trust anchor that did not consult reality.

One nuance worth knowing: `BrokerAccountSnapshot.account_mode` is **already**
`Literal["paper", "live"]` (`broker/contract/models.py:139`). The contract admits
live; the adapter refuses to derive it. This is a policy lock, not a typing
problem — which is what makes rung 1 below tractable.

---

## 3. Rung 0 — do now, no code

1. Confirm MFA uses an authenticator app, not SMS.
2. **Mint keys least-privilege.** Never reuse a full-access live key where a
   read-only one suffices.
3. Keys go in `.env` only. Never committed, never in `compose.yaml` defaults.
4. Verify the control plane is closed before any live key exists on the box:
   `DATA_PLANE_CONTROL_SECRET` defaults to `""` and
   `DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL` defaults to `False`
   (`app/config.py:59-60`) — mutating broker routes fail closed. Confirm neither
   has been loosened for local dev.

Already good, worth knowing: the verbatim capture journal redacts any payload key
matching `key|secret|token|password|authorization|apca`
(`broker/capture/journal.py:67`), so captured traffic will not leak credentials.

---

## 4. Rung 1 — live account, read-only observation

**Goal:** see real balances, positions, and history in the app. Never submit,
cancel, or modify a live order.

**Credential:** a live key minted with Access Controls = *Read only*, or *Custom*
with **Trading = Read only**.

### The design that makes this cheap

Do **not** set `ALPACA_MODE=live`. Add a third value — `live_observation`.

Every one of the 13 gates is written as `!= "paper"`. A third mode therefore keeps
**all of them refusing, unchanged**. You open only the read paths you explicitly
touch, and no order path silently becomes reachable because someone widened a
`Literal`. Reusing `"live"` would mean auditing all 13 to confirm each still
refuses; a third value means you never have to.

### Work

1. `config.py` — admit `live_observation`; derive base URL to Alpaca's live host;
   keep `_enforce_paper_only` refusing bare `"live"`.
2. `adapter.py:183` — derive `account_mode` from settings instead of hardcoding.
   Read-path contract already accepts it.
3. Read surfaces — account card, positions, portfolio history.
4. `schemas/broker_bots.py:347` — **leave as `Literal["paper"]`.** Bots stay paper.
5. **Frontend must say LIVE, loudly.** This is the ADR 0011 lesson repeating: a
   real-money account rendered with paper chrome is exactly the failure that ADR
   was written to prevent. Note `shell/broker-banner.component.ts` is IBKR
   *market-data* status — it does not reflect Alpaca account mode at all, so a
   live Alpaca account would surface **no** global indicator today. That gap has
   to close in the same PR, not after.
6. Regenerate the OpenAPI contract immediately after the schema change, or CI fails.
7. Tests — 5 existing assertions pin live refusal. They must keep passing on the
   order path and gain read-path counterparts.

### Posture

Keep the software refusal even though the key is read-only. Two independent
controls — Alpaca-side scope and repo-side refusal — neither trusted alone. If the
wrong key is ever pasted into `.env`, the repo still refuses.

**Risk:** moderate. Worst realistic case is a display bug, or a leaked read-only
key exposing positions — not funds.
**Size:** a real PR across three stacks plus contract regen. Not a toggle.

---

## 5. Rung 2 — real-money order submission

This stops being an engineering task and becomes a risk-ownership decision. It
reverses ADR 0042, ADR 0021's guardrail envelope, and PRD FR-035, so it needs a
superseding ADR before a line of code.

Reopening the 13 gates is the *easy* part. These do not exist at all:

| Missing | Why it bites on live but not paper |
|---|---|
| **Intraday Margin Rule compliance** | PDT was retired on 2026-06-04; FINRA's Intraday Margin Rule replaced it. Risk is now measured continuously as an Intraday Margin Level; any peak deficit (IMD) triggers a margin call due in two business days, and an unmet one freezes the account for 90 days. `pattern_day_trader` is read (`adapter.py:192`) and nothing tracks IML. Paper never issues a margin call; live does. |
| **Settlement / margin** | No T+1 settlement, no margin-call handling, no post-settlement buying-power model. Nothing in the codebase. |
| **Alpaca fee model** | The only commission model is **IBKR** — `engine/execution/commission.py` re-exports `IbkrEquityCommissionModel`. Alpaca live equities are commission-free but pass SEC + FINRA TAF fees on sells. Backtest costs would be modelled against the wrong broker. |
| **Short locate / hard-to-borrow** | Nothing. |
| **Live risk envelope** | No per-account max notional, max daily loss, or dollar-denominated kill switch. Cohort flatten (ADR 0051) and the hard-down breaker (ADR 0046) exist but are *operational* controls, not loss limits. |
| **Realistic fills** | Paper fills are optimistic. Slippage and halts land differently with real size. |

Two additional footguns that are currently safe only *because* live is unreachable:

- The fault-injection seam is paper-gated with the comment *"Never enable in a
  live/production path"* (`app/config.py`).
- `dev_reset` **moves authority aside** rather than deleting it. Pointed at a live
  account, that is a very bad afternoon.

**Sequencing:** rung 2 should not begin until rung 1 has run long enough that
observed live numbers reconcile against expectations.

---

## 6. Recommendation

- **Rung 0 now.** Costs nothing.
- **Rung 1 if you want to see the account.** Scoped, reversible, genuinely useful,
  and the `live_observation` third mode keeps every existing refusal intact.
- **Rung 2 only** behind a written risk envelope, a superseding ADR, and an account
  funded with an amount you are willing to lose outright.

The single highest-value idea here is the third mode value. It buys real-account
visibility without asking any existing safety gate to change behaviour.

## 7. Open questions

- Does Alpaca's *Custom* access control apply to Trading-API key pairs on an
  individual account, or only to Broker-API/OAuth integrations? Verify in your own
  dashboard before designing rung 1 around it.
- Is rung 1's goal *observation* or *reconciliation* — i.e. do you want to see the
  account, or to diff it against what the engine thought would happen? The latter
  is a bigger and more interesting build.

## Sources

- [Alpaca — Credentials Management](https://docs.alpaca.markets/docs/credential-management)
- [Alpaca — Authentication](https://docs.alpaca.markets/us/docs/authentication)
- [Alpaca — Security](https://alpaca.markets/security)
- [Alpaca — Connect to the API](https://alpaca.markets/learn/connect-to-alpaca-api)

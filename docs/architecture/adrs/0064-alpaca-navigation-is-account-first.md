# ADR 0064 — Alpaca navigation is account-first: one account workspace replaces per-page account choosers

**Status:** Accepted 2026-09-16
**Provenance:** The owner's 2026-09-16 grill-with-docs session on why the account list, Bots roster and Gallery felt hard to move between. A walk through the running app found that each operational page carried a different set of links to its siblings, the top bar's Bots/Gallery links dropped the account the operator was in, four menu items led to the same list of account cards, the account page re-rendered every account card above itself, and nothing could switch Paper to Live in place. The broker-wide Bots/Gallery chooser pages had shipped that morning in #2168.
**Related:** ADR 0062 (broker clerk fleet — the lane dimension this navigation sits on), ADR 0059 (Decision 8 amended the same day: the account number leaves the always-on-screen set), ADR 0060 (account nickname), PRD `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md` §9.10 and §13.
**Vocabulary:** `CONTEXT.md` § "Account workspace (resolved 2026-09-16)" (new); § "Broker Desk lenses" renames **Broker Desk** to **account overview**; § "App shell" revised (Botasur, menubar, account badge).

## Decision

1. **The account is the place.** Every page about one broker account sits in an **account workspace**: one account header — the account name as an **account switcher**, the Paper/Live mode, equity and a sync indicator — over five tabs: Overview, Bots, Gallery, Configuration, Deploy strategy. A bot's page sits inside the workspace, under the tab it was opened from (Bots when it has no origin), and its way back returns there. (Decision 1 extended 2026-09-17: Deploy started as a header action opening an overlay drawer; it is a routed tab like the other four, so the sealed evidence it reviews has its own URL and the same "explain in place" and account-switch behavior every other tab already has. See "Amendment" below.)
2. **The account list is the only multi-account page.** `/brokers/alpaca` lists every account (name, mode, readiness, equity, running bots) and opens its workspace. The broker-wide Bots and Gallery chooser routes and the menu's Deploy item redirect to it; the app menu's Alpaca group names only Accounts.
3. **Account badges navigate.** Each top-bar account badge opens its account — on the same tab when the operator is already in a workspace, otherwise on Overview. The unscoped Bots/Gallery quick links are removed.
4. **Switching keeps the tab.** Moving from Paper → Bots to Live lands on Live → Bots; from a bot's page, which the other account does not have, it lands on Bots.
5. **One account name.** The account nickname, or the lane label until one is set. Nothing refuses a duplicate; when two accounts share a name, each is shown with its lane label beside it. The account number appears only on Configuration and in the confirmation of a consequential action.
6. **Window titles carry the account:** "Gallery · Paper".

## Constraints kept

- **FR-091** — nothing remembers a last-used account: no `?clerk=` and no clerk preference key. The workspace is addressed by its URL alone, so the account list is always the entry.
- **FR-092** — canonical routes keep broker, clerk and account identity; Configuration stays lane-scoped and renders inside the workspace from the lane's confirmed account.
- **FR-094 / FR-095** — the switcher and the badges are navigation, never command authority. A command still freezes the target it was opened against, and confirmations still name the exact account.
- **FR-096** — a bad lane deep link still fails in place. A not-ready account keeps its workspace: its Bots and Gallery tabs explain why they cannot open and link to Configuration (this replaces the clerk-only explanation pages). Choosing another account is the operator's explicit act, not a redirect.

## Considered options

- **Page-first, links fixed (the #2168 shape).** Rejected: every page re-asks for the account, so each new page multiplies cross-links, and the header's broker-wide links cannot know the current account without the preference key FR-091 bans.
- **Paper and Live on one Bots page and one Gallery.** Rejected: it puts real-money and paper bots on the same screen, and it works against per-lane independent loading and failure (FR-093).
- **Bots and Gallery as one tab with a List/Wall switch.** Rejected: a third switch beside Trader/Operator costs more explanation than a fourth tab.
- **Remember the last account and skip the list.** Blocked by FR-091; not pursued.
- **Refuse duplicate account names.** Rejected: nicknames live on each lane's own volume, so a lane can only see its own; a cross-lane refusal would need the coordinator to inspect a forwarded configuration write, which ADR 0062 keeps it from doing. Showing the lane label beside a shared name keeps accounts distinguishable without a writer that sees every lane.

## Consequences

- The fleet directory must carry each lane's account nickname so every page can show the same name and spot a shared one.
- The pick-an-account pages from #2168 and the clerk-only surface pages are retired as destinations; their URLs redirect or render as the not-ready workspace tabs.

## Amendment 2026-09-17 — Deploy strategy becomes the fifth tab

Deploy shipped as Decision 1 described it: a header action opening an overlay drawer. Once graduation and continuity work made the account workspace the obvious home for every account-scoped surface, keeping Deploy as the one exception — no URL of its own, no "explain in place" for a not-ready lane, retargeted by closing and reopening rather than by the account-switcher's normal same-tab behavior — cost more than the extra tab does. Deploy is now routed at `.../accounts/:accountId/deploy`, positioned after Configuration, and follows Overview's pattern: it requires a confirmed account and has no lane-scoped fallback, because Deploy cannot target anything without one.

- The broker-wide `?deploy=` intent (arriving from strategy validation with no account chosen yet) is unchanged: it still carries through the account list, and now lands the operator on the chosen account's Deploy tab instead of opening the drawer.
- `AlpacaDeployDrawerComponent` is retired from the account workspace; no other production caller remained.
- FR-094's "not a command surface" language and FR-096's "explain in place" language now cover Deploy exactly as they cover Bots and Gallery.

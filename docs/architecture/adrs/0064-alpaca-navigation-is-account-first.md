# ADR 0064 — Alpaca navigation is account-first: one account workspace replaces per-page account choosers

**Status:** Accepted 2026-09-16
**Provenance:** The owner's 2026-09-16 grill-with-docs session on why the account list, Bots roster and Gallery felt hard to move between. A walk through the running app found that each operational page carried a different set of links to its siblings, the top bar's Bots/Gallery links dropped the account the operator was in, four menu items led to the same list of account cards, the account page re-rendered every account card above itself, and nothing could switch Paper to Live in place. The broker-wide Bots/Gallery chooser pages had shipped that morning in #2168.
**Related:** ADR 0062 (broker clerk fleet — the lane dimension this navigation sits on), ADR 0059 (Decision 8 amended the same day: the account number leaves the always-on-screen set), ADR 0060 (account nickname), PRD `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md` §9.10 and §13.
**Vocabulary:** `CONTEXT.md` § "Account workspace (resolved 2026-09-16)" (new); § "Broker Desk lenses" renames **Broker Desk** to **account overview**; § "App shell" revised (Botasur, menubar, account badge).

## Decision

1. **The account is the place.** Every page about one broker account sits in an **account workspace**: one account header — the account name as an **account switcher**, the Paper/Live mode, equity and a sync indicator — over five tabs: Overview, Bots, Gallery, Configuration, Deploy strategy. A bot's page sits inside the workspace, under the tab it was opened from (Bots when it has no origin), and its way back returns there. (Decision 1 extended 2026-09-17: Deploy started as a header action opening an overlay drawer; it is a routed tab like the other four, so the sealed evidence it reviews has its own URL and the same "explain in place" and account-switch behavior every other tab already has. See "Amendment 2026-09-17" below.) (Amended 2026-09-28 by PRD #2560: Overview, Bots and Gallery are one Home tab, Activity is added, Configuration is Settings, Deploy moves to the account header, and the account switcher is replaced by the top-bar account pills. See "Amendment 2026-09-28" below.)
2. **The account list is the only multi-account page.** `/brokers/alpaca` lists every account (name, mode, readiness, equity, running bots) and opens its workspace. The broker-wide Bots and Gallery chooser routes and the menu's Deploy item redirect to it; the app menu's Alpaca group names only Accounts.
3. **Account badges navigate.** Each top-bar account badge opens its account — on the same tab when the operator is already in a workspace, otherwise on Overview. The unscoped Bots/Gallery quick links are removed.
4. **Switching keeps the tab.** Moving from Paper → Bots to Live lands on Live → Bots; from a bot's page, which the other account does not have, it lands on Bots.
5. **One account name.** The account nickname, or the lane label until one is set. Nothing refuses a duplicate; when two accounts share a name, each is shown with its lane label beside it. The account number appears only on Settings (Configuration until PRD #2560) and in the confirmation of a consequential action.
6. **Window titles carry the account:** "Gallery · Paper".

## Constraints kept

- **FR-091** — nothing remembers a last-used account: no `?clerk=` and no clerk preference key. The workspace is addressed by its URL alone, so the account list is always the entry.
- **FR-092** — canonical routes keep broker, clerk and account identity; Settings (Configuration until PRD #2560) stays lane-scoped — reachable without a confirmed account — and renders inside the workspace from the lane's confirmed account. A broker-wide Settings address names no lane, so it lands on the account list.
- **FR-094 / FR-095** — the account pills (the switcher and badges until PRD #2560) are navigation, never command authority. A command still freezes the target it was opened against, and confirmations still name the exact account.
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

## Amendment 2026-09-28 — Home · Activity · Settings (PRD #2560)

PRD #2560 (the money map) reshapes the workspace this ADR introduced. The account-first decisions stand: the account is the place, the account list is the only multi-account page, badges navigate, switching keeps the tab, one account name, window titles carry the account, and every constraint kept above. What changed:

- **Overview, Bots and Gallery are one Home tab** (PRD D1, D3). Home is the account's money bar, its attention lines, and its bots grouped running, stopped but still holding, Dry Run, and a folded Finished list. Gallery is Home's **Wall** view (`?view=wall`), drawn in the List's order with no drag-to-arrange (D11). The `bots` and `gallery` routes redirect to Home and to its Wall, at both the lane and the account depth. A bot's page sits under Home, so Decision 1's "under the tab it was opened from" and Decision 4's "lands on Bots" now read Home.
- **Activity** (slice 5, #2565) is the account's history and records: the period statement, orders and cash moves, fees, and **Order records and recovery**, which now holds the Clerk recovery panel the Overview desk used to hold.
- **Configuration is Settings** (slice 6, #2566), still lane-scoped (FR-092); the `configuration` URL redirects to it.
- **No page has a Trader / Operator switch** (D2): the Overview desk it switched is deleted, the bot page is one view (slice 3, #2563), and the global top-bar switch, its page registration and the `?lens=` URL parameter are retired (slice 7, #2567). A retired `?lens=` in an old link is dropped on arrival, so the link still lands on the page it named. The option rejected above, "Bots and Gallery as one tab with a List/Wall switch", is now Home's shape, because the switch it would have sat beside is gone.
- **The cohort archive is removed** (D7). A stopped bot leaves the list by itself once it is flat and its money is released, into Home's Finished list. ADR 0052's per-bot archive is unchanged.
- **Deploy is the account header's "Deploy a bot" button** (D3, slice 4 #2564), on every tab, at the same `.../accounts/:accountId/deploy` URL. It is no longer in the tab strip; its page keeps its own URL, its window title ("Deploy a bot") and its explain-in-place behaviour, and the button is marked current while that page is open. "Deploy again" (Home's Finished rows, a stopped bot's page) opens the same page as `deploy?from=<bot>`, pre-filled from that bot's sealed settings.
- FR-096's "explain in place" now covers Home: a lane that cannot serve it says why at `.../clerks/:clerkId/home`. Activity and Deploy need a confirmed account: Activity's tab says so, and a lane with no confirmed account shows no Deploy a bot button.
- **The top-bar pills are the lane switch** (D4, slice 7, #2567). The workspace header's account dropdown is removed; the account's name is the header's heading. Decisions 3 and 4 now describe the pills alone: a pill opens its account on the tab the operator is on (Home from a bot's page, Settings for an account the lane has not confirmed), or Home from outside a workspace, and carries nothing that was open over the workspace. The pill for the account the operator is in is marked current.
- **Each pill carries an attention dot** from the fleet directory's `attention_count`, the lane's own count of things needing the owner: a dot when there are any, a dot marked "?" when the count is unknown (never a zero), none when there are none. The count, or that it is unknown, is in the pill's accessible name and tooltip. The tooltip's readiness sentence is the shared risk check's own words, so a missing daily loss limit reads "Set one in Settings", as Deploy and Settings say it.
- **Lane colour frames the account** (D4): Live red (the existing bear red), Paper cyan, Shadow violet — one token each — draw the workspace frame, the header wash, the open tab's underline, the mode badge, the Accounts cards and the pills. The mode is always worded beside the colour ("LIVE · real money", "PAPER · practice money", "SHADOW · simulated fills on your live account"), so colour never carries it alone. A mode not yet read frames the workspace neutrally and reads "Reading account mode…" (the pill: "Reading…"), never a real-money warning; a failed or unknown read stays loud. Dry Run is never a lane colour.
- **Window titles** (Decision 6) read "Home · Paper".
- **The broker-wide `/brokers/alpaca/settings` and `/configuration` addresses** land on the account list (FR-096), like the retired Bots and Gallery choosers: Settings is one lane's page, and the list is where the account is chosen.

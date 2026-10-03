/** The account workspace's URL vocabulary (ADR 0064 Decision 1, PRD #2560).
 *
 * One broker account is one place: an account header over its Home,
 * Activity and Settings tabs, and the header's Deploy page. Which account
 * that is, which tab is open, and which bot's page is open under Home is
 * carried by the URL alone (nothing remembers a last-used account, FR-091),
 * so deciding all three is a pure function of the URL and lives here rather
 * than in a service every surface would have to inject.
 *
 * Three consumers share it: the workspace shell (which tab to mark current,
 * where each tab and the top-bar account pills lead), `app-menu`'s
 * `activeMenuNodeFor` (every workspace URL highlights Accounts), and
 * `AppComponent`'s window title. Keeping it pure is what lets them agree
 * without one calling the other.
 *
 * It stays dependency-injection free: live account data (the display name, the
 * mode, a bot's label) reaches these functions as plain parameters, read by
 * the component that already holds the directory.
 */

import type { components } from '../api/broker.types';

/** The workspace's tabs, in the order they are presented. Home is the
 * account's money and its bots: Overview, Bots and Gallery merged (D1, D3).
 * History is every bot across every account (#2574). */
export type AccountWorkspaceTab = 'home' | 'activity' | 'history' | 'settings' | 'deploy';

/** One tab's identity and its operator-facing name. */
export interface AccountWorkspaceTabDescriptor {
  readonly id: AccountWorkspaceTab;
  readonly label: string;
}

/** The one place a tab is named: the tab strip renders these and the window
 * title composes from them, so a tab can never pick up a second name. */
const ACCOUNT_WORKSPACE_TAB_LABELS: Readonly<Record<AccountWorkspaceTab, string>> = {
  home: 'Home',
  activity: 'Activity',
  history: 'History',
  settings: 'Settings',
  deploy: 'Deploy a bot',
};

/** The presented tab order (ADR 0064 Decision 1): Home · Activity · History
 * · Settings. Activity — the account's records (PRD #2560) — and History —
 * every bot across every account (#2574) — sit before Settings. Deploy is not
 * in the strip (PRD #2560 D3): it is the header's "Deploy a bot" button, and
 * keeps its routed `deploy` URL, its title and its explain-in-place
 * behaviour. */
export const ACCOUNT_WORKSPACE_TABS: readonly AccountWorkspaceTabDescriptor[] = (
  ['home', 'activity', 'history', 'settings'] as const
).map((id) => ({ id, label: ACCOUNT_WORKSPACE_TAB_LABELS[id] }));

/** Home's query parameter for how its bots are shown: `wall` as chart tiles,
 * anything else (or nothing) as the List. */
export const HOME_VIEW_QUERY_PARAM = 'view';

/** The value of `HOME_VIEW_QUERY_PARAM` that shows the Wall. */
export const HOME_WALL_VIEW = 'wall';

/** Deploy again's query parameter (PRD #2560): `deploy?from=<sid>` opens
 * Deploy pre-filled from that bot's sealed settings, never its money or
 * consent. */
export const DEPLOY_AGAIN_QUERY_PARAM = 'from';

/** Activity's query parameter for one recovery action: `activity?recover=<action_id>`
 * opens the order records and recovery section at that action's button. */
export const ACTIVITY_RECOVER_QUERY_PARAM = 'recover';

/** Golden Search's handoff (#2696): `?golden_qualification=<id>` carries an
 * approved qualification from "Use in Deploy" through the account list to
 * the chosen account's Deploy, which applies its exact settings. */
export const GOLDEN_QUALIFICATION_QUERY_PARAM = 'golden_qualification';

/** Where "Use in Deploy" lands: the account list, where choosing the account
 * is the owner's step (#2696). */
export const GOLDEN_DEPLOY_HANDOFF_ROUTE = '/brokers/alpaca';

type BotHistoryRow = components['schemas']['FleetBotHistoryRow'];

/** History's filters, as its URL carries them (#2574) — the one shape every
 * link to History, its filters and its page read. Opening the tab sets none;
 * Home's Finished fold sets `status`, a bot's own page `account` and `bot`.
 * `account` is one account's lane (its `clerkId`) and `bot` one bot's
 * `strategy_instance_id`. */
export interface BotHistoryUrl {
  readonly account?: string;
  readonly status?: BotHistoryRow['status'];
  readonly world?: BotHistoryRow['world'];
  readonly symbol?: string;
  readonly bot?: string;
  readonly page?: string;
}

/** One tab's operator-facing name. */
export function accountWorkspaceTabLabel(tab: AccountWorkspaceTab): string {
  return ACCOUNT_WORKSPACE_TAB_LABELS[tab];
}

/** Which account a URL is inside, which of its tabs is open, and whether a
 * bot's own page is open under Home. */
export interface AccountWorkspaceLocation {
  readonly broker: string;
  readonly clerkId: string;
  /** `null` on the lane-scoped URLs — Settings and a not-ready Home —
   * which name no account at all (FR-092). The workspace shell resolves the
   * lane's confirmed account for those; the URL cannot. */
  readonly accountId: string | null;
  readonly tab: AccountWorkspaceTab;
  /** The bot whose page is open under Home, or `null` when a tab itself is
   * open. A bot's page is not a tab of its own: it belongs to Home, which is
   * what keeps Home highlighted while it is open. */
  readonly botSid: string | null;
  /** Home is open as its Wall (`?view=wall`). The Wall is how Home is being
   * looked at, so an account pill keeps it, like the tab (story 38). */
  readonly wall: boolean;
}

/** The account — or the account-less lane — a route is built for. A route
 * depends on that identity alone, never on which tab is currently open, so a
 * caller that only knows where it is going does not have to invent one. */
export type AccountWorkspaceAddress = Pick<
  AccountWorkspaceLocation,
  'broker' | 'clerkId' | 'accountId'
>;

/** Just the lane: what a lane-scoped page (Settings, History) is built for. */
export type LaneAddress = Pick<AccountWorkspaceAddress, 'broker' | 'clerkId'>;

/** The same address, for a workspace whose account is confirmed — the only
 * kind a bot's page can have, because a bot runs on an account. */
export interface BoundAccountWorkspaceAddress extends AccountWorkspaceAddress {
  readonly accountId: string;
}

/** A router destination: the commands to navigate with, and the query the
 * destination is opened with. */
export interface AccountWorkspaceLink {
  readonly commands: readonly string[];
  readonly queryParams: Readonly<Record<string, string>>;
}

/**
 * The account-scoped workspace URLs: broker, clerk and account identity
 * (FR-092), optionally followed by Activity's or Deploy's segment or one bot's
 * own page (`bots/:sid`). Home is the bare account URL.
 */
const ACCOUNT_WORKSPACE_URL =
  /^\/brokers\/([^/]+)\/clerks\/([^/]+)\/accounts\/([^/]+)(?:\/(activity|deploy)|\/bots\/([^/]+))?$/;

/**
 * The lane-scoped workspace URLs: Settings, which stays clerk-scoped
 * wherever it is opened from (FR-092); History, which is every account's bots
 * and so belongs to no one account (#2574); and the Home of a lane with no
 * account to serve it, which explains in place why it cannot open and never
 * redirects to another lane (FR-096).
 */
const LANE_WORKSPACE_URL = /^\/brokers\/([^/]+)\/clerks\/([^/]+)\/(settings|history|home)$/;

/**
 * The workspace `url` is inside, or `null` when it is not a workspace URL at
 * all.
 */
export function accountWorkspaceLocation(url: string): AccountWorkspaceLocation | null {
  const path = routePathOf(url);

  const account = ACCOUNT_WORKSPACE_URL.exec(path);
  if (account !== null) {
    const [, broker, clerkId, accountId, segment, sid] = account;
    const tab = segment === 'activity' || segment === 'deploy' ? segment : 'home';
    return {
      broker: decodeURIComponent(broker),
      clerkId: decodeURIComponent(clerkId),
      accountId: decodeURIComponent(accountId),
      tab,
      botSid: sid === undefined ? null : decodeURIComponent(sid),
      wall: tab === 'home' && sid === undefined && routeQueryOf(url).get(HOME_VIEW_QUERY_PARAM) === HOME_WALL_VIEW,
    };
  }

  const lane = LANE_WORKSPACE_URL.exec(path);
  if (lane === null) return null;
  const [, broker, clerkId, surface] = lane;
  return {
    broker: decodeURIComponent(broker),
    clerkId: decodeURIComponent(clerkId),
    accountId: null,
    tab: surface === 'settings' || surface === 'history' ? surface : 'home',
    botSid: null,
    wall: false,
  };
}

/**
 * One tab's router commands for a workspace, or `null` when this workspace has
 * no address for that tab.
 *
 * Settings is always lane-scoped — it is the one tab a lane can serve
 * before it has a confirmed account, which is what makes it the way out of an
 * unbound lane. History is lane-scoped too: it lists every account's bots, so
 * no confirmed account is needed to open it (#2574). Home always has an
 * address: on a lane with no confirmed account it is the page that explains
 * why it cannot open (FR-096). Activity and Deploy are the account's own
 * records and its own action — each needs a confirmed account to target — so
 * an accountless workspace offers neither: Activity's tab is inert and the
 * header shows no "Deploy a bot" button.
 */
export function accountWorkspaceTabRoute(
  address: AccountWorkspaceAddress,
  tab: AccountWorkspaceTab,
): readonly string[] | null {
  if (tab === 'settings') return settingsRoute(address);
  if (tab === 'history') return historyRoute(address);
  if (tab === 'home') return accountWorkspaceHomeRoute(address);
  return address.accountId === null ? null : [...workspaceRoute(address), tab];
}

/** Home's route: the account's own URL, or the lane's not-ready Home. */
export function accountWorkspaceHomeRoute(address: AccountWorkspaceAddress): readonly string[] {
  return address.accountId === null ? [...workspaceRoute(address), 'home'] : workspaceRoute(address);
}

/** The link that opens one bot's page, which belongs to Home. */
export function accountWorkspaceBotRoute(
  account: BoundAccountWorkspaceAddress,
  sid: string,
): AccountWorkspaceLink {
  return { commands: [...workspaceRoute(account), 'bots', sid], queryParams: {} };
}

/** Where an attention line's fix lives (`LaneAttentionAction.destination`). */
export type AccountWorkspaceFixDestination = 'bot' | 'activity' | 'reconcile' | 'settings';

/**
 * The link to an attention line's fix: the line's own bot page, Activity's
 * order records and recovery, that section's Reconcile now, or Settings —
 * lane-scoped wherever it is opened from. `null` only for a bot fix naming
 * no bot, which the backend never sends.
 */
export function accountWorkspaceFixRoute(
  account: BoundAccountWorkspaceAddress,
  destination: AccountWorkspaceFixDestination,
  sid: string | null,
): AccountWorkspaceLink | null {
  switch (destination) {
    case 'bot':
      return sid === null ? null : accountWorkspaceBotRoute(account, sid);
    case 'activity':
      return { commands: [...workspaceRoute(account), 'activity'], queryParams: {} };
    case 'reconcile':
      return {
        commands: [...workspaceRoute(account), 'activity'],
        queryParams: { [ACTIVITY_RECOVER_QUERY_PARAM]: 'reconcile_now' },
      };
    case 'settings':
      return { commands: settingsRoute(account), queryParams: {} };
  }
}

/**
 * History opened with a filter already set — Home's Finished fold opens it on
 * the cleared bots, a bot's own page on that one bot (#2574). The tab itself
 * opens with none. History is lane-scoped, so only the lane is needed.
 */
export function accountWorkspaceHistoryLink(
  lane: LaneAddress,
  filters: Omit<BotHistoryUrl, 'page'>,
): AccountWorkspaceLink {
  const queryParams: Record<string, string> = {};
  for (const [name, value] of Object.entries(filters)) {
    if (value !== undefined) queryParams[name] = value;
  }
  return { commands: historyRoute(lane), queryParams };
}

/**
 * Deploy again for one ended bot: the account's Deploy page, pre-filled from
 * it. Home's Finished rows and a stopped bot's page offer it (PRD #2560);
 * Deploy reads the query and asks the backend for that bot's sealed settings,
 * never its money or consent.
 */
export function accountWorkspaceDeployAgainRoute(
  account: BoundAccountWorkspaceAddress,
  sid: string,
): AccountWorkspaceLink {
  return {
    commands: [...workspaceRoute(account), 'deploy'],
    queryParams: { [DEPLOY_AGAIN_QUERY_PARAM]: sid },
  };
}

/**
 * Where opening an account from *outside* any workspace lands: its Home, the
 * account's own page — or Settings, the one tab a lane with no
 * confirmed account can serve, which is where binding it happens anyway.
 *
 * The account list's cards and the shell's account badges both open an
 * account cold, so both ask this rather than each re-deriving the fallback.
 * Every caller passes the lane's confirmed account whenever it has one,
 * whatever else that lane can report about itself — the substitution turns on
 * the account alone. One account therefore has one front door wherever it is
 * opened from (FR-092).
 */
export function accountWorkspaceEntryRoute(address: AccountWorkspaceAddress): readonly string[] {
  return address.accountId === null ? settingsRoute(address) : workspaceRoute(address);
}

/**
 * Where a top-bar account pill lands (ADR 0064 Decisions 3/4; PRD #2560 D4).
 *
 * The pills are the only way between accounts — the workspace header's
 * account switcher is retired — so inside a workspace a pill keeps the
 * operator on the tab they are on, on the chosen account. A bot's page is
 * never carried across — the chosen account need not run that bot — so a
 * switch from one lands on Home. An account the chosen lane has not
 * confirmed has no Activity or Deploy either; Settings is the tab it can
 * serve, and binding it is what the operator has to do there anyway.
 *
 * Outside a workspace — any other page in the app — there is no tab to keep,
 * so the pill opens that account's front door. A pill for a *different*
 * broker than the workspace the operator is in is a move between brokers, so
 * it takes the front door too.
 *
 * Home's Wall travels with Home: it is how the tab is being looked at, so
 * Live's Wall lands on Paper's Wall (story 38). Nothing that was open *over*
 * the workspace travels: an open Deploy form, a selected custody timeline, a
 * retired `?lens=`, or any later `?`-addressed state closes on a switch
 * instead of retargeting itself at the other account.
 */
export function accountWorkspaceBadgeRoute(
  from: AccountWorkspaceLocation | null,
  target: AccountWorkspaceAddress,
): AccountWorkspaceLink {
  if (from === null || from.broker !== target.broker) {
    return { commands: accountWorkspaceEntryRoute(target), queryParams: {} };
  }
  const tab: AccountWorkspaceTab = from.botSid === null ? from.tab : 'home';
  const commands = accountWorkspaceTabRoute(target, tab) ?? settingsRoute(target);
  const wall = from.wall && target.accountId !== null;
  return { commands, queryParams: wall ? { [HOME_VIEW_QUERY_PARAM]: HOME_WALL_VIEW } : {} };
}

/**
 * The window title inside a workspace (ADR 0064 Decision 6): what is open,
 * then the account it is open on — "Home · Paper".
 *
 * `accountName` is the account's *name* (`laneDisplayNameText`), never its
 * Paper/Live mode. The two are separate facts that only coincide while a lane
 * has no nickname and its label happens to read like its mode. A bot's page
 * titles itself by the bot rather than by the tab it sits under. History is
 * every account's bots, so it names no one account (#2574).
 */
export function accountWorkspaceTitle(
  tab: AccountWorkspaceTab,
  accountName: string | null,
  botLabel: string | null,
): string {
  const subject = botLabel ?? accountWorkspaceTabLabel(tab);
  return accountName === null || tab === 'history' ? subject : `${subject} · ${accountName}`;
}

/** Every workspace URL's first four segments. */
function laneRoute(broker: string, clerkId: string): string[] {
  return ['/brokers', broker, 'clerks', clerkId];
}

/** The workspace's own URL, which Home and Deploy extend: the account's page
 * where the workspace has a confirmed account, the lane's where it does not
 * (FR-092). */
function workspaceRoute(address: AccountWorkspaceAddress): string[] {
  const lane = laneRoute(address.broker, address.clerkId);
  return address.accountId === null ? lane : [...lane, 'accounts', address.accountId];
}

/** Settings' URL — lane-scoped wherever it is opened from (FR-092), so
 * it is the one tab an address can always offer. */
function settingsRoute(address: AccountWorkspaceAddress): string[] {
  return [...laneRoute(address.broker, address.clerkId), 'settings'];
}

/** History's URL — lane-scoped like Settings: it lists every account's bots,
 * so it needs no confirmed account, and it is the same list from every
 * workspace (#2574). */
function historyRoute(lane: LaneAddress): string[] {
  return [...laneRoute(lane.broker, lane.clerkId), 'history'];
}

/** `url` without its query string, fragment, or trailing slash — the part a
 * route pattern matches on. Shared with `app-menu`, which matches its entries
 * against the same path this module parses workspace URLs out of. */
/** The query of a router URL, without its fragment. */
function routeQueryOf(url: string): URLSearchParams {
  return new URLSearchParams(url.split('#')[0].split('?')[1] ?? '');
}

export function routePathOf(url: string): string {
  const path = url.split('#')[0].split('?')[0];
  return path.length > 1 && path.endsWith('/') ? path.slice(0, -1) : path;
}

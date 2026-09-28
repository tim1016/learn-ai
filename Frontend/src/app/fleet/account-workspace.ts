/** The account workspace's URL vocabulary (ADR 0064 Decision 1, PRD #2560).
 *
 * One broker account is one place: an account header over its Home,
 * Activity, Settings and Deploy tabs. Which account that is, which tab is
 * open, and which bot's page is open under Home is carried by the URL alone
 * (nothing remembers a last-used account, FR-091), so deciding all three is a
 * pure function of the URL and lives here rather than in a service every
 * surface would have to inject.
 *
 * Three consumers share it: the workspace shell (which tab to mark current,
 * where each tab and the account switcher lead), `app-menu`'s
 * `activeMenuNodeFor` (every workspace URL highlights Accounts), and
 * `AppComponent`'s window title. Keeping it pure is what lets them agree
 * without one calling the other.
 *
 * It stays dependency-injection free: live account data (the display name, the
 * mode, a bot's label) reaches these functions as plain parameters, read by
 * the component that already holds the directory.
 */

import { LENS_QUERY_PARAM } from '../shared/lens/lens';

/** The workspace's tabs, in the order they are presented. Home is the
 * account's money and its bots: Overview, Bots and Gallery merged (D1, D3). */
export type AccountWorkspaceTab = 'home' | 'activity' | 'settings' | 'deploy';

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
  settings: 'Settings',
  deploy: 'Deploy strategy',
};

/** The presented tab order (ADR 0064 Decision 1). Activity — the account's
 * history and records (PRD #2560) — sits before Settings. Deploy sits
 * last: binding a new strategy to the account, not a fact about it. */
export const ACCOUNT_WORKSPACE_TABS: readonly AccountWorkspaceTabDescriptor[] = (
  ['home', 'activity', 'settings', 'deploy'] as const
).map((id) => ({ id, label: ACCOUNT_WORKSPACE_TAB_LABELS[id] }));

/** Home's query parameter for how its bots are shown: `wall` as chart tiles,
 * anything else (or nothing) as the List. */
export const HOME_VIEW_QUERY_PARAM = 'view';

/** The value of `HOME_VIEW_QUERY_PARAM` that shows the Wall. */
export const HOME_WALL_VIEW = 'wall';

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
}

/** The account — or the account-less lane — a route is built for. A route
 * depends on that identity alone, never on which tab is currently open, so a
 * caller that only knows where it is going does not have to invent one. */
export type AccountWorkspaceAddress = Pick<
  AccountWorkspaceLocation,
  'broker' | 'clerkId' | 'accountId'
>;

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
 * wherever it is opened from (FR-092), and the Home of a lane with no account
 * to serve it, which explains in place why it cannot open and never
 * redirects to another lane (FR-096).
 */
const LANE_WORKSPACE_URL = /^\/brokers\/([^/]+)\/clerks\/([^/]+)\/(settings|home)$/;

/**
 * The workspace `url` is inside, or `null` when it is not a workspace URL at
 * all.
 */
export function accountWorkspaceLocation(url: string): AccountWorkspaceLocation | null {
  const path = routePathOf(url);

  const account = ACCOUNT_WORKSPACE_URL.exec(path);
  if (account !== null) {
    const [, broker, clerkId, accountId, segment, sid] = account;
    return {
      broker: decodeURIComponent(broker),
      clerkId: decodeURIComponent(clerkId),
      accountId: decodeURIComponent(accountId),
      tab: segment === 'activity' || segment === 'deploy' ? segment : 'home',
      botSid: sid === undefined ? null : decodeURIComponent(sid),
    };
  }

  const lane = LANE_WORKSPACE_URL.exec(path);
  if (lane === null) return null;
  const [, broker, clerkId, surface] = lane;
  return {
    broker: decodeURIComponent(broker),
    clerkId: decodeURIComponent(clerkId),
    accountId: null,
    tab: surface === 'settings' ? 'settings' : 'home',
    botSid: null,
  };
}

/**
 * The lens perspective `url` names, or `null` when it names none.
 *
 * Read from the URL rather than from a stored preference: only a perspective
 * the operator addressed is one to keep across a move. Lives here so the one
 * place that knows how to read a workspace URL is also the one place that
 * knows how to write the lens back into the next one.
 */
export function accountWorkspaceLens(url: string): string | null {
  return queryOf(url).get(LENS_QUERY_PARAM);
}

/**
 * One tab's router commands for a workspace, or `null` when this workspace has
 * no address for that tab.
 *
 * Settings is always lane-scoped — it is the one tab a lane can serve
 * before it has a confirmed account, which is what makes it the way out of an
 * unbound lane. Home always has an address: on a lane with no confirmed
 * account it is the page that explains why it cannot open (FR-096). Activity
 * and Deploy are the account's own history and its own action — each needs a
 * confirmed account to target — so they are the tabs an accountless workspace
 * cannot offer.
 */
export function accountWorkspaceTabRoute(
  address: AccountWorkspaceAddress,
  tab: AccountWorkspaceTab,
): readonly string[] | null {
  if (tab === 'settings') return settingsRoute(address);
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
export type AccountWorkspaceFixDestination = 'bot' | 'activity' | 'settings';

/**
 * The link to an attention line's fix: the line's own bot page, Activity's
 * order records and recovery, or Settings — lane-scoped wherever it is
 * opened from. `null` only for a bot fix naming no bot, which the backend
 * never sends.
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
    case 'settings':
      return { commands: settingsRoute(account), queryParams: {} };
  }
}

/** Deploy, starting from one ended bot's settings ("Deploy again"). */
export function accountWorkspaceDeployAgainRoute(
  account: BoundAccountWorkspaceAddress,
  sid: string,
): AccountWorkspaceLink {
  return { commands: [...workspaceRoute(account), 'deploy'], queryParams: { from: sid } };
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
 * Where a shell account badge lands (ADR 0064 Decisions 3/4).
 *
 * A badge is the account switcher reached from the top bar instead of the
 * workspace header, so inside a workspace it behaves identically: the same
 * tab on the chosen account, keeping the lens, dropping whatever was open
 * over it. Outside one — any other page in the app — there is no tab to keep,
 * so the badge opens that account's front door.
 *
 * `from` is the workspace the operator is standing in, or `null` when they
 * are not in one. A badge for a *different* broker than the workspace they
 * are in is a move between brokers rather than between accounts: it has no
 * tab to carry, and `accountWorkspaceSwitchRoute` would build the
 * destination under the broker being left, so it takes the front door too.
 */
export function accountWorkspaceBadgeRoute(
  from: AccountWorkspaceLocation | null,
  target: AccountWorkspaceAddress,
  lens: string | null,
): AccountWorkspaceLink {
  if (from !== null && from.broker === target.broker) {
    return accountWorkspaceSwitchRoute(from, target, lens);
  }
  return { commands: accountWorkspaceEntryRoute(target), queryParams: lensQuery(lens) };
}

/**
 * Where the account switcher lands (ADR 0064 Decision 4): the same tab on the
 * chosen account, keeping the operator's lens perspective.
 *
 * A bot's page is never carried across — the chosen account need not run that
 * bot — so a switch from one lands on Home. An account the chosen lane has not
 * confirmed has no Deploy either; Settings is the tab it can serve for
 * that, and binding it is what the operator has to do there anyway.
 *
 * Nothing that was open *over* the workspace travels: the destination's query
 * is built from `lens` alone rather than merged from the current URL, so an
 * open Deploy drawer, a selected custody timeline, or any later `?`-addressed
 * state closes on a switch instead of retargeting itself at the other account.
 */
export function accountWorkspaceSwitchRoute(
  from: AccountWorkspaceLocation,
  target: Omit<AccountWorkspaceAddress, 'broker'>,
  lens: string | null,
): AccountWorkspaceLink {
  const tab: AccountWorkspaceTab = from.botSid === null ? from.tab : 'home';
  const destination: AccountWorkspaceAddress = {
    broker: from.broker,
    clerkId: target.clerkId,
    accountId: target.accountId,
  };
  const commands = accountWorkspaceTabRoute(destination, tab) ?? settingsRoute(destination);
  return { commands, queryParams: lensQuery(lens) };
}

/**
 * The window title inside a workspace (ADR 0064 Decision 6): what is open,
 * then the account it is open on — "Home · Paper".
 *
 * `accountName` is the account's *name* (`laneDisplayNameText`), never its
 * Paper/Live mode. The two are separate facts that only coincide while a lane
 * has no nickname and its label happens to read like its mode. A bot's page
 * titles itself by the bot rather than by the tab it sits under.
 */
export function accountWorkspaceTitle(
  tab: AccountWorkspaceTab,
  accountName: string | null,
  botLabel: string | null,
): string {
  const subject = botLabel ?? accountWorkspaceTabLabel(tab);
  return accountName === null ? subject : `${subject} · ${accountName}`;
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

/** The destination's query, built from the lens alone — never merged from the
 * URL being left, so nothing open *over* a workspace travels with a move. */
function lensQuery(lens: string | null): Readonly<Record<string, string>> {
  return lens === null ? {} : { [LENS_QUERY_PARAM]: lens };
}

/** `url`'s query parameters, empty when it carries none. */
function queryOf(url: string): URLSearchParams {
  const withoutHash = url.split('#')[0];
  const queryIndex = withoutHash.indexOf('?');
  return new URLSearchParams(queryIndex === -1 ? '' : withoutHash.slice(queryIndex + 1));
}

/** `url` without its query string, fragment, or trailing slash — the part a
 * route pattern matches on. Shared with `app-menu`, which matches its entries
 * against the same path this module parses workspace URLs out of. */
export function routePathOf(url: string): string {
  const path = url.split('#')[0].split('?')[0];
  return path.length > 1 && path.endsWith('/') ? path.slice(0, -1) : path;
}

/** The account workspace's URL vocabulary (ADR 0064 Decision 1).
 *
 * One broker account is one place: an account header over Overview, Bots,
 * Gallery and Configuration tabs. Which account that is, which tab is open,
 * and — on a bot's own page — which tab it was opened from, is carried by the
 * URL alone (nothing remembers a last-used account, FR-091), so deciding all
 * three is a pure function of the URL and lives here rather than in a service
 * every surface would have to inject.
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

/** The workspace's four tabs, in the order they are presented. */
export type AccountWorkspaceTab = 'overview' | 'bots' | 'gallery' | 'configuration';

/** The tabs a bot's page can be opened from, and therefore belongs to. */
export type AccountWorkspaceOriginTab = Extract<AccountWorkspaceTab, 'bots' | 'gallery'>;

/** One tab's identity and its operator-facing name. */
export interface AccountWorkspaceTabDescriptor {
  readonly id: AccountWorkspaceTab;
  readonly label: string;
}

/** The one place a tab is named: the tab strip renders these and the window
 * title composes from them, so a tab can never pick up a second name. */
const ACCOUNT_WORKSPACE_TAB_LABELS: Readonly<Record<AccountWorkspaceTab, string>> = {
  overview: 'Overview',
  bots: 'Bots',
  gallery: 'Gallery',
  configuration: 'Configuration',
};

/** The presented tab order (ADR 0064 Decision 1). */
export const ACCOUNT_WORKSPACE_TABS: readonly AccountWorkspaceTabDescriptor[] = (
  ['overview', 'bots', 'gallery', 'configuration'] as const
).map((id) => ({ id, label: ACCOUNT_WORKSPACE_TAB_LABELS[id] }));

/** The query parameter a link to a bot's page stamps with the tab it left.
 * Written by the Gallery tiles and the roster's links, read back here. */
export const ORIGIN_TAB_QUERY_PARAM = 'from';

/** One tab's operator-facing name. */
export function accountWorkspaceTabLabel(tab: AccountWorkspaceTab): string {
  return ACCOUNT_WORKSPACE_TAB_LABELS[tab];
}

/** The tab a bot's page was opened from, from the stamp its link carried.
 * Anything the URL does not name — a pasted link, a bookmark, an unknown
 * value — belongs to Bots. */
export function accountWorkspaceOriginTab(stamp: string | null): AccountWorkspaceOriginTab {
  return stamp === 'gallery' ? 'gallery' : 'bots';
}

/** Which account a URL is inside, which of its tabs is open, and whether a
 * bot's own page is open under that tab. */
export interface AccountWorkspaceLocation {
  readonly broker: string;
  readonly clerkId: string;
  /** `null` on the lane-scoped URLs — Configuration and the not-ready Bots and
   * Gallery tabs — which name no account at all (FR-092). The workspace shell
   * resolves the lane's confirmed account for those; the URL cannot. */
  readonly accountId: string | null;
  readonly tab: AccountWorkspaceTab;
  /** The bot whose page is open under `tab`, or `null` when the tab itself is
   * open. A bot's page is not a fifth tab: it belongs to the tab it was opened
   * from, which is what keeps that tab highlighted while it is open. */
  readonly botSid: string | null;
}

/** The account — or the account-less lane — a route is built for. A route
 * depends on that identity alone, never on which tab is currently open, so a
 * caller that only knows where it is going does not have to invent one. */
export type AccountWorkspaceAddress = Pick<
  AccountWorkspaceLocation,
  'broker' | 'clerkId' | 'accountId'
>;

/** A router destination: the commands to navigate with, and the query the
 * destination is opened with. */
export interface AccountWorkspaceLink {
  readonly commands: readonly string[];
  readonly queryParams: Readonly<Record<string, string>>;
}

/**
 * The account-scoped workspace URLs: broker, clerk and account identity
 * (FR-092), optionally followed by the tab's own segment and — under Bots —
 * one bot's own page. Overview is the bare account URL, so the canonical URLs
 * are unchanged by the workspace.
 */
const ACCOUNT_WORKSPACE_URL =
  /^\/brokers\/([^/]+)\/clerks\/([^/]+)\/accounts\/([^/]+)(?:\/(bots|gallery)(?:\/([^/]+))?)?$/;

/**
 * The lane-scoped workspace URLs: Configuration, which stays clerk-scoped
 * wherever it is opened from (FR-092), and the Bots and Gallery tabs of a lane
 * with no account to serve them, which explain in place why they cannot open
 * and never redirect to another lane (FR-096).
 */
const LANE_WORKSPACE_URL = /^\/brokers\/([^/]+)\/clerks\/([^/]+)\/(configuration|bots|gallery)$/;

/**
 * The workspace `url` is inside, or `null` when it is not a workspace URL at
 * all.
 */
export function accountWorkspaceLocation(url: string): AccountWorkspaceLocation | null {
  const path = routePathOf(url);

  const account = ACCOUNT_WORKSPACE_URL.exec(path);
  if (account !== null) {
    const [, broker, clerkId, accountId, surface, sid] = account;
    // Only Bots nests a further segment. `…/gallery/anything` addresses
    // nothing, so it is outside the workspace rather than a Gallery tab with a
    // stray tail.
    if (sid !== undefined && surface !== 'bots') return null;
    return {
      broker: decodeURIComponent(broker),
      clerkId: decodeURIComponent(clerkId),
      accountId: decodeURIComponent(accountId),
      tab:
        sid === undefined
          ? tabOfSegment(surface)
          : accountWorkspaceOriginTab(queryOf(url).get(ORIGIN_TAB_QUERY_PARAM)),
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
    tab: tabOfSegment(surface),
    botSid: null,
  };
}

/**
 * One tab's router commands for a workspace, or `null` when this workspace has
 * no address for that tab.
 *
 * Configuration is always lane-scoped — it is the one tab a lane can serve
 * before it has a confirmed account, which is what makes it the way out of an
 * unbound lane. Bots and Gallery have a lane-scoped address too: the tab that
 * explains why it cannot open. Overview is the account's own page and has
 * none, so it is the one tab an accountless workspace cannot offer.
 */
export function accountWorkspaceTabRoute(
  address: AccountWorkspaceAddress,
  tab: AccountWorkspaceTab,
): readonly string[] | null {
  if (tab === 'configuration') return configurationRoute(address);
  if (tab !== 'overview') return accountWorkspaceOriginTabRoute(address, tab);
  return address.accountId === null ? null : workspaceRoute(address);
}

/**
 * The same tab route, narrowed to the two tabs a bot's page can be opened
 * from.
 *
 * Both of those tabs have an address whether or not the workspace has a
 * confirmed account — the lane-scoped one explains in place why it cannot open
 * (FR-096) — so this route always resolves, and a caller that only ever asks
 * for an origin tab needs no null guard for a case it cannot reach.
 */
export function accountWorkspaceOriginTabRoute(
  address: AccountWorkspaceAddress,
  origin: AccountWorkspaceOriginTab,
): readonly string[] {
  return [...workspaceRoute(address), origin];
}

/**
 * The link that opens one bot's page, stamped with the tab it was opened from.
 *
 * The stamp is what makes a bot's page belong to a tab: it keeps that tab
 * highlighted while the page is open and points the page's way back at it. A
 * link that carries no stamp — a pasted URL, a bookmark — belongs to Bots.
 */
export function accountWorkspaceBotRoute(
  account: { readonly broker: string; readonly clerkId: string; readonly accountId: string },
  sid: string,
  origin: AccountWorkspaceOriginTab,
): AccountWorkspaceLink {
  return {
    commands: [
      ...laneRoute(account.broker, account.clerkId),
      'accounts',
      account.accountId,
      'bots',
      sid,
    ],
    queryParams: { [ORIGIN_TAB_QUERY_PARAM]: origin },
  };
}

/**
 * Where the account switcher lands (ADR 0064 Decision 4): the same tab on the
 * chosen account, keeping the operator's lens perspective.
 *
 * A bot's page is never carried across — the chosen account need not run that
 * bot — so a switch from one lands on Bots. An account the chosen lane has not
 * confirmed has no Overview to open either; Configuration is the one tab it
 * can serve, and binding it is what the operator has to do there anyway.
 *
 * Nothing that was open *over* the workspace travels: the destination's query
 * is built from `lens` alone rather than merged from the current URL, so an
 * open Deploy drawer, a selected custody timeline, or any later `?`-addressed
 * state closes on a switch instead of retargeting itself at the other account.
 */
export function accountWorkspaceSwitchRoute(
  from: AccountWorkspaceLocation,
  target: { readonly clerkId: string; readonly accountId: string | null },
  lens: string | null,
): AccountWorkspaceLink {
  const tab: AccountWorkspaceTab = from.botSid === null ? from.tab : 'bots';
  const destination: AccountWorkspaceAddress = {
    broker: from.broker,
    clerkId: target.clerkId,
    accountId: target.accountId,
  };
  const commands = accountWorkspaceTabRoute(destination, tab) ?? configurationRoute(destination);
  return { commands, queryParams: lens === null ? {} : { [LENS_QUERY_PARAM]: lens } };
}

/**
 * The window title inside a workspace (ADR 0064 Decision 6): what is open,
 * then the account it is open on — "Gallery · Paper".
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

/** The workspace's own URL, which every tab but Configuration extends: the
 * account's page where the workspace has a confirmed account, the lane's where
 * it does not (FR-092). */
function workspaceRoute(address: AccountWorkspaceAddress): string[] {
  const lane = laneRoute(address.broker, address.clerkId);
  return address.accountId === null ? lane : [...lane, 'accounts', address.accountId];
}

/** Configuration's URL — lane-scoped wherever it is opened from (FR-092), so
 * it is the one tab an address can always offer. */
function configurationRoute(address: AccountWorkspaceAddress): string[] {
  return [...laneRoute(address.broker, address.clerkId), 'configuration'];
}

/** The tab one URL segment names; the absent segment is Overview. */
function tabOfSegment(segment: string | undefined): AccountWorkspaceTab {
  if (segment === 'bots') return 'bots';
  if (segment === 'gallery') return 'gallery';
  if (segment === 'configuration') return 'configuration';
  return 'overview';
}

/** `url`'s query parameters, empty when it carries none. */
function queryOf(url: string): URLSearchParams {
  const withoutHash = url.split('#')[0];
  const queryIndex = withoutHash.indexOf('?');
  return new URLSearchParams(queryIndex === -1 ? '' : withoutHash.slice(queryIndex + 1));
}

/** `url` without its query string, fragment, or trailing slash. */
function routePathOf(url: string): string {
  const path = url.split('#')[0].split('?')[0];
  return path.length > 1 && path.endsWith('/') ? path.slice(0, -1) : path;
}

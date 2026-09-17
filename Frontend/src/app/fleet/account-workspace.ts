/** The account workspace's URL vocabulary (ADR 0064 Decision 1).
 *
 * One broker account is one place: an account header over Overview, Bots,
 * Gallery and Configuration tabs. Which account that is, and which tab is
 * open, is carried by the URL alone — nothing remembers a last-used account
 * (FR-091) — so deciding both is a pure function of the URL and lives here
 * rather than in a service every surface would have to inject.
 *
 * Two consumers share it: the workspace shell (which tab to mark current)
 * and `app-menu`'s `activeMenuNodeFor` (every workspace URL highlights
 * Accounts). Keeping it pure is what lets those two agree without one
 * calling the other.
 */

/** The workspace's four tabs, in the order they are presented. */
export type AccountWorkspaceTab = 'overview' | 'bots' | 'gallery' | 'configuration';

/** One tab's identity and its operator-facing name. */
export interface AccountWorkspaceTabDescriptor {
  readonly id: AccountWorkspaceTab;
  readonly label: string;
}

/** The presented tab order (ADR 0064 Decision 1). */
export const ACCOUNT_WORKSPACE_TABS: readonly AccountWorkspaceTabDescriptor[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'bots', label: 'Bots' },
  { id: 'gallery', label: 'Gallery' },
  { id: 'configuration', label: 'Configuration' },
];

/** Which account a URL is inside, and which of its tabs is open. */
export interface AccountWorkspaceLocation {
  readonly broker: string;
  readonly clerkId: string;
  readonly accountId: string;
  readonly tab: AccountWorkspaceTab;
}

/**
 * The account-scoped workspace URL shape: broker, clerk and account identity
 * (FR-092), optionally followed by the tab's own segment. Overview is the
 * bare account URL, so the canonical URLs are unchanged by the workspace.
 *
 * Configuration is deliberately absent: it stays lane-scoped at
 * `…/clerks/{clerkId}/configuration` until a later slice renders it inside
 * the workspace, so no URL resolves to that tab yet.
 */
const WORKSPACE_URL =
  /^\/brokers\/([^/]+)\/clerks\/([^/]+)\/accounts\/([^/]+)(?:\/(bots|gallery))?$/;

/**
 * The workspace `url` is inside, or `null` when it is not a workspace URL at
 * all. A bot's own page (`…/bots/{sid}`) is not yet a workspace URL — it
 * moves inside in a later slice — and neither is any clerk-only surface.
 */
export function accountWorkspaceLocation(url: string): AccountWorkspaceLocation | null {
  const match = WORKSPACE_URL.exec(routePathOf(url));
  if (match === null) return null;
  const [, broker, clerkId, accountId, surface] = match;
  return {
    broker: decodeURIComponent(broker),
    clerkId: decodeURIComponent(clerkId),
    accountId: decodeURIComponent(accountId),
    tab: surface === 'bots' ? 'bots' : surface === 'gallery' ? 'gallery' : 'overview',
  };
}

/** One tab's router commands for a workspace. Configuration still points at
 * the lane's own configuration page, which is clerk-scoped and carries no
 * account segment. */
export function accountWorkspaceTabRoute(
  location: AccountWorkspaceLocation,
  tab: AccountWorkspaceTab,
): readonly string[] {
  const lane = ['/brokers', location.broker, 'clerks', location.clerkId];
  if (tab === 'configuration') return [...lane, 'configuration'];
  const account = [...lane, 'accounts', location.accountId];
  return tab === 'overview' ? account : [...account, tab];
}

/** `url` without its query string, fragment, or trailing slash. */
function routePathOf(url: string): string {
  const path = url.split('#')[0].split('?')[0];
  return path.length > 1 && path.endsWith('/') ? path.slice(0, -1) : path;
}

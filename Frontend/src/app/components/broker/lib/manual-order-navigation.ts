import type { ParamMap, Params } from '@angular/router';

const MANUAL_ORDER_QUERY = {
  intent: 'order',
  accountId: 'accountId',
  symbol: 'symbol',
  ticketId: 'ticketId',
  legId: 'legId',
} as const;

export interface ManualOrderTicketRoute {
  readonly intent: 'new';
  readonly accountId: string;
  readonly symbol: string;
  readonly ticketId: string;
  readonly legId: string;
}

interface ManualOrderTicketQuery {
  readonly order: 'new';
  readonly accountId: string;
  readonly symbol: string;
  readonly ticketId: string;
  readonly legId: string;
}

export interface ManualOrderTicketNavigation {
  readonly commands: readonly ['/brokers', string, 'clerks', string, 'accounts', string];
  readonly queryParams: Params;
}

/**
 * The link from a bot's page to the account's manual-order ticket.
 *
 * The path is the account as this workspace routes it (`routeAccountId`), so
 * the link lands in the same workspace every other route uses (hurdle H20:
 * the broker's upper-case spelling opened a second address for one account).
 * The ticket query keeps the broker's own account id, which is what the desk
 * checks the connected account against before opening the ticket.
 */
export function buildManualOrderTicketNavigation(link: {
  readonly broker: string;
  readonly clerkId: string;
  readonly routeAccountId: string;
  readonly accountId: string;
  readonly symbol: string;
}): ManualOrderTicketNavigation {
  const queryParams = {
    [MANUAL_ORDER_QUERY.intent]: 'new',
    [MANUAL_ORDER_QUERY.accountId]: link.accountId,
    [MANUAL_ORDER_QUERY.symbol]: link.symbol,
    [MANUAL_ORDER_QUERY.ticketId]: crypto.randomUUID(),
    [MANUAL_ORDER_QUERY.legId]: crypto.randomUUID(),
  } satisfies ManualOrderTicketQuery;
  return {
    commands: ['/brokers', link.broker, 'clerks', link.clerkId, 'accounts', link.routeAccountId],
    queryParams,
  };
}

export function parseManualOrderTicketQuery(
  params: ParamMap,
): ManualOrderTicketRoute | null {
  if (params.get(MANUAL_ORDER_QUERY.intent) !== 'new') return null;
  const accountId = params.get(MANUAL_ORDER_QUERY.accountId)?.trim();
  const symbol = params.get(MANUAL_ORDER_QUERY.symbol)?.trim().toUpperCase();
  const ticketId = params.get(MANUAL_ORDER_QUERY.ticketId)?.trim();
  const legId = params.get(MANUAL_ORDER_QUERY.legId)?.trim();
  if (!accountId || !symbol || !isUuid(ticketId) || !isUuid(legId)) return null;
  return { intent: 'new', accountId, symbol, ticketId, legId };
}

function isUuid(value: string | undefined): value is string {
  return Boolean(
    value && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value),
  );
}

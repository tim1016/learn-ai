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
  readonly commands: readonly ['/brokers', string];
  readonly queryParams: Params;
}

export function buildManualOrderTicketNavigation(
  broker: string,
  accountId: string,
  symbol: string,
): ManualOrderTicketNavigation {
  const queryParams = {
    [MANUAL_ORDER_QUERY.intent]: 'new',
    [MANUAL_ORDER_QUERY.accountId]: accountId,
    [MANUAL_ORDER_QUERY.symbol]: symbol,
    [MANUAL_ORDER_QUERY.ticketId]: crypto.randomUUID(),
    [MANUAL_ORDER_QUERY.legId]: crypto.randomUUID(),
  } satisfies ManualOrderTicketQuery;
  return { commands: ['/brokers', broker], queryParams };
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

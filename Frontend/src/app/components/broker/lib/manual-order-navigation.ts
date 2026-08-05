import type { ParamMap, Params } from '@angular/router';

const MANUAL_ORDER_QUERY = {
  intent: 'order',
  accountId: 'accountId',
  symbol: 'symbol',
} as const;

export interface ManualOrderTicketRoute {
  readonly intent: 'new';
  readonly accountId: string;
  readonly symbol: string;
}

interface ManualOrderTicketQuery {
  readonly order: 'new';
  readonly accountId: string;
  readonly symbol: string;
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
  } satisfies ManualOrderTicketQuery;
  return { commands: ['/brokers', broker], queryParams };
}

export function parseManualOrderTicketQuery(
  params: ParamMap,
): ManualOrderTicketRoute | null {
  if (params.get(MANUAL_ORDER_QUERY.intent) !== 'new') return null;
  const accountId = params.get(MANUAL_ORDER_QUERY.accountId)?.trim();
  const symbol = params.get(MANUAL_ORDER_QUERY.symbol)?.trim().toUpperCase();
  if (!accountId || !symbol) return null;
  return { intent: 'new', accountId, symbol };
}

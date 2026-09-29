import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../api/broker.types';

/** One page of every bot across every account (#2574), as the coordinator
 * merges it. Every dollar is a Python-authored string; every instant is
 * `int64 ms UTC`. */
export type FleetBotHistoryPage = components['schemas']['FleetBotHistoryPage'];
export type FleetBotHistoryRow = components['schemas']['FleetBotHistoryRow'];
export type FleetBotHistoryGap = components['schemas']['FleetBotHistoryGap'];
export type BotHistoryRun = components['schemas']['BotHistoryRun'];
export type BotHistoryStatus = FleetBotHistoryRow['status'];
export type BotHistoryWorld = FleetBotHistoryRow['world'];

/** Bots per page. */
export const BOT_HISTORY_PAGE_SIZE = 25;

const BOT_HISTORY_URL = '/api/broker-clerks/aggregate/bot-history';

/** The status filter's choices, worded for the control (closed copy map). */
export const BOT_HISTORY_STATUSES: readonly { readonly id: BotHistoryStatus; readonly label: string }[] = [
  { id: 'running', label: 'Running' },
  { id: 'holding', label: 'Stopped · still holding' },
  { id: 'finished', label: 'Finished' },
  { id: 'cleared', label: 'Cleared' },
];

/** The world filter's choices, worded for the control (closed copy map). */
export const BOT_HISTORY_WORLDS: readonly { readonly id: BotHistoryWorld; readonly label: string }[] = [
  { id: 'live', label: 'Live' },
  { id: 'paper', label: 'Paper' },
  { id: 'shadow', label: 'Shadow' },
  { id: 'dry_run', label: 'Dry Run' },
];

/** What the list is narrowed to. `null` is "any"; nothing is pre-selected. */
export interface BotHistoryQuery {
  readonly clerkId: string | null;
  readonly status: BotHistoryStatus | null;
  readonly world: BotHistoryWorld | null;
  readonly symbol: string | null;
  readonly page: number;
}

/** The URL's raw filter values. */
export interface BotHistoryQueryParams {
  readonly account?: string;
  readonly status?: string;
  readonly world?: string;
  readonly symbol?: string;
  readonly page?: string;
}

/** Read the filters out of the URL. A value the list does not know is
 * treated as unset, so an old or hand-edited link still opens the list. */
export function botHistoryQuery(params: BotHistoryQueryParams): BotHistoryQuery {
  const page = Number(params.page);
  return {
    clerkId: params.account || null,
    status: BOT_HISTORY_STATUSES.find((status) => status.id === params.status)?.id ?? null,
    world: BOT_HISTORY_WORLDS.find((world) => world.id === params.world)?.id ?? null,
    symbol: params.symbol ? params.symbol.toUpperCase() : null,
    page: Number.isInteger(page) && page >= 1 ? page : 1,
  };
}

/** The all-accounts bot history, read on demand (never polled). */
@Injectable({ providedIn: 'root' })
export class BotHistoryService {
  private readonly http = inject(HttpClient);

  read(query: BotHistoryQuery): Promise<FleetBotHistoryPage> {
    let params = new HttpParams().set('page', query.page).set('page_size', BOT_HISTORY_PAGE_SIZE);
    if (query.clerkId !== null) params = params.set('clerk_id', query.clerkId);
    if (query.status !== null) params = params.set('status', query.status);
    if (query.world !== null) params = params.set('world', query.world);
    if (query.symbol !== null) params = params.set('symbol', query.symbol);
    return firstValueFrom(this.http.get<FleetBotHistoryPage>(BOT_HISTORY_URL, { params }));
  }
}

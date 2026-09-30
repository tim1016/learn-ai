import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../api/broker.types';
import type { BotHistoryUrl } from '../../../fleet/account-workspace';
import type { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneDisplayNameText } from '../../../fleet/fleet-directory.types';
import type { LaneModeChip } from '../../../services/alpaca-live-verdict.service';

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

/** One filter the owner can set; `page` is the pager's own. */
export type BotHistoryFilter = Exclude<keyof BotHistoryUrl, 'page'>;

/** The URL's raw values: whatever a link, or a hand-edited address, carries. */
export type BotHistoryUrlValues = { readonly [K in keyof BotHistoryUrl]?: string };

/** What the list is narrowed to: each filter set, or `null` for "any".
 * Nothing is pre-selected. */
export type BotHistoryQuery = { readonly [K in BotHistoryFilter]: NonNullable<BotHistoryUrl[K]> | null } & {
  readonly page: number;
};

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

/** A symbol the coordinator accepts: its `US_EQUITY_SYMBOL_PATTERN`
 * (`app/broker/contract/models.py`), checked here so a malformed one in the
 * URL is treated as unset rather than refused. */
const US_EQUITY_SYMBOL = /^[A-Z]{1,5}(?:[.-][A-Z])?$/;
/** The longest bot id the coordinator accepts. */
const MAX_BOT_ID_LENGTH = 128;

/** Read the filters out of the URL. A value the list does not know — an
 * account no lane serves, a malformed symbol, an unknown status — is treated
 * as unset, so an old or hand-edited link still opens the list. */
export function botHistoryQuery(
  values: BotHistoryUrlValues,
  isKnownAccount: (clerkId: string) => boolean,
): BotHistoryQuery {
  const page = Number(values.page);
  const symbol = values.symbol?.trim().toUpperCase() ?? '';
  const bot = values.bot?.trim() ?? '';
  return {
    account: values.account && isKnownAccount(values.account) ? values.account : null,
    status: BOT_HISTORY_STATUSES.find((status) => status.id === values.status)?.id ?? null,
    world: BOT_HISTORY_WORLDS.find((world) => world.id === values.world)?.id ?? null,
    symbol: US_EQUITY_SYMBOL.test(symbol) ? symbol : null,
    bot: bot.length > 0 && bot.length <= MAX_BOT_ID_LENGTH ? bot : null,
    page: Number.isInteger(page) && page >= 1 ? page : 1,
  };
}

const WORLD_TONES: Readonly<Record<BotHistoryWorld, LaneModeChip['tone']>> = {
  live: 'live',
  paper: 'paper',
  shadow: 'shadow',
  dry_run: 'dry-run',
};

/** The chip a History row wears: the world that bot's run was in, worded by
 * the backend (`world_label`) — never the lane's mode today, so a red chip
 * only ever means real money (owner decision 2026-09-30, #2615). */
export function historyWorldChip(row: FleetBotHistoryRow): LaneModeChip {
  return { tone: WORLD_TONES[row.world], mode: row.world_label };
}

/** The one name History shows an account by: its lane's name, else its
 * account number — never a raw lane id. */
export function historyAccountName(
  directory: FleetDirectoryService,
  broker: string,
  clerkId: string,
  accountId: string | null,
): string {
  const name = directory.displayNameOf(broker, clerkId);
  return name !== null ? laneDisplayNameText(name) : (accountId ?? 'Unnamed account');
}

/** The all-accounts bot history, read on demand (never polled). */
@Injectable({ providedIn: 'root' })
export class BotHistoryService {
  private readonly http = inject(HttpClient);

  read(query: BotHistoryQuery): Promise<FleetBotHistoryPage> {
    let params = new HttpParams().set('page', query.page).set('page_size', BOT_HISTORY_PAGE_SIZE);
    if (query.account !== null) params = params.set('clerk_id', query.account);
    if (query.status !== null) params = params.set('status', query.status);
    if (query.world !== null) params = params.set('world', query.world);
    if (query.symbol !== null) params = params.set('symbol', query.symbol);
    if (query.bot !== null) params = params.set('strategy_instance_id', query.bot);
    return firstValueFrom(this.http.get<FleetBotHistoryPage>(BOT_HISTORY_URL, { params }));
  }
}

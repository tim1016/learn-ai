import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import type { components } from '../../api/broker.types';
import { etDayEndMs, etMidnightMs } from '../../shared/date/et-midnight';
import { parseIsoDate } from '../../shared/date/iso-date';

/** One page of the Research History: the typed index over every study's own store, merged by Python (#3519). */
export type ResearchHistoryPage = components['schemas']['ResearchHistoryPage'];
export type ResearchHistoryEntry = components['schemas']['ResearchHistoryEntry'];
export type ResearchHistorySource = components['schemas']['ResearchHistorySource'];
export type ResearchHistoryType = ResearchHistoryEntry['type'];
export type ResearchHistoryStatus = ResearchHistoryEntry['status'];

/** Entries per page. */
export const RESEARCH_HISTORY_PAGE_SIZE = 25;

export interface HistoryTypeInfo {
  /** What the kind is called in the list and in its filter. */
  readonly label: string;
  /**
   * Router commands to the store's own detail, which reads what the store holds and runs nothing; `null` for a
   * store that keeps no detail beyond the entry itself.
   */
  readonly detail: ((nativeId: string) => readonly string[]) | null;
}

/**
 * The kinds of record the History lists. Typed over every `type` the server can send, so a store added there cannot
 * ship without its name and its detail here.
 */
export const HISTORY_TYPES: Readonly<Record<ResearchHistoryType, HistoryTypeInfo>> = {
  'spec-run': { label: 'Rule sandbox run', detail: (id) => ['/research/history/spec-run', id] },
  'walk-forward': { label: 'Walk-forward', detail: (id) => ['/research/history/walk-forward', id] },
  'monte-carlo': { label: 'Monte Carlo', detail: (id) => ['/research/history/monte-carlo', id] },
  baseline: { label: 'Baseline', detail: (id) => ['/research/history/baseline', id] },
  'feature-study': { label: 'Feature study', detail: (id) => ['/research/history/feature-study', id] },
  'signal-study': { label: 'Signal study', detail: (id) => ['/research/history/signal-study', id] },
  'cross-sectional-study': {
    label: 'Cross-sectional study',
    detail: (id) => ['/research/history/cross-sectional-study', id],
  },
  'indicator-reliability': {
    label: 'Indicator reliability',
    detail: (id) => ['/research/history/indicator-reliability', id],
  },
  'realized-vs-iv': { label: 'Realized vs IV', detail: (id) => ['/research/history/realized-vs-iv', id] },
  // The rows the retired .NET backend wrote: kept as they were, apart from the runs the studies record now.
  'signal-report': { label: 'Signal study (earlier)', detail: (id) => ['/research/history/signal-report', id] },
  'feature-experiment': { label: 'Feature study (earlier)', detail: null },
};

export const HISTORY_TYPE_IDS = Object.keys(HISTORY_TYPES) as ResearchHistoryType[];

/** The status filter's choices and the words a row shows, closed over every status the server can send. */
export const HISTORY_STATUSES: Readonly<Record<ResearchHistoryStatus, string>> = {
  completed: 'Completed',
  partial: 'Partial',
  failed: 'Failed',
  cancelled: 'Cancelled',
  running: 'Running',
  unreadable: 'Unreadable',
};

export const HISTORY_STATUS_IDS = Object.keys(HISTORY_STATUSES) as ResearchHistoryStatus[];

/** The URL's raw values: whatever a link, or a hand-edited address, carries. */
export interface ResearchHistoryUrl {
  readonly type?: string;
  readonly symbol?: string;
  readonly status?: string;
  /** First day of the created-between filter, an ET calendar date `YYYY-MM-DD`. */
  readonly from?: string;
  /** Last day of it, included. */
  readonly to?: string;
  readonly page?: string;
}

/** What the list is narrowed to: each filter set, or `null` for "any". Nothing is pre-selected. */
export interface ResearchHistoryQuery {
  readonly type: ResearchHistoryType | null;
  readonly symbol: string | null;
  readonly status: ResearchHistoryStatus | null;
  readonly from: string | null;
  readonly to: string | null;
  readonly page: number;
}

const SYMBOL_MAX_LENGTH = 50;

/**
 * Read the filters out of the URL. A value the list does not know (an old kind, a hand-typed status, half a date, a
 * range that ends before it starts) is treated as unset, so an old or edited link still opens the list.
 */
export function researchHistoryQuery(values: ResearchHistoryUrl): ResearchHistoryQuery {
  const page = Number(values.page);
  const symbol = values.symbol?.trim().toUpperCase() ?? '';
  const from = parseIsoDate(values.from ?? '');
  const to = parseIsoDate(values.to ?? '');
  const inverted = from !== null && to !== null && from > to;
  return {
    type: HISTORY_TYPE_IDS.find((id) => id === values.type) ?? null,
    symbol: symbol.length > 0 && symbol.length <= SYMBOL_MAX_LENGTH ? symbol : null,
    status: HISTORY_STATUS_IDS.find((id) => id === values.status) ?? null,
    from: inverted ? null : from,
    to: inverted ? null : to,
    page: Number.isInteger(page) && page >= 1 ? page : 1,
  };
}

/** The 503 the endpoint answers when every store it asked failed, with each store's reason. */
export class ResearchHistoryUnavailableError extends Error {
  constructor(readonly sources: readonly ResearchHistorySource[]) {
    super('Every research store that was asked failed.');
  }
}

/** The Research History, read on demand (never polled, and never a run). */
@Injectable({ providedIn: 'root' })
export class ResearchHistoryService {
  private readonly http = inject(HttpClient);
  private readonly url = `${environment.pythonServiceUrl}/api/research/history`;

  async read(query: ResearchHistoryQuery): Promise<ResearchHistoryPage> {
    let params = new HttpParams().set('page', query.page).set('page_size', RESEARCH_HISTORY_PAGE_SIZE);
    if (query.type !== null) params = params.set('type', query.type);
    if (query.symbol !== null) params = params.set('symbol', query.symbol);
    if (query.status !== null) params = params.set('status', query.status);
    if (query.from !== null) params = params.set('created_from_ms', etMidnightMs(query.from));
    if (query.to !== null) params = params.set('created_before_ms', etDayEndMs(query.to));
    try {
      return await firstValueFrom(this.http.get<ResearchHistoryPage>(this.url, { params }));
    } catch (error) {
      const refusal = error instanceof HttpErrorResponse && error.status === 503 ? error.error?.detail : null;
      if (refusal?.code === 'RESEARCH_HISTORY_UNAVAILABLE') throw new ResearchHistoryUnavailableError(refusal.sources);
      throw error;
    }
  }
}

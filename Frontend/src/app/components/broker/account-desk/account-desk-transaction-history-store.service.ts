import { Injectable, computed, inject, signal } from '@angular/core';

import type {
  ClerkTransactionFilters,
  ClerkTransactionHistoryResponse,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokerService } from '../../../services/broker.service';
import { extractServerMessage } from '../operation-error';

const PAGE_SIZE = 100;
const MAX_CLIENT_ROWS = 5_000;
const MAX_CLIENT_PAGES = MAX_CLIENT_ROWS / PAGE_SIZE;

/** Loads a bounded server window completely before exposing it for client-side table filters. */
@Injectable()
export class AccountDeskTransactionHistoryStore {
  private readonly broker = inject(BrokerService);
  private readonly accountKey = signal<string | null>(null);
  private readonly rowsState = signal<readonly ClerkTransactionSummary[]>([]);
  private readonly feedState = signal<ClerkTransactionHistoryResponse | null>(null);
  private readonly loadingState = signal(false);
  private readonly loadedCountState = signal(0);
  private readonly loadedPagesState = signal(0);
  private readonly rowLimitReachedState = signal(false);
  private readonly errorState = signal<string | null>(null);
  private readonly filtersState = signal<ClerkTransactionFilters>({});
  private requestGeneration = 0;
  private refreshPending = false;

  readonly accountId = this.accountKey.asReadonly();
  readonly rows = this.rowsState.asReadonly();
  readonly feed = this.feedState.asReadonly();
  readonly loading = this.loadingState.asReadonly();
  readonly loadedCount = this.loadedCountState.asReadonly();
  readonly loadedPages = this.loadedPagesState.asReadonly();
  readonly rowLimitReached = this.rowLimitReachedState.asReadonly();
  readonly errorMessage = this.errorState.asReadonly();
  readonly filters = this.filtersState.asReadonly();
  readonly hasLastGood = computed(() => this.feedState() !== null);

  async load(
    accountId: string,
    filters?: ClerkTransactionFilters,
  ): Promise<void> {
    const normalizedFilters = filters === undefined ? this.filtersState() : normalizeFilters(filters);
    const requestChanged = this.accountKey() !== accountId
      || !sameFilters(this.filtersState(), normalizedFilters);
    if (requestChanged) {
      this.requestGeneration += 1;
      this.refreshPending = false;
      this.accountKey.set(accountId);
      this.filtersState.set(normalizedFilters);
      this.rowsState.set([]);
      this.feedState.set(null);
      this.loadingState.set(false);
      this.loadedCountState.set(0);
      this.loadedPagesState.set(0);
      this.rowLimitReachedState.set(false);
      this.errorState.set(null);
    }
    if (this.loadingState()) {
      this.refreshPending = true;
      return;
    }
    await this.fetchAllPages();
  }

  retry(): void {
    if (this.loadingState()) {
      this.refreshPending = true;
      return;
    }
    void this.fetchAllPages();
  }

  private async fetchAllPages(): Promise<void> {
    const accountId = this.accountKey();
    if (accountId === null || this.loadingState()) return;
    const requestGeneration = this.requestGeneration;
    this.loadingState.set(true);
    this.loadedCountState.set(0);
    this.loadedPagesState.set(0);
    this.rowLimitReachedState.set(false);
    this.errorState.set(null);
    try {
      let cursor: string | null = null;
      let rows: readonly ClerkTransactionSummary[] = [];
      let latestPage: ClerkTransactionHistoryResponse | null = null;
      const seenCursors = new Set<string>();

      do {
        const page = await this.broker.accountTransactions(
          accountId,
          cursor,
          PAGE_SIZE,
          this.filtersState(),
        );
        if (!this.isCurrentRequest(accountId, requestGeneration)) return;

        latestPage = page;
        rows = mergeRows(rows, page.rows).slice(0, MAX_CLIENT_ROWS);
        this.loadedCountState.set(rows.length);
        this.loadedPagesState.update((count) => count + 1);

        const nextCursor = page.next_cursor;
        if (nextCursor === null) {
          cursor = null;
          break;
        }
        if (seenCursors.has(nextCursor)) {
          throw new Error('The transaction projection returned a repeated page cursor.');
        }
        seenCursors.add(nextCursor);
        cursor = nextCursor;
      } while (rows.length < MAX_CLIENT_ROWS && this.loadedPagesState() < MAX_CLIENT_PAGES);

      if (!this.isCurrentRequest(accountId, requestGeneration) || latestPage === null) return;
      this.feedState.set(latestPage);
      this.rowsState.set(rows);
      this.rowLimitReachedState.set(cursor !== null);
    } catch (error) {
      if (this.isCurrentRequest(accountId, requestGeneration)) {
        this.errorState.set(extractServerMessage(error, 'Transaction history is unavailable. Retry to request it again.'));
      }
    } finally {
      if (this.isCurrentRequest(accountId, requestGeneration)) {
        this.loadingState.set(false);
        if (this.refreshPending) {
          this.refreshPending = false;
          void this.fetchAllPages();
        }
      }
    }
  }

  private isCurrentRequest(accountId: string, requestGeneration: number): boolean {
    return this.accountKey() === accountId && this.requestGeneration === requestGeneration;
  }
}

function normalizeFilters(filters: ClerkTransactionFilters): ClerkTransactionFilters {
  const fromMs = validTimestamp(filters.fromMs);
  const toMs = validTimestamp(filters.toMs);
  return {
    ...(filters.origin ? { origin: filters.origin } : {}),
    ...(filters.lifecycleState?.trim() ? { lifecycleState: filters.lifecycleState.trim() } : {}),
    ...(filters.strategyInstanceId?.trim()
      ? { strategyInstanceId: filters.strategyInstanceId.trim() }
      : {}),
    ...(filters.runId?.trim() ? { runId: filters.runId.trim() } : {}),
    ...(fromMs !== undefined ? { fromMs } : {}),
    ...(toMs !== undefined ? { toMs } : {}),
  };
}

function validTimestamp(value: number | null | undefined): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : undefined;
}

function sameFilters(left: ClerkTransactionFilters, right: ClerkTransactionFilters): boolean {
  return left.origin === right.origin
    && left.lifecycleState === right.lifecycleState
    && left.strategyInstanceId === right.strategyInstanceId
    && left.runId === right.runId
    && left.fromMs === right.fromMs
    && left.toMs === right.toMs;
}

function mergeRows(
  existing: readonly ClerkTransactionSummary[],
  incoming: readonly ClerkTransactionSummary[],
): readonly ClerkTransactionSummary[] {
  const rows = new Map(existing.map((row) => [row.transaction_id, row]));
  for (const row of incoming) rows.set(row.transaction_id, row);
  return [...rows.values()].sort((left, right) =>
    right.recorded_at_ms - left.recorded_at_ms || right.journal_seq - left.journal_seq,
  );
}

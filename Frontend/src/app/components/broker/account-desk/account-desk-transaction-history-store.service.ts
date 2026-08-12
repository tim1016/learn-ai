import { Injectable, computed, inject, signal } from '@angular/core';

import type {
  ClerkTransactionFilters,
  ClerkTransactionHistoryResponse,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokerService } from '../../../services/broker.service';
import { extractServerMessage } from '../operation-error';

const FIRST_PAGE_SIZE = 25;

/** Bounded Clerk projection state; it never scans or classifies history in the browser. */
@Injectable()
export class AccountDeskTransactionHistoryStore {
  private readonly broker = inject(BrokerService);
  private readonly accountKey = signal<string | null>(null);
  private readonly rowsState = signal<readonly ClerkTransactionSummary[]>([]);
  private readonly nextCursorState = signal<string | null>(null);
  private readonly feedState = signal<ClerkTransactionHistoryResponse | null>(null);
  private readonly loadingState = signal(false);
  private readonly errorState = signal<string | null>(null);
  private readonly filtersState = signal<ClerkTransactionFilters>({});
  private requestGeneration = 0;
  private refreshPending = false;

  readonly accountId = this.accountKey.asReadonly();
  readonly rows = this.rowsState.asReadonly();
  readonly nextCursor = this.nextCursorState.asReadonly();
  readonly feed = this.feedState.asReadonly();
  readonly loading = this.loadingState.asReadonly();
  readonly errorMessage = this.errorState.asReadonly();
  readonly filters = this.filtersState.asReadonly();
  readonly hasLastGood = computed(() => this.feedState() !== null);

  async load(
    accountId: string,
    filters?: ClerkTransactionFilters,
  ): Promise<void> {
    const normalizedFilters = filters === undefined ? this.filtersState() : normalizeFilters(filters);
    if (this.accountKey() !== accountId || !sameFilters(this.filtersState(), normalizedFilters)) {
      this.requestGeneration += 1;
      this.refreshPending = false;
      this.accountKey.set(accountId);
      this.filtersState.set(normalizedFilters);
      this.rowsState.set([]);
      this.nextCursorState.set(null);
      this.feedState.set(null);
      this.loadingState.set(false);
      this.errorState.set(null);
    }
    if (this.loadingState()) {
      this.refreshPending = true;
      return;
    }
    await this.fetchPage(null, true);
  }

  retry(): void {
    void this.fetchPage(null, true);
  }

  loadOlder(): void {
    const cursor = this.nextCursorState();
    if (cursor !== null) void this.fetchPage(cursor, false);
  }

  setFilters(filters: ClerkTransactionFilters): void {
    this.requestGeneration += 1;
    this.refreshPending = false;
    this.filtersState.set(normalizeFilters(filters));
    this.rowsState.set([]);
    this.nextCursorState.set(null);
    // The prior request is deliberately allowed to finish but cannot publish
    // after its generation changes. Clear the local gate so this new bounded
    // request is not stranded behind it.
    this.loadingState.set(false);
    void this.fetchPage(null, true);
  }

  private async fetchPage(cursor: string | null, replace: boolean): Promise<void> {
    const accountId = this.accountKey();
    if (accountId === null || this.loadingState()) return;
    const requestGeneration = this.requestGeneration;
    this.loadingState.set(true);
    this.errorState.set(null);
    try {
      const page = await this.broker.accountTransactions(
        accountId,
        cursor,
        FIRST_PAGE_SIZE,
        this.filtersState(),
      );
      if (!this.isCurrentRequest(accountId, requestGeneration)) return;
      this.feedState.set(page);
      this.rowsState.set(replace ? page.rows : mergeRows(this.rowsState(), page.rows));
      this.nextCursorState.set(page.next_cursor);
    } catch (error) {
      if (this.isCurrentRequest(accountId, requestGeneration)) {
        this.errorState.set(extractServerMessage(error, 'Transaction history is unavailable. Retry to request it again.'));
      }
    } finally {
      if (this.isCurrentRequest(accountId, requestGeneration)) {
        this.loadingState.set(false);
        if (this.refreshPending) {
          this.refreshPending = false;
          void this.fetchPage(null, true);
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

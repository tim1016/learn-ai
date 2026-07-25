import { Injectable, computed, inject, signal } from '@angular/core';

import type {
  ClerkTransactionDetail,
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

  readonly accountId = this.accountKey.asReadonly();
  readonly rows = this.rowsState.asReadonly();
  readonly nextCursor = this.nextCursorState.asReadonly();
  readonly feed = this.feedState.asReadonly();
  readonly loading = this.loadingState.asReadonly();
  readonly errorMessage = this.errorState.asReadonly();
  readonly hasLastGood = computed(() => this.feedState() !== null);

  async load(accountId: string): Promise<void> {
    if (this.accountKey() !== accountId) {
      this.accountKey.set(accountId);
      this.rowsState.set([]);
      this.nextCursorState.set(null);
      this.feedState.set(null);
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

  transactionDetail(transactionId: string): Promise<ClerkTransactionDetail> {
    const accountId = this.accountKey();
    if (accountId === null) return Promise.reject(new Error('No account is selected.'));
    return this.broker.accountTransaction(accountId, transactionId);
  }

  private async fetchPage(cursor: string | null, replace: boolean): Promise<void> {
    const accountId = this.accountKey();
    if (accountId === null || this.loadingState()) return;
    this.loadingState.set(true);
    this.errorState.set(null);
    try {
      const page = await this.broker.accountTransactions(accountId, cursor, FIRST_PAGE_SIZE);
      if (this.accountKey() !== accountId) return;
      this.feedState.set(page);
      this.rowsState.set(replace ? page.rows : mergeRows(this.rowsState(), page.rows));
      this.nextCursorState.set(page.next_cursor);
    } catch (error) {
      if (this.accountKey() === accountId) {
        this.errorState.set(extractServerMessage(error, 'Transaction history is unavailable. Retry to request it again.'));
      }
    } finally {
      if (this.accountKey() === accountId) this.loadingState.set(false);
    }
  }
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

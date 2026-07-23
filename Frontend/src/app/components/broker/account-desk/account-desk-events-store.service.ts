import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import type {
  AccountEventKind,
  AccountEventRow,
  AccountEventsRequest,
  AccountEventsResponse,
  AccountEventView,
} from '../../../api/account-events.types';
import { BrokerService } from '../../../services/broker.service';

const POLL_INTERVAL_MS = 15_000;

interface EventViewState {
  readonly rows: readonly AccountEventRow[];
  readonly latestSeq: number | null;
  readonly nextBeforeSeq: number | null;
  readonly loading: boolean;
  readonly errorMessage: string | null;
  readonly lastGoodAtMs: number | null;
}

const EMPTY_STATE: EventViewState = {
  rows: [],
  latestSeq: null,
  nextBeforeSeq: null,
  loading: false,
  errorMessage: null,
  lastGoodAtMs: null,
};

/**
 * Route-scoped event-journal state. The browser only merges stable backend
 * event identities; it never classifies events or creates trader copy.
 */
@Injectable()
export class AccountDeskEventsStore {
  private readonly broker = inject(BrokerService);
  private readonly destroyRef = inject(DestroyRef);
  private routeGeneration = 0;
  private operationsFilterGeneration = 0;
  private readonly accountKey = signal<string | null>(null);
  private readonly traderState = signal<EventViewState>(EMPTY_STATE);
  private readonly operationsState = signal<EventViewState>(EMPTY_STATE);
  private readonly operationKindsState = signal<readonly AccountEventKind[]>([]);
  private readonly pollId: number;

  readonly accountId = this.accountKey.asReadonly();
  readonly traderRows = computed(() => this.traderState().rows);
  readonly traderLoading = computed(() => this.traderState().loading);
  readonly traderErrorMessage = computed(() => this.traderState().errorMessage);
  readonly traderHasLastGood = computed(() => this.traderState().lastGoodAtMs !== null);
  readonly traderShowingStaleLastGood = computed(() =>
    this.traderState().lastGoodAtMs !== null && this.traderState().errorMessage !== null,
  );
  readonly traderLastGoodAtMs = computed(() => this.traderState().lastGoodAtMs);
  readonly operationRows = computed(() => this.operationsState().rows);
  readonly operationsLoading = computed(() => this.operationsState().loading);
  readonly operationsErrorMessage = computed(() => this.operationsState().errorMessage);
  readonly operationsHasLastGood = computed(() => this.operationsState().lastGoodAtMs !== null);
  readonly operationsShowingStaleLastGood = computed(() =>
    this.operationsState().lastGoodAtMs !== null && this.operationsState().errorMessage !== null,
  );
  readonly operationsLastGoodAtMs = computed(() => this.operationsState().lastGoodAtMs);
  readonly nextBeforeSeq = computed(() => this.operationsState().nextBeforeSeq);
  readonly operationKinds = this.operationKindsState.asReadonly();

  constructor() {
    this.pollId = window.setInterval(() => this.pollForNewer(), POLL_INTERVAL_MS);
    this.destroyRef.onDestroy(() => {
      this.routeGeneration += 1;
      window.clearInterval(this.pollId);
    });
  }

  async load(accountId: string): Promise<void> {
    if (this.accountKey() !== accountId) {
      this.routeGeneration += 1;
      this.accountKey.set(accountId);
      this.traderState.set(EMPTY_STATE);
      this.operationsState.set(EMPTY_STATE);
    }
    await Promise.all([this.refreshTrader(), this.refreshOperations()]);
  }

  retry(): void {
    void Promise.all([this.refreshTrader(), this.refreshOperations()]);
  }

  loadOlder(): void {
    const accountId = this.accountKey();
    const beforeSeq = this.operationsState().nextBeforeSeq;
    if (accountId === null || beforeSeq === null || this.operationsState().loading) return;
    void this.fetchOperations(accountId, { beforeSeq });
  }

  toggleOperationKind(kind: AccountEventKind): void {
    const selected = this.operationKindsState();
    const next = selected.includes(kind)
      ? selected.filter((value) => value !== kind)
      : [...selected, kind];
    this.operationKindsState.set(next);
    this.operationsFilterGeneration += 1;
    this.operationsState.set(EMPTY_STATE);
    void this.refreshOperations();
  }

  private pollForNewer(): void {
    if (this.accountKey() === null) return;
    void Promise.all([this.refreshTrader(), this.refreshOperations()]);
  }

  private async refreshTrader(): Promise<void> {
    const accountId = this.accountKey();
    if (accountId === null || this.traderState().loading) return;
    await this.fetchTrader(accountId, { afterSeq: this.traderState().latestSeq ?? undefined });
  }

  private async refreshOperations(): Promise<void> {
    const accountId = this.accountKey();
    if (accountId === null || this.operationsState().loading) return;
    await this.fetchOperations(accountId, { afterSeq: this.operationsState().latestSeq ?? undefined });
  }

  private async fetchTrader(accountId: string, cursor: Pick<AccountEventsRequest, 'afterSeq'>): Promise<void> {
    const generation = this.routeGeneration;
    this.traderState.update((state) => ({ ...state, loading: true, errorMessage: null }));
    try {
      const response = await this.broker.accountEvents(accountId, {
        view: 'trader_today',
        limit: 100,
        ...cursor,
      });
      if (!this.isCurrentRequest(accountId, generation)) return;
      const page = validatePage(response, accountId, 'trader_today');
      this.traderState.update((state) => applyPage(state, page, cursor, Date.now()));
    } catch (error) {
      if (this.isCurrentRequest(accountId, generation)) {
        this.traderState.update((state) => ({ ...state, errorMessage: serverMessage(error) }));
      }
    } finally {
      if (this.isCurrentRequest(accountId, generation)) {
        this.traderState.update((state) => ({ ...state, loading: false }));
      }
    }
  }

  private async fetchOperations(
    accountId: string,
    cursor: Pick<AccountEventsRequest, 'afterSeq' | 'beforeSeq'>,
  ): Promise<void> {
    const generation = this.routeGeneration;
    const filterGeneration = this.operationsFilterGeneration;
    const kinds = this.operationKindsState();
    this.operationsState.update((state) => ({ ...state, loading: true, errorMessage: null }));
    try {
      const response = await this.broker.accountEvents(accountId, {
        view: 'operations',
        limit: 50,
        kinds,
        ...cursor,
      });
      if (!this.isCurrentOperationsRequest(accountId, generation, filterGeneration)) return;
      const page = validatePage(response, accountId, 'operations');
      this.operationsState.update((state) => applyPage(state, page, cursor, Date.now()));
    } catch (error) {
      if (this.isCurrentOperationsRequest(accountId, generation, filterGeneration)) {
        this.operationsState.update((state) => ({ ...state, errorMessage: serverMessage(error) }));
      }
    } finally {
      if (this.isCurrentOperationsRequest(accountId, generation, filterGeneration)) {
        this.operationsState.update((state) => ({ ...state, loading: false }));
      }
    }
  }

  private isCurrentRequest(accountId: string, generation: number): boolean {
    return this.accountKey() === accountId && this.routeGeneration === generation;
  }

  private isCurrentOperationsRequest(
    accountId: string,
    routeGeneration: number,
    filterGeneration: number,
  ): boolean {
    return this.isCurrentRequest(accountId, routeGeneration) &&
      this.operationsFilterGeneration === filterGeneration;
  }
}

function applyPage(
  state: EventViewState,
  page: AccountEventsResponse,
  cursor: Pick<AccountEventsRequest, 'afterSeq' | 'beforeSeq'>,
  lastGoodAtMs: number,
): EventViewState {
  return {
    rows: mergeRows(state.rows, page.rows),
    latestSeq: Math.max(state.latestSeq ?? 0, page.latest_seq ?? 0) || null,
    nextBeforeSeq: cursor.beforeSeq === undefined ? state.nextBeforeSeq ?? page.next_before_seq : page.next_before_seq,
    loading: state.loading,
    errorMessage: null,
    lastGoodAtMs,
  };
}

function mergeRows(
  existing: readonly AccountEventRow[],
  incoming: readonly AccountEventRow[],
): readonly AccountEventRow[] {
  const rowsById = new Map(existing.map((row) => [row.event_id, row]));
  for (const row of incoming) rowsById.set(row.event_id, row);
  return [...rowsById.values()].sort((left, right) => right.seq - left.seq);
}

function validatePage(
  value: unknown,
  accountId: string,
  expectedView: AccountEventView,
): AccountEventsResponse {
  if (!isRecord(value) || value['schema_version'] !== 1 || value['account_id'] !== accountId || value['view'] !== expectedView) {
    throw new Error('Account event response did not attest this route.');
  }
  const rows = value['rows'];
  const latestSeq = value['latest_seq'];
  const nextBeforeSeq = value['next_before_seq'];
  if (!Array.isArray(rows) || !isNullableSequence(latestSeq) || !isNullableSequence(nextBeforeSeq)) {
    throw new Error('Account event response was malformed.');
  }
  const typedRows: AccountEventRow[] = [];
  for (const row of rows) {
    if (!isAccountEventRow(row)) throw new Error('Account event response contained malformed rows.');
    typedRows.push(row);
  }
  return {
    schema_version: 1,
    account_id: accountId,
    view: expectedView,
    rows: typedRows,
    latest_seq: latestSeq,
    next_before_seq: nextBeforeSeq,
  };
}

function isAccountEventRow(value: unknown): value is AccountEventRow {
  if (!isRecord(value) || value['schema_version'] !== 1 || !isSequence(value['seq']) || !isInt64Ms(value['occurred_at_ms'])) {
    return false;
  }
  if (typeof value['event_id'] !== 'string' || typeof value['operator_detail'] !== 'string') return false;
  if (value['trader_narration'] !== null && typeof value['trader_narration'] !== 'string') return false;
  if (!isAccountEventKind(value['kind']) || !Array.isArray(value['evidence_refs'])) return false;
  return value['evidence_refs'].every(isEvidenceRef);
}

function isEvidenceRef(value: unknown): boolean {
  return isRecord(value) && typeof value['source'] === 'string' && typeof value['ref'] === 'string' &&
    (value['detail'] === null || typeof value['detail'] === 'string');
}

function isAccountEventKind(value: unknown): value is AccountEventKind {
  return value === 'activity' || value === 'safety' || value === 'reconciliation' || value === 'clerk' ||
    value === 'configuration' || value === 'other';
}

function isNullableSequence(value: unknown): value is number | null {
  return value === null || isSequence(value);
}

function isSequence(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1;
}

function isInt64Ms(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function serverMessage(error: unknown): string {
  if (!isRecord(error)) return 'Account event history is unavailable. Retry to request it again.';
  const body = error['error'];
  if (isRecord(body)) {
    const detail = body['detail'];
    if (isRecord(detail) && typeof detail['message'] === 'string') return detail['message'];
  }
  return 'Account event history is unavailable. Retry to request it again.';
}

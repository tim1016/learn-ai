import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';
import { PanelModule } from 'primeng/panel';

import type {
  ClerkTransactionFilters,
  ClerkTransactionOrigin,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokerService } from '../../../services/broker.service';
import { formatReceiptLabel, ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ClerkTransactionEvidenceDrawerComponent } from '../clerk-transaction-evidence-drawer/clerk-transaction-evidence-drawer.component';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

/** Operator-only Clerk transaction grid with receipt detail fetched on selection. */
@Component({
  selector: 'app-account-desk-transaction-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DecimalPipe,
    ButtonModule,
    PanelModule,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    ClerkTransactionEvidenceDrawerComponent,
  ],
  templateUrl: './account-desk-transaction-history.component.html',
  styleUrl: './account-desk-transaction-history.component.scss',
})
export class AccountDeskTransactionHistoryComponent {
  readonly store = inject(AccountDeskTransactionHistoryStore);
  private readonly broker = inject(BrokerService);
  readonly accountId = input<string | null>(null);
  readonly refreshVersion = input(0);
  private activeAccountId = this.store.accountId();
  readonly selectedTransaction = signal<ClerkTransactionSummary | null>(null);
  readonly receiptOpener = signal<HTMLElement | null>(null);
  readonly filterOrigin = signal<ClerkTransactionOrigin | ''>('');
  readonly filterLifecycle = signal('');
  readonly filterStrategy = signal('');
  readonly filterRun = signal('');
  readonly acknowledgementOperator = signal('');
  readonly acknowledgingExternalOrderId = signal<string | null>(null);
  readonly acknowledgementError = signal<string | null>(null);

  constructor() {
    effect(() => {
      const accountId = this.accountId();
      this.refreshVersion();
      // `store.load` reads/writes its own signals (loadingState, accountKey)
      // in its synchronous prefix before its first await. Without untracked,
      // those reads are attributed to this effect, and the effect re-fires
      // every time load's own writes settle them — an unbounded reload loop.
      if (accountId !== null) untracked(() => void this.store.load(accountId));
    });
    effect(() => {
      const accountId = this.store.accountId();
      if (accountId === this.activeAccountId) return;
      this.activeAccountId = accountId;
      this.selectedTransaction.set(null);
      this.receiptOpener.set(null);
    });
  }
  trackRow = (_: number, row: ClerkTransactionSummary): string => row.transaction_id;

  inputValue(event: Event): string {
    return event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement
      ? event.target.value
      : '';
  }

  private static readonly ORIGIN_VALUES: readonly ClerkTransactionOrigin[] = [
    'manual',
    'strategy',
    'external',
    'unknown',
    'recovery',
    'emergency',
    'shutdown',
    'force_flat',
    'other',
  ];

  setOriginFilter(event: Event): void {
    const value = this.inputValue(event);
    const match = AccountDeskTransactionHistoryComponent.ORIGIN_VALUES.find(
      (origin) => origin === value,
    );
    this.filterOrigin.set(match ?? '');
  }

  applyFilters(): void {
    const filters: ClerkTransactionFilters = {
      origin: this.filterOrigin() || null,
      lifecycleState: this.filterLifecycle(),
      strategyInstanceId: this.filterStrategy(),
      runId: this.filterRun(),
    };
    this.store.setFilters(filters);
  }

  clearFilters(): void {
    this.filterOrigin.set('');
    this.filterLifecycle.set('');
    this.filterStrategy.set('');
    this.filterRun.set('');
    this.store.setFilters({});
  }

  originLabel(row: ClerkTransactionSummary): string {
    if (row.transaction_origin === 'strategy') {
      const strategyInstanceId = row.strategy_instance_id;
      return strategyInstanceId
        ? `Placed by ${strategyInstanceId}`
        : 'Unknown — review required';
    }
    if (row.transaction_origin === 'unknown') return 'Unknown — review required';
    if (row.transaction_origin === 'external' || row.transaction_origin === 'manual') {
      return 'External / manual';
    }
    // System-initiated safety-flatten origins (recovery/emergency/shutdown/
    // force_flat) and any future code fall back to the humanized raw code —
    // never silently relabeled as "External / manual".
    return formatReceiptLabel(row.transaction_origin ?? 'manual');
  }

  /** Suppress the raw-code hint where it would exactly duplicate the headline label above it. */
  originCodeDistinctFromLabel(origin: ClerkTransactionOrigin): boolean {
    return origin === 'strategy' || origin === 'unknown' || origin === 'external' || origin === 'manual';
  }

  instruction(row: ClerkTransactionSummary): string {
    const instruction = row.order_instruction;
    return [instruction?.symbol, instruction?.quantity]
      .filter((value) => value !== null && value !== undefined)
      .join(' ');
  }

  openReceipt(row: ClerkTransactionSummary, event: MouseEvent): void {
    const opener = event.currentTarget;
    this.receiptOpener.set(opener instanceof HTMLElement ? opener : null);
    this.selectedTransaction.set(row);
  }

  onReceiptClosed(): void {
    this.selectedTransaction.set(null);
    this.receiptOpener.set(null);
  }

  canAcknowledgeSelectedExternalOrder(): boolean {
    const transaction = this.selectedTransaction();
    return transaction?.transaction_origin === 'external'
      && transaction.lifecycle_state === 'review_required'
      && transaction.external_order_id !== null
      && transaction.external_order_id !== undefined;
  }

  async acknowledgeSelectedExternalOrder(): Promise<void> {
    const transaction = this.selectedTransaction();
    const accountId = this.store.accountId();
    const externalOrderId = transaction?.external_order_id;
    const operator = this.acknowledgementOperator().trim();
    if (!accountId || !externalOrderId || !operator || this.acknowledgingExternalOrderId() !== null) return;

    this.acknowledgingExternalOrderId.set(externalOrderId);
    this.acknowledgementError.set(null);
    try {
      await this.broker.acknowledgeExternalOrder(accountId, externalOrderId, operator);
      await this.store.load(accountId);
      this.acknowledgementOperator.set('');
      this.onReceiptClosed();
    } catch {
      this.acknowledgementError.set('Could not acknowledge this external order. Retry after reviewing the evidence.');
    } finally {
      this.acknowledgingExternalOrderId.set(null);
    }
  }
}

import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  signal,
} from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { PanelModule } from 'primeng/panel';

import type {
  ClerkTransactionFilters,
  ClerkTransactionOrigin,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ClerkTransactionEvidenceDrawerComponent } from '../clerk-transaction-evidence-drawer/clerk-transaction-evidence-drawer.component';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

/** Operator-only Clerk transaction grid with receipt detail fetched on selection. */
@Component({
  selector: 'app-account-desk-transaction-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
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
  private activeAccountId = this.store.accountId();
  readonly selectedTransaction = signal<ClerkTransactionSummary | null>(null);
  readonly receiptOpener = signal<HTMLElement | null>(null);
  readonly filterOrigin = signal<ClerkTransactionOrigin | ''>('');
  readonly filterLifecycle = signal('');
  readonly filterStrategy = signal('');
  readonly filterRun = signal('');

  constructor() {
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

  setOriginFilter(event: Event): void {
    const value = this.inputValue(event);
    this.filterOrigin.set(
      value === 'manual' || value === 'strategy' || value === 'recovery' || value === 'emergency'
        || value === 'shutdown' || value === 'force_flat' || value === 'other'
        ? value
        : '',
    );
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

  origin(row: ClerkTransactionSummary): ClerkTransactionOrigin {
    return row.transaction_origin ?? 'manual';
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
}

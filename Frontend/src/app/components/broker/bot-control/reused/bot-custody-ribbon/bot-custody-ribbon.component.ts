import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';

import type {
  ClerkTransactionHistoryResponse,
  ClerkTransactionSummary,
} from '../../../../../api/clerk-transaction-history.types';
import { BrokerService } from '../../../../../services/broker.service';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../../shared/timestamp';
import type { AccountSafetySnapshotState } from '../../../account-safety/account-safety-snapshot.store';
import { ClerkTransactionEvidenceDrawerComponent } from '../../../clerk-transaction-evidence-drawer/clerk-transaction-evidence-drawer.component';

/**
 * Narrow Bot Control reader for server-folded, bounded Clerk evidence.
 *
 * It has no timer, broker connection, or command authority. A manual refresh
 * and a namespace change are the only reasons it reads the projection.
 */
@Component({
  selector: 'app-bot-custody-ribbon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    ClerkTransactionEvidenceDrawerComponent,
  ],
  templateUrl: './bot-custody-ribbon.component.html',
  styleUrl: './bot-custody-ribbon.component.scss',
})
export class BotCustodyRibbonComponent {
  private readonly broker = inject(BrokerService);
  private activeKey: string | null = null;
  private requestGeneration = 0;

  readonly accountId = input<string | null>(null);
  readonly strategyInstanceId = input<string | null>(null);
  readonly accountSafetyState = input<AccountSafetySnapshotState | null>(null);
  readonly page = signal<ClerkTransactionHistoryResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly selectedTransaction = signal<ClerkTransactionSummary | null>(null);
  readonly receiptOpener = signal<HTMLElement | null>(null);
  readonly rows = computed(() => this.page()?.rows ?? []);
  readonly accountReconciliationPending = computed(
    () => this.accountSafetyState()?.snapshot?.verdict === 'RECONCILING',
  );

  constructor() {
    effect(() => {
      const key = this.namespaceKey();
      if (key === this.activeKey) return;
      this.activeKey = key;
      untracked(() => this.refresh(true));
    });
  }

  trackRow = (_: number, row: ClerkTransactionSummary): string => row.transaction_id;

  refresh(clearPriorNamespace = false): void {
    const accountId = this.accountId();
    const strategyInstanceId = this.strategyInstanceId();
    const expectedKey = namespaceKey(accountId, strategyInstanceId);
    const requestGeneration = ++this.requestGeneration;
    this.selectedTransaction.set(null);
    if (clearPriorNamespace) this.page.set(null);
    this.error.set(null);
    if (accountId === null || strategyInstanceId === null) {
      this.page.set(null);
      this.loading.set(false);
      return;
    }

    this.loading.set(true);
    void this.broker.accountTransactions(accountId, null, 50, { strategyInstanceId })
      .then((page) => {
        if (!this.isCurrentRequest(expectedKey, requestGeneration)) return;
        this.page.set(page);
      })
      .catch(() => {
        if (!this.isCurrentRequest(expectedKey, requestGeneration)) return;
        this.error.set('Namespace custody evidence is unavailable. Refresh to request the saved projection again.');
      })
      .finally(() => {
        if (this.isCurrentRequest(expectedKey, requestGeneration)) this.loading.set(false);
      });
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

  private namespaceKey(): string | null {
    return namespaceKey(this.accountId(), this.strategyInstanceId());
  }

  private isCurrentRequest(expectedKey: string | null, requestGeneration: number): boolean {
    return this.namespaceKey() === expectedKey && this.requestGeneration === requestGeneration;
  }
}

function namespaceKey(accountId: string | null, strategyInstanceId: string | null): string | null {
  const account = accountId?.trim() ?? '';
  const instance = strategyInstanceId?.trim() ?? '';
  return account !== '' && instance !== '' ? `${account}:${instance}` : null;
}

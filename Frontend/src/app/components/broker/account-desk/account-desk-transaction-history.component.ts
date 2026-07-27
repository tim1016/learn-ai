import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { PanelModule } from 'primeng/panel';

import type {
  ClerkTransactionDetail,
  ClerkTransactionFilters,
  ClerkTransactionOrigin,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { formatReceiptValue, ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

/** Operator-only Clerk transaction grid with receipt detail fetched on selection. */
@Component({
  selector: 'app-account-desk-transaction-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, PanelModule, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-desk-transaction-history.component.html',
  styleUrl: './account-desk-transaction-history.component.scss',
})
export class AccountDeskTransactionHistoryComponent {
  readonly store = inject(AccountDeskTransactionHistoryStore);
  private activeAccountId = this.store.accountId();
  private readonly drawer = viewChild<ElementRef<HTMLDialogElement>>('receiptDrawer');
  private readonly opener = signal<HTMLElement | null>(null);
  private detailRequestGeneration = 0;
  readonly selected = signal<ClerkTransactionDetail | null>(null);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly drawerOpen = signal(false);
  readonly filterOrigin = signal<ClerkTransactionOrigin | ''>('');
  readonly filterLifecycle = signal('');
  readonly filterStrategy = signal('');
  readonly filterRun = signal('');
  readonly receiptEntries = computed(() => receiptEntries(this.selected()?.receipt ?? {}));

  constructor() {
    effect(() => {
      const accountId = this.store.accountId();
      if (accountId === this.activeAccountId) return;
      this.activeAccountId = accountId;
      this.detailRequestGeneration += 1;
      this.selected.set(null);
      this.detailError.set(null);
      this.detailLoading.set(false);
      if (this.drawerOpen()) this.closeReceipt();
    });
  }

  trackRow = (_: number, row: ClerkTransactionSummary): string => row.transaction_id;
  trackEvent = (_: number, event: ClerkTransactionDetail['events'][number]): string => event.event_id;
  trackReceipt = (_: number, entry: ReceiptEntry): string => entry.key;

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

  instruction(row: ClerkTransactionSummary | ClerkTransactionDetail): string {
    const instruction = row.order_instruction;
    return [instruction?.symbol, instruction?.quantity]
      .filter((value) => value !== null && value !== undefined)
      .join(' ');
  }

  eventReceiptJson(event: ClerkTransactionDetail['events'][number]): string {
    return JSON.stringify(event.receipt, null, 2);
  }

  async openReceipt(row: ClerkTransactionSummary, event: MouseEvent): Promise<void> {
    const requestGeneration = ++this.detailRequestGeneration;
    const opener = event.currentTarget;
    this.opener.set(opener instanceof HTMLElement ? opener : null);
    this.selected.set(null);
    this.detailError.set(null);
    this.detailLoading.set(true);
    this.drawerOpen.set(true);
    this.openNativeDrawer();
    try {
      const detail = await this.store.transactionDetail(row.transaction_id);
      if (requestGeneration === this.detailRequestGeneration && this.drawerOpen()) {
        this.selected.set(detail);
      }
    } catch {
      if (requestGeneration === this.detailRequestGeneration && this.drawerOpen()) {
        this.detailError.set('The selected receipt is unavailable. Close the drawer and try again.');
      }
    } finally {
      if (requestGeneration === this.detailRequestGeneration) this.detailLoading.set(false);
    }
  }

  closeReceipt(): void {
    this.detailRequestGeneration += 1;
    this.drawerOpen.set(false);
    const dialog = this.drawer()?.nativeElement;
    if (dialog?.open && typeof dialog.close === 'function') dialog.close();
  }

  onDrawerClosed(): void {
    this.drawerOpen.set(false);
    const opener = this.opener();
    this.opener.set(null);
    opener?.focus();
  }

  private openNativeDrawer(): void {
    queueMicrotask(() => {
      const dialog = this.drawer()?.nativeElement;
      if (this.drawerOpen() && dialog !== undefined && !dialog.open && typeof dialog.showModal === 'function') {
        dialog.showModal();
      }
    });
  }
}

interface ReceiptEntry {
  readonly key: string;
  readonly label: string;
  readonly value: string;
}

function receiptEntries(receipt: Record<string, unknown>): readonly ReceiptEntry[] {
  return flattenReceiptEntries(receipt, []);
}

function flattenReceiptEntries(value: unknown, path: readonly string[]): readonly ReceiptEntry[] {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => flattenReceiptEntries(item, [...path, String(index)]));
  }
  if (isReceiptRecord(value)) {
    return Object.entries(value).flatMap(([key, item]) =>
      flattenReceiptEntries(item, [...path, key]),
    );
  }

  const label = path.join('.');
  return [{
    key: label,
    label,
    value: typeof value === 'string' ? formatReceiptValue(label, value) : JSON.stringify(value) ?? '',
  }];
}

function isReceiptRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

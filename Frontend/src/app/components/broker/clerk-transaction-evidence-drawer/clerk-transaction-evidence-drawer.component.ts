import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
  viewChild,
} from '@angular/core';

import type {
  ClerkCustodyTimeline,
  ClerkTransactionDetail,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokerService } from '../../../services/broker.service';
import { formatReceiptValue, ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

interface ReceiptEntry {
  readonly key: string;
  readonly label: string;
  readonly value: string;
}

interface TimelineClock {
  readonly label: string;
  readonly value: number | null;
}

/** Shared, on-demand reader for one immutable projected Clerk receipt. */
@Component({
  selector: 'app-clerk-transaction-evidence-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './clerk-transaction-evidence-drawer.component.html',
  styleUrl: './clerk-transaction-evidence-drawer.component.scss',
})
export class ClerkTransactionEvidenceDrawerComponent {
  private readonly broker = inject(BrokerService);
  private readonly drawer = viewChild<ElementRef<HTMLDialogElement>>('drawer');
  private readonly requestGeneration = signal(0);
  private activeKey: string | null = null;
  private restoreTarget: HTMLElement | null = null;

  readonly accountId = input<string | null>(null);
  readonly transaction = input<ClerkTransactionSummary | null>(null);
  /** Caller-owned launch target so native-dialog close reliably restores focus. */
  readonly openerElement = input<HTMLElement | null>(null);
  readonly closed = output();
  readonly detail = signal<ClerkTransactionDetail | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly receiptEntries = computed(() => receiptEntries(this.detail()?.receipt ?? {}));
  readonly custodyClocks = computed(() => custodyClocks(this.detail()?.custody_timeline ?? null));
  readonly epochProvenance = computed(() => epochProvenance(this.detail()));

  constructor() {
    effect(() => {
      const accountId = this.accountId();
      const transaction = this.transaction();
      const nextKey = accountId !== null && transaction !== null
        ? `${accountId}:${transaction.transaction_id}`
        : null;
      if (nextKey === this.activeKey) return;
      this.activeKey = nextKey;
      untracked(() => void this.load(accountId, transaction));
    });
  }

  trackEvent = (_: number, event: ClerkTransactionDetail['events'][number]): string => event.event_id;
  trackReceipt = (_: number, entry: ReceiptEntry): string => entry.key;
  trackClock = (_: number, clock: TimelineClock): string => clock.label;

  instruction(receipt: ClerkTransactionSummary | ClerkTransactionDetail): string {
    const instruction = receipt.order_instruction;
    return [instruction?.symbol, instruction?.quantity]
      .filter((value) => value !== null && value !== undefined)
      .join(' ');
  }

  eventReceiptJson(event: ClerkTransactionDetail['events'][number]): string {
    return JSON.stringify(event.receipt, null, 2);
  }

  close(): void {
    this.requestGeneration.update((generation) => generation + 1);
    this.closeNativeDrawer();
  }

  onNativeClosed(): void {
    const opener = this.restoreTarget;
    this.restoreTarget = null;
    this.closed.emit();
    opener?.focus();
  }

  private async load(
    accountId: string | null,
    transaction: ClerkTransactionSummary | null,
  ): Promise<void> {
    const generation = this.requestGeneration() + 1;
    this.requestGeneration.set(generation);
    this.detail.set(null);
    this.error.set(null);
    if (accountId === null || transaction === null) {
      this.loading.set(false);
      this.closeNativeDrawer();
      return;
    }
    this.restoreTarget = this.openerElement() ?? (
      typeof document !== 'undefined' && document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    );
    this.loading.set(true);
    this.openNativeDrawer();
    try {
      const detail = await this.broker.accountTransaction(accountId, transaction.transaction_id);
      if (this.requestGeneration() === generation && this.transaction()?.transaction_id === transaction.transaction_id) {
        this.detail.set(detail);
      }
    } catch {
      if (this.requestGeneration() === generation && this.transaction()?.transaction_id === transaction.transaction_id) {
        this.error.set('The selected receipt is unavailable. Close the drawer and try again.');
      }
    } finally {
      if (this.requestGeneration() === generation) this.loading.set(false);
    }
  }

  private openNativeDrawer(): void {
    queueMicrotask(() => {
      const dialog = this.drawer()?.nativeElement;
      if (this.transaction() !== null && dialog !== undefined && !dialog.open && typeof dialog.showModal === 'function') {
        dialog.showModal();
      }
    });
  }

  private closeNativeDrawer(): void {
    const dialog = this.drawer()?.nativeElement;
    if (dialog?.open && typeof dialog.close === 'function') dialog.close();
  }
}

function custodyClocks(timeline: ClerkCustodyTimeline | null): readonly TimelineClock[] {
  return [
    { label: 'Intent created (originator clock)', value: timeline?.intent_created_at_ms ?? null },
    { label: 'Clerk request received', value: timeline?.clerk_request_received_at_ms ?? null },
    { label: 'Clerk intake admitted', value: timeline?.clerk_intake_admitted_at_ms ?? null },
    { label: 'Inbox fsynced', value: timeline?.inbox_fsynced_at_ms ?? null },
    { label: 'A0 custody accepted', value: timeline?.a0_custody_accepted_at_ms ?? null },
    { label: 'A1 broker write started', value: timeline?.broker_write_started_at_ms ?? null },
    { label: 'Broker call returned', value: timeline?.broker_call_returned_at_ms ?? null },
    { label: 'A2 broker acknowledgement recorded', value: timeline?.broker_ack_recorded_at_ms ?? null },
    { label: 'Earliest broker event time', value: timeline?.earliest_broker_source_at_ms ?? null },
    { label: 'First callback arrival', value: timeline?.first_callback_arrived_at_ms ?? null },
    { label: 'First callback durable record', value: timeline?.first_callback_recorded_at_ms ?? null },
    { label: 'A3 economic terminal record', value: timeline?.economic_terminal_recorded_at_ms ?? null },
  ];
}

interface EpochProvenance {
  readonly origin: string;
  readonly observed: string;
}

/** Formats only explicit receipt fields; never derives an epoch from sequence or time. */
function epochProvenance(detail: ClerkTransactionDetail | null): EpochProvenance {
  const receipts = detail === null ? [] : [detail.receipt, ...detail.events.map((event) => event.receipt)];
  return {
    origin: firstEpoch(receipts, 'origin_epoch'),
    observed: firstEpoch(receipts, 'observed_epoch'),
  };
}

function firstEpoch(receipts: readonly Record<string, unknown>[], key: string): string {
  for (const receipt of receipts) {
    const value = receipt[key];
    if (!isReceiptRecord(value)) continue;
    const bootId = value['clerk_boot_id'];
    const epochSeq = value['epoch_seq'];
    if (typeof bootId === 'string' && typeof epochSeq === 'number') {
      return `${bootId} / ${epochSeq}`;
    }
  }
  return 'Unknown (not recorded)';
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

import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from 'primeng/accordion';

import type { ClerkTransactionDetail } from '../../../api/clerk-transaction-history.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { formatReceiptLabel, formatReceiptValue, ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

interface ReceiptEntry {
  readonly key: string;
  readonly label: string;
  readonly value: string;
}

/** The three intentionally collapsed evidence disclosures for a selected Clerk receipt. */
@Component({
  selector: 'app-clerk-receipt-evidence-accordions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Accordion,
    AccordionContent,
    AccordionHeader,
    AccordionPanel,
    AssetIdentityComponent,
    CurrencyPipe,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './clerk-receipt-evidence-accordions.component.html',
  styleUrl: './clerk-receipt-evidence-accordions.component.scss',
})
export class ClerkReceiptEvidenceAccordionsComponent {
  readonly detail = input.required<ClerkTransactionDetail>();
  /** The instruction's side and order type, each only when it was recorded. */
  protected readonly sideAndType = computed(() => {
    const instruction = this.detail().order_instruction;
    const parts = [instruction?.action, instruction?.order_type]
      .filter((part): part is string => typeof part === 'string' && part !== '')
      .map((part) => formatReceiptLabel(part));
    return parts.length === 0 ? 'Not recorded' : parts.join(' · ');
  });
  /** The instrument an execution event recorded, when exactly one did: the instruction's symbol is no observed one. */
  protected readonly events = computed(() => this.detail().events ?? []);
  protected readonly executionSymbol = computed(() => {
    const symbols = new Set(
      this.events()
        .filter((event) => event.event_kind.startsWith('execution_'))
        .map((event) => {
          const receipt: Record<string, unknown> = event.receipt ?? {};
          return receipt['symbol'];
        })
        .filter((symbol): symbol is string => typeof symbol === 'string' && symbol !== ''),
    );
    return symbols.size === 1 ? [...symbols][0] : null;
  });
  protected readonly receiptEntries = computed(() => receiptEntries(this.detail().receipt ?? {}));
  protected readonly trackEvent = (_: number, event: NonNullable<ClerkTransactionDetail['events']>[number]): string => event.event_id;
  protected readonly trackReceipt = (_: number, entry: ReceiptEntry): string => entry.key;
  protected readonly eventReceiptEntries = (event: NonNullable<ClerkTransactionDetail['events']>[number]): readonly ReceiptEntry[] => receiptEntries(event.receipt ?? {});
}

function receiptEntries(receipt: Record<string, unknown>): readonly ReceiptEntry[] {
  return flattenReceiptEntries(receipt, []);
}

function flattenReceiptEntries(value: unknown, path: readonly string[]): readonly ReceiptEntry[] {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => flattenReceiptEntries(item, [...path, String(index)]));
  }
  if (isReceiptRecord(value)) {
    return Object.entries(value).flatMap(([key, item]) => flattenReceiptEntries(item, [...path, key]));
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

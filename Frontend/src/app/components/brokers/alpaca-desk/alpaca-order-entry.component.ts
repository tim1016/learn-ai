import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { InputNumberModule } from 'primeng/inputnumber';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { SelectButtonModule } from 'primeng/selectbutton';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type {
  BrokerOrderLeg,
  OrderLegResult,
  OrderSide,
} from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokersService } from '../../../services/brokers.service';

/** A draft equity leg the operator is assembling (pre-submit). */
interface DraftLeg {
  readonly id: number;
  symbol: string;
  side: OrderSide;
  quantity: number | null;
}

interface SideOption {
  readonly label: string;
  readonly value: OrderSide;
}

const SIDE_OPTIONS: SideOption[] = [
  { label: 'Buy', value: 'buy' },
  { label: 'Sell', value: 'sell' },
];

/**
 * Alpaca order-entry panel (phase-2 S1). Leg-based paradigm: the operator adds
 * equity legs, previews, confirms, and submits. Option legs are present but
 * disabled ("coming in 2b"). Per-leg results render after submit — acked or a
 * typed failure. Market-only in S1; limit/TIF land in S2.
 */
@Component({
  selector: 'app-alpaca-order-entry',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    ButtonModule,
    DialogModule,
    InputNumberModule,
    InputTextModule,
    MessageModule,
    SelectButtonModule,
    TableModule,
    TagModule,
    ReceiptLabelPipe,
  ],
  templateUrl: './alpaca-order-entry.component.html',
  host: { class: 'block' },
})
export class AlpacaOrderEntryComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly sideOptions: SideOption[] = SIDE_OPTIONS;
  // S1 has no operator-identity plumbing yet; the manual namespace uses a fixed
  // desk operator. Later slices thread the signed-in operator through here.
  private readonly operator = 'desk';

  protected readonly legs = signal<DraftLeg[]>([]);
  protected readonly previewOpen = signal(false);
  protected readonly submitting = signal(false);
  protected readonly results = signal<OrderLegResult[] | null>(null);
  protected readonly submitError = signal<string | null>(null);

  private nextId = 0;

  protected readonly canSubmit = computed(
    () => this.legs().length > 0 && this.legs().every((leg) => this.legValid(leg)),
  );

  protected legValid(leg: DraftLeg): boolean {
    return leg.symbol.trim().length > 0 && leg.quantity != null && leg.quantity > 0;
  }

  protected addEquityLeg(): void {
    this.legs.update((legs) => [
      ...legs,
      { id: this.nextId++, symbol: '', side: 'buy', quantity: null },
    ]);
    // A new draft invalidates the last submit's results view.
    this.results.set(null);
    this.submitError.set(null);
  }

  protected removeLeg(id: number): void {
    this.legs.update((legs) => legs.filter((leg) => leg.id !== id));
    // Editing the draft (removing a leg) invalidates the last submit's results
    // view, so a stale results table isn't left rendered against an empty draft.
    this.results.set(null);
    this.submitError.set(null);
  }

  protected updateSymbol(id: number, symbol: string): void {
    this.legs.update((legs) =>
      legs.map((leg) => (leg.id === id ? { ...leg, symbol: symbol.toUpperCase() } : leg)),
    );
  }

  protected updateSide(id: number, side: OrderSide): void {
    this.legs.update((legs) => legs.map((leg) => (leg.id === id ? { ...leg, side } : leg)));
  }

  protected updateQuantity(id: number, quantity: number | null): void {
    this.legs.update((legs) =>
      legs.map((leg) => (leg.id === id ? { ...leg, quantity } : leg)),
    );
  }

  protected openPreview(): void {
    if (!this.canSubmit()) return;
    this.previewOpen.set(true);
  }

  protected closePreview(): void {
    this.previewOpen.set(false);
  }

  protected async confirmSubmit(): Promise<void> {
    if (!this.canSubmit() || this.submitting()) return;
    this.submitting.set(true);
    this.submitError.set(null);
    const request = {
      operator: this.operator,
      legs: this.legs().map(
        (leg): BrokerOrderLeg => ({
          symbol: leg.symbol.trim(),
          side: leg.side,
          quantity: leg.quantity as number,
          order_type: 'market',
        }),
      ),
    };
    try {
      const result = await this.brokers.submitOrder('alpaca', request);
      this.results.set(result.results);
      this.previewOpen.set(false);
      this.legs.set([]);
    } catch {
      this.submitError.set('Could not reach Alpaca to submit the order. Nothing was sent.');
    } finally {
      this.submitting.set(false);
    }
  }

  protected trackLeg = (_: number, leg: DraftLeg): number => leg.id;
  protected trackResult = (_: number, result: OrderLegResult): string => result.order_ref;
}

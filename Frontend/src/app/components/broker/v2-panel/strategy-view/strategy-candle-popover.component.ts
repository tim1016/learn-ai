import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterRenderEffect,
  computed,
  input,
  output,
  viewChild,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { StrategyViewCandle, StrategyViewGateView } from '../lib/broker-v2-panel.types';
import { closePopover, openPopoverAt, type PopoverAnchor } from './popover-placement';
import { StrategyChecksComponent } from './strategy-checks.component';
import { gateResult, gateResultText } from './strategy-view-model';

let nextCandlePopoverId = 0;

/**
 * One clicked candle's story (#2639): its bar, what it was (before start or
 * the decision's outcome), its prices, the checks the bot applied and what
 * the active gate made of it. Opens over the page beside the click; the
 * browser closes it on Escape or a click elsewhere.
 */
@Component({
  selector: 'app-strategy-candle-popover',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, StrategyChecksComponent, TimestampDisplayComponent],
  templateUrl: './strategy-candle-popover.component.html',
  styleUrl: './strategy-candle-popover.component.scss',
})
export class StrategyCandlePopoverComponent {
  readonly candle = input.required<StrategyViewCandle>();
  readonly gate = input<StrategyViewGateView | null>(null);
  readonly anchor = input.required<PopoverAnchor>();

  readonly closed = output();

  protected readonly titleId = `strategy-candle-popover-${nextCandlePopoverId++}`;
  protected readonly gateResult = computed(() => {
    const gate = this.gate();
    return gate === null ? null : gateResult(this.candle(), gate.gate_id);
  });
  protected readonly gateText = computed(() => gateResultText(this.gateResult()));

  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  constructor() {
    afterRenderEffect(() => {
      this.candle();
      openPopoverAt(this.panel().nativeElement, this.anchor());
    });
  }

  protected close(): void {
    closePopover(this.panel().nativeElement);
    this.closed.emit();
  }

  protected onToggle(event: Event): void {
    if ('newState' in event && event.newState === 'closed') this.closed.emit();
  }
}

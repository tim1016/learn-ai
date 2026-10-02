import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  viewChild,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import type { StrategyViewGateView } from '../lib/broker-v2-panel.types';
import { anchorBelow, placePopover } from './popover-placement';
import { gatesStrategyFirst } from './strategy-view-model';

let nextGatePickerId = 0;

/**
 * The Dark Bright Gate chip and its picker (#2639): which gate shades the
 * strategy candles, one at a time, the strategy's own rule first. Picking
 * one only changes the shading — gates never trade.
 *
 * The host owns the choice; this emits the picked gate id. Saving a new
 * gate of the viewer's own arrives with the custom-gate editor.
 */
@Component({
  selector: 'app-strategy-gate-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './strategy-gate-picker.component.html',
  styleUrl: './strategy-gate-picker.component.scss',
})
export class StrategyGatePickerComponent {
  readonly gates = input.required<readonly StrategyViewGateView[]>();
  readonly activeGate = input.required<StrategyViewGateView>();

  readonly gateChange = output<string>();

  protected readonly popoverId = `strategy-gate-picker-${nextGatePickerId++}`;
  protected readonly titleId = `${this.popoverId}-title`;
  protected readonly orderedGates = computed(() => gatesStrategyFirst(this.gates()));

  private readonly trigger = viewChild.required<ElementRef<HTMLButtonElement>>('trigger');
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  /** Applies the pick and leaves the picker open: arrow keys step through
   * the radio group one change at a time, and Escape or a click outside close it. */
  protected choose(gateId: string): void {
    this.gateChange.emit(gateId);
  }

  protected onToggle(event: Event): void {
    if ('newState' in event && event.newState === 'open') {
      placePopover(this.panel().nativeElement, anchorBelow(this.trigger().nativeElement));
    }
  }
}

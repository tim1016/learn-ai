import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import type { CustomGate, CustomGateInput, StrategyViewGateView } from '../lib/broker-v2-panel.types';
import { anchorBelow, placePopover } from './popover-placement';
import { StrategyGateEditorComponent } from './strategy-gate-editor.component';
import { DRAFT_GATE_ID, type GateVariableGroup } from './strategy-gates-model';
import { gatesStrategyFirst } from './strategy-view-model';

let nextGatePickerId = 0;

/**
 * The Dark Bright Gate chip and its picker (#2639): which gate shades the
 * strategy candles, one at a time, the strategy's own rule first. Picking
 * one only changes the shading — gates never trade.
 *
 * The viewer's own gates are saved on the strategy (D10) and edited here:
 * "+ New gate" and Edit open the editor in place of the list, and Delete
 * asks once more. The host owns the choice, the saved list and every call
 * to the data plane; this emits the picked gate id and the draft to preview.
 */
@Component({
  selector: 'app-strategy-gate-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, StrategyGateEditorComponent],
  templateUrl: './strategy-gate-picker.component.html',
  styleUrl: './strategy-gate-picker.component.scss',
})
export class StrategyGatePickerComponent {
  readonly gates = input.required<readonly StrategyViewGateView[]>();
  readonly activeGate = input.required<StrategyViewGateView>();
  /** The viewer's gates saved on this strategy: the ones Edit and Delete apply to. */
  readonly savedGates = input<readonly CustomGate[]>([]);
  readonly strategyName = input.required<string>();
  readonly variables = input<readonly GateVariableGroup[]>([]);
  readonly draftRefusal = input<string | null>(null);
  readonly previewing = input(false);
  readonly save = input.required<(draft: CustomGateInput, gateId: string | null) => Promise<unknown>>();
  readonly remove = input.required<(gateId: string) => Promise<void>>();

  readonly gateChange = output<string>();
  readonly previewDraft = output<CustomGateInput>();
  /** The editor closed: whatever draft it previewed no longer applies. */
  readonly editorClosed = output();

  protected readonly popoverId = `strategy-gate-picker-${nextGatePickerId++}`;
  protected readonly titleId = `${this.popoverId}-title`;
  /** The previewed draft shades the chart but is not a gate to pick. */
  protected readonly orderedGates = computed(() =>
    gatesStrategyFirst(this.gates()).filter((gate) => gate.gate_id !== DRAFT_GATE_ID),
  );
  /** `{ gate: null }` writes a new gate; `null` shows the list. */
  protected readonly editing = signal<{ readonly gate: CustomGate | null } | null>(null);
  protected readonly confirmingDelete = signal<string | null>(null);
  protected readonly removeError = signal<string | null>(null);

  private readonly trigger = viewChild.required<ElementRef<HTMLButtonElement>>('trigger');
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  protected savedGate(gateId: string): CustomGate | null {
    return this.savedGates().find((gate) => gate.gate_id === gateId) ?? null;
  }

  /** Applies the pick and leaves the picker open: arrow keys step through
   * the radio group one change at a time, and Escape or a click outside close it. */
  protected choose(gateId: string): void {
    this.gateChange.emit(gateId);
  }

  protected openEditor(gate: CustomGate | null): void {
    this.confirmingDelete.set(null);
    this.removeError.set(null);
    this.editing.set({ gate });
  }

  protected closeEditor(): void {
    this.editing.set(null);
    this.editorClosed.emit();
  }

  protected async confirmDelete(gate: CustomGate): Promise<void> {
    this.removeError.set(null);
    try {
      await this.remove()(gate.gate_id);
      this.confirmingDelete.set(null);
    } catch (error) {
      this.removeError.set(error instanceof Error ? error.message : 'The gate could not be deleted.');
    }
  }

  protected onToggle(event: Event): void {
    if ('newState' in event && event.newState === 'open') {
      placePopover(this.panel().nativeElement, anchorBelow(this.trigger().nativeElement));
    }
  }
}

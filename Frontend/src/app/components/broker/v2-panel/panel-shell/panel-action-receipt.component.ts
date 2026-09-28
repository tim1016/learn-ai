import { ChangeDetectionStrategy, Component, ElementRef, computed, input, output, viewChild } from '@angular/core';
import { ReceiptLabelPipe, formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

export interface ActionReceiptView {
  readonly actionId: string;
  readonly outcome: 'success' | 'conflict' | 'failure' | 'unknown';
  readonly receiptId: string | null;
  readonly recordedAtMs: number;
  readonly message: string;
  readonly remediation: string | null;
  /** The backend's refusal code, when it sent one. */
  readonly reasonCode?: string | null;
  /** When a refused command can next be tried — a closed session's next open. */
  readonly availableAtMs?: number | null;
}

@Component({
  selector: 'app-panel-action-receipt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './panel-action-receipt.component.html',
  styleUrl: './panel-action-receipt.component.scss',
})
export class PanelActionReceiptComponent {
  readonly receipt = input.required<ActionReceiptView>();
  readonly dismissed = output();

  private readonly outcome = viewChild.required<ElementRef<HTMLElement>>('outcome');

  /** The refusal code, unless the remediation already is its label — a
   * refusal with no prose of its own falls back to exactly that. */
  protected readonly reasonCode = computed(() => {
    const { reasonCode, remediation } = this.receipt();
    return reasonCode && formatReceiptLabel(reasonCode) !== remediation ? reasonCode : null;
  });
  protected readonly availableAtMs = computed(() => this.receipt().availableAtMs ?? null);

  /** Move the keyboard to the outcome, so the owner hears what an action did (story 48). */
  focus(): void {
    this.outcome().nativeElement.focus();
  }
}

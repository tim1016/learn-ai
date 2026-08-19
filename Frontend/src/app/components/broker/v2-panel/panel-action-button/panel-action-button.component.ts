import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';

import type { PanelAction, PanelActionTrigger } from '../lib/broker-v2-panel.types';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

export type PanelActionTone = 'primary' | 'neutral' | 'warning' | 'danger';

/** Renders one backend-presented panel action with its confirmation and blockers. */
@Component({
  selector: 'app-panel-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReceiptLabelPipe,
    TypedHaltConfirmComponent,
  ],
  templateUrl: './panel-action-button.component.html',
  styleUrl: './panel-action-button.component.scss',
})
export class PanelActionButtonComponent {
  readonly action = input.required<PanelAction>();
  readonly pending = input(false);
  readonly tone = input<PanelActionTone>('neutral');
  readonly suppressedBlockerId = input<string | null>(null);
  readonly suppressedBlockerReasonCode = input<string | null>(null);

  readonly triggered = output<PanelActionTrigger>();
  protected readonly confirmationOpen = signal(false);

  protected readonly visibleBlockers = computed(() => {
    const suppressedBlockerId = this.suppressedBlockerId();
    return this.action().blockers.filter(
      (blocker) => blocker.condition.id !== suppressedBlockerId,
    );
  });

  protected readonly disabled = computed(
    () => !this.action().enabled || this.pending(),
  );

  protected trigger(): void {
    if (this.disabled()) return;
    if (this.action().confirmation) {
      this.confirmationOpen.set(true);
      return;
    }
    this.triggered.emit({ action: this.action(), reason: null });
  }

  protected confirm(): void {
    this.confirmationOpen.set(false);
    this.triggered.emit({ action: this.action(), reason: null });
  }

}

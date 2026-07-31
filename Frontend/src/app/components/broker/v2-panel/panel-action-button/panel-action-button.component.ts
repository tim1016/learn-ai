import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';

import type { PanelAction } from '../lib/broker-v2-panel.types';
import { TypedHaltConfirmComponent } from '../../bot-control/reused/typed-halt-confirm/typed-halt-confirm.component';

export type PanelActionTone = 'primary' | 'neutral' | 'warning' | 'danger';

/** Renders one backend-presented panel action with its confirmation and blockers. */
@Component({
  selector: 'app-panel-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TypedHaltConfirmComponent],
  templateUrl: './panel-action-button.component.html',
  styleUrl: './panel-action-button.component.scss',
})
export class PanelActionButtonComponent {
  readonly action = input.required<PanelAction>();
  readonly pending = input(false);
  readonly tone = input<PanelActionTone>('neutral');
  readonly suppressedBlockerId = input<string | null>(null);

  readonly triggered = output<PanelAction>();
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
    this.triggered.emit(this.action());
  }

  protected confirm(): void {
    this.confirmationOpen.set(false);
    this.triggered.emit(this.action());
  }
}

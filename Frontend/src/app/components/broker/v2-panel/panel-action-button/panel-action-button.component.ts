import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';

import type { PanelAction } from '../lib/broker-v2-panel.types';

export type PanelActionTone = 'primary' | 'neutral' | 'warning' | 'danger';

/** Renders one backend-presented panel action with its confirmation and blockers. */
@Component({
  selector: 'app-panel-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './panel-action-button.component.html',
  styleUrl: './panel-action-button.component.scss',
})
export class PanelActionButtonComponent {
  readonly action = input.required<PanelAction>();
  readonly pending = input(false);
  readonly tone = input<PanelActionTone>('neutral');

  readonly triggered = output<PanelAction>();

  protected readonly disabled = computed(
    () => !this.action().enabled || this.pending(),
  );

  protected trigger(): void {
    if (!this.disabled()) {
      this.triggered.emit(this.action());
    }
  }
}

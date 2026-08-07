import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';

import type { ActionId, PanelAction, PanelActionTrigger } from '../lib/broker-v2-panel.types';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import { PanelActionCommentConfirmComponent } from './panel-action-comment-confirm.component';

export type PanelActionTone = 'primary' | 'neutral' | 'warning' | 'danger';

/** Action ids whose confirmation requires a free-text operator comment,
 *  journaled as `PanelActionRequest.reason` (custody-resolution comment
 *  parity — Slice 3, Task 3.2). */
const COMMENT_REQUIRED_ACTION_IDS: ReadonlySet<ActionId> = new Set([
  'clear_hold',
  'record_inventory_baseline',
]);

/** Renders one backend-presented panel action with its confirmation and blockers. */
@Component({
  selector: 'app-panel-action-button',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TypedHaltConfirmComponent, PanelActionCommentConfirmComponent],
  templateUrl: './panel-action-button.component.html',
  styleUrl: './panel-action-button.component.scss',
})
export class PanelActionButtonComponent {
  readonly action = input.required<PanelAction>();
  readonly pending = input(false);
  readonly tone = input<PanelActionTone>('neutral');
  readonly suppressedBlockerId = input<string | null>(null);

  readonly triggered = output<PanelActionTrigger>();
  protected readonly confirmationOpen = signal(false);

  protected readonly requiresComment = computed(() =>
    COMMENT_REQUIRED_ACTION_IDS.has(this.action().action_id),
  );

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

  protected confirmWithReason(value: { reason: string }): void {
    this.confirmationOpen.set(false);
    this.triggered.emit({ action: this.action(), reason: value.reason });
  }
}

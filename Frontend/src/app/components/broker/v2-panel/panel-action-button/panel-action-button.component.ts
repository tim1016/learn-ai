import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';

import type { OperatorBlocker, OperatorMove } from '../../../../api/operator-blocker.types';
import { movesForBlocker } from '../../../../api/operator-blocker.types';
import type { PanelAction, PanelActionTrigger } from '../lib/broker-v2-panel.types';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

export type PanelActionTone = 'primary' | 'neutral' | 'warning' | 'danger';

/**
 * A move this component performs with no host help. `navigate` is the only
 * self-contained kind; everything else names something only the host knows how
 * to perform, so the host must declare support for it.
 */
function isSelfDispatchable(move: OperatorMove): boolean {
  return move.action.kind === 'navigate';
}

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
  // Not optional. `Router` is root-providable, so `{ optional: true }` never
  // yielded null — it only bought a fallback branch that emitted
  // `moveRequested` for a `navigate` move, an output the roster host does not
  // bind. That branch was unreachable dead weight dressed as a safety net.
  private readonly router = inject(Router);

  readonly action = input.required<PanelAction>();
  readonly pending = input(false);
  readonly tone = input<PanelActionTone>('neutral');
  readonly suppressedBlockerId = input<string | null>(null);
  readonly suppressedBlockerReasonCode = input<string | null>(null);
  /**
   * Lets the host declare which non-`navigate` moves it can actually
   * dispatch (which `confirm_in_form` anchors it recognizes). An
   * unsupported move renders no button — the control never offers a click
   * that silently does nothing, matching the account-desk posture card.
   */
  readonly moveIsSupported = input<(move: OperatorMove) => boolean>(isSelfDispatchable);

  readonly triggered = output<PanelActionTrigger>();
  readonly moveRequested = output<OperatorMove>();
  protected readonly confirmationOpen = signal(false);

  protected readonly visibleBlockers = computed(() => {
    const suppressedBlockerId = this.suppressedBlockerId();
    return this.action().blockers.filter(
      (blocker) => blocker.condition.id !== suppressedBlockerId,
    );
  });

  /**
   * Every blocker's backend-authored cure, including the blocker whose prose
   * the parent suppresses: suppression de-duplicates the gate's *prose*, and
   * a move is not prose. Filtering the cure out with the headline is what
   * left `fix_here` blockers authored-but-uncurable (#1778). `movesForBlocker`
   * keeps the disposition contract — `wait` yields nothing, by design.
   */
  protected readonly blockerMoves = computed<readonly OperatorMove[]>(() => {
    const isSupported = this.moveIsSupported();
    return this.action().blockers.flatMap((blocker: OperatorBlocker) =>
      movesForBlocker(blocker).filter(isSupported),
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

  protected requestMove(move: OperatorMove): void {
    if (move.action.kind === 'navigate') {
      void this.router.navigate([move.action.route], {
        fragment: move.action.fragment ?? undefined,
      });
      return;
    }
    this.moveRequested.emit(move);
  }
}

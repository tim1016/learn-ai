import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
} from '@angular/core';
import { Router } from '@angular/router';

import type { AccountOperatorPosture, OperatorBlocker, OperatorMove } from '../../../api/operator-blocker.types';
import { accountOperatorPostureBlocker, movesForBlocker } from '../../../api/operator-blocker.types';

interface PostureView {
  readonly headline: string;
  readonly detail: string | null;
  /** Null exactly when the account is healthy — drives disposition-based rendering. */
  readonly blocker: OperatorBlocker | null;
  readonly moves: readonly OperatorMove[];
}

/**
 * One decision-first operator posture card, authored entirely by the
 * backend-supplied `AccountOperatorPosture` (issue #1664). This component
 * renders `posture.account_desk` verbatim when a condition is active, or
 * the backend-authored healthy status copy when it is null. A missing or
 * malformed posture fails closed to an explicit "unavailable" state rather
 * than re-deriving a verdict from raw evidence.
 */
@Component({
  selector: 'app-alpaca-operator-posture',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './alpaca-operator-posture.component.html',
  styleUrl: './alpaca-operator-posture.component.scss',
})
export class AlpacaOperatorPostureComponent {
  private readonly router = inject(Router, { optional: true });
  readonly posture = input<AccountOperatorPosture | null>(null);

  readonly blockerMoveRequested = output<OperatorMove>();

  protected readonly view = computed<PostureView | null>(() => {
    const posture = this.posture();
    if (posture === null) return null;
    const blocker = accountOperatorPostureBlocker(posture, 'account_desk');
    return {
      headline: blocker?.headline ?? posture.status_headline,
      // A blocker's own null detail must render as "no detail", not fall
      // back to the unrelated healthy-case status_detail.
      detail: blocker !== null ? (blocker.detail ?? null) : posture.status_detail,
      blocker,
      moves: blocker !== null ? movesForBlocker(blocker) : [],
    };
  });

  protected requestMove(move: OperatorMove): void {
    if (move.action.kind === 'navigate' && this.router !== null) {
      void this.router.navigate([move.action.route], {
        fragment: move.action.fragment ?? undefined,
      });
      return;
    }
    this.blockerMoveRequested.emit(move);
  }
}

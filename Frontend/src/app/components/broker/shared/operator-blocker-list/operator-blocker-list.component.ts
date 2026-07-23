import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { OperatorBlocker, OperatorMove } from '../../../../api/operator-blocker.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

export interface OperatorBlockerMoveEvent {
  readonly blocker: OperatorBlocker;
  readonly move: OperatorMove;
}

@Component({
  selector: 'app-operator-blocker-list',
  imports: [ReceiptLabelPipe],
  templateUrl: './operator-blocker-list.component.html',
  styleUrl: './operator-blocker-list.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OperatorBlockerListComponent {
  readonly blockers = input.required<readonly OperatorBlocker[]>();
  readonly ariaLabel = input<string>('Operator blockers');
  readonly showMoves = input<boolean>(true);

  readonly moveSelected = output<OperatorBlockerMoveEvent>();

  moves(blocker: OperatorBlocker): OperatorMove[] {
    if (blocker.disposition === 'wait') return [];
    if (blocker.disposition === 'terminal') {
      return blocker.primary_move
        ? [blocker.primary_move, ...blocker.secondary_moves]
        : blocker.secondary_moves;
    }
    return blocker.primary_move ? [blocker.primary_move] : [];
  }

  trackBlocker(blocker: OperatorBlocker): string {
    return [
      blocker.host,
      blocker.condition.id,
      blocker.headline,
      blocker.primary_move?.label ?? 'no-primary-move',
    ].join(':');
  }

  isPrimaryMove(move: OperatorMove): boolean {
    return move.action.kind === 'confirm_in_form' ||
      move.action.kind === 'retire_replace' ||
      move.action.kind === 'remove';
  }

  selectMove(blocker: OperatorBlocker, move: OperatorMove): void {
    this.moveSelected.emit({ blocker, move });
  }
}

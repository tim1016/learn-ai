import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';

import { AccountDeskTransactionHistoryComponent } from '../../broker/account-desk/account-desk-transaction-history.component';
import { AccountDeskTransactionHistoryStore } from '../../broker/account-desk/account-desk-transaction-history-store.service';
import { ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR, type OperatorMove } from '../../../api/operator-blocker.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { SqliteTimelineQuery } from '../../../services/brokers.service';
import { AlpacaSqliteCustodyComponent } from './alpaca-sqlite-custody.component';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';
import { AlpacaOperatorPostureComponent } from './alpaca-operator-posture.component';

/** Mechanism, repair, and immutable receipt evidence for the Alpaca desk. */
@Component({
  selector: 'app-alpaca-operator-lens',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskTransactionHistoryComponent,
    AlpacaOperatorPostureComponent,
    AlpacaSqliteCustodyComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  providers: [AccountDeskTransactionHistoryStore],
  templateUrl: './alpaca-operator-lens.component.html',
  styleUrl: './alpaca-operator-lens.component.scss',
})
export class AlpacaOperatorLensComponent {
  readonly refreshVersion = input(0);
  readonly timelineQuery = input<SqliteTimelineQuery | null>(null);
  private readonly data = inject(AlpacaOperatorLensDataService);
  private readonly custodyPanel = viewChild<ElementRef<HTMLDetailsElement>>('custodyPanel');

  protected readonly status = this.data.status;
  protected readonly projection = this.data.projection;
  protected readonly projectionRefreshVersion = this.data.projectionRefreshVersion;
  protected readonly custodyOpened = signal(false);

  constructor() {
    effect(() => {
      if (this.timelineQuery() === null) return;
      this.openCustodyPanel();
    });
  }

  protected onCustodyToggle(event: Event): void {
    if (event.currentTarget instanceof HTMLDetailsElement && event.currentTarget.open) {
      this.custodyOpened.set(true);
    }
  }

  /**
   * This predicate replaces the posture card's own default, so it must
   * restate `navigate` (self-dispatched by the card) in addition to this
   * lens's own capability — the `confirm_in_form` recovery anchor, which
   * opens this lens's recovery panel in place. The card only ever renders
   * a button for a move that passes this predicate (#1664 review), so an
   * `open_runbook` terminal move — for which this lens has no target yet —
   * is not offered as a dead click instead of silently doing nothing.
   */
  protected readonly isBlockerMoveSupported = (move: OperatorMove): boolean =>
    move.action.kind === 'navigate' ||
    (move.action.kind === 'confirm_in_form' && move.action.anchor === ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR);

  protected onBlockerMoveRequested(move: OperatorMove): void {
    if (move.action.kind === 'confirm_in_form' && move.action.anchor === ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR) {
      this.openCustodyPanel();
    }
  }

  private openCustodyPanel(): void {
    this.custodyOpened.set(true);
    this.custodyPanel()?.nativeElement.setAttribute('open', '');
  }

  protected refreshCustodyProjection(): void {
    this.data.refreshProjection();
  }
}

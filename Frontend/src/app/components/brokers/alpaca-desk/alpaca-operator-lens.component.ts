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
import type { SqliteRecoveryAction } from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
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
  private readonly data = inject(AlpacaOperatorLensDataService);
  private readonly custodyPanel = viewChild<ElementRef<HTMLDetailsElement>>('custodyPanel');
  private readonly custody = viewChild(AlpacaSqliteCustodyComponent);

  protected readonly status = this.data.status;
  protected readonly projection = this.data.projection;
  protected readonly custodyOpened = signal(false);
  private readonly pendingRepair = signal<SqliteRecoveryAction | null>(null);

  constructor() {
    effect(() => {
      const action = this.pendingRepair();
      const custody = this.custody();
      if (action === null || custody === undefined) return;
      this.pendingRepair.set(null);
      custody.requestPresentedAction(action);
    });
  }

  protected onCustodyToggle(event: Event): void {
    if (event.currentTarget instanceof HTMLDetailsElement && event.currentTarget.open) {
      this.custodyOpened.set(true);
    }
  }

  protected openRepair(action: SqliteRecoveryAction): void {
    this.custodyOpened.set(true);
    this.pendingRepair.set(action);
    this.custodyPanel()?.nativeElement.setAttribute('open', '');
  }
}

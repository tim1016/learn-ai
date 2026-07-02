import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type {
  AccountTruthInvariant,
  AccountTruthMessage,
  AccountTruthOwnerSummary,
  AccountTruthResponse,
  AccountTruthSymbolExposure,
} from '../../../api/broker-models';
import { DataSourceComponent } from '../../../shared/data-source/data-source.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { fmtCurrency, fmtSignedNumber, fmtTimestampNy } from '../format';
import { AccountTruthExecutionHistoryComponent } from './account-truth-execution-history.component';

@Component({
  selector: 'app-account-truth-board',
  imports: [
    DataSourceComponent,
    ReceiptLabelPipe,
    AccountTruthExecutionHistoryComponent,
  ],
  templateUrl: './account-truth-board.component.html',
  styleUrl: './account-truth-board.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AccountTruthBoardComponent {
  readonly truth = input.required<AccountTruthResponse>();
  readonly showAccountMetrics = input(false);
  readonly showOwnerSummary = input(false);
  readonly showSymbolExposures = input(false);
  readonly showExecutionHistory = input(false);
  readonly showInvariants = input(false);
  readonly showCaveats = input(true);

  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedNumber = fmtSignedNumber;
  readonly fmtTimestampNy = fmtTimestampNy;

  trackInvariant = (_: number, invariant: AccountTruthInvariant): string => invariant.key;
  trackMessage = (_: number, message: AccountTruthMessage): string => message.code;
  trackOwner = (_: number, owner: AccountTruthOwnerSummary): string =>
    `${owner.owner_class}:${owner.owner_key}:${owner.evidence_tier}:${owner.owner_binding_state}`;
  trackExposure = (_: number, exposure: AccountTruthSymbolExposure): string =>
    `${exposure.symbol}:${exposure.owner_key}:${exposure.con_id ?? 'none'}`;
}

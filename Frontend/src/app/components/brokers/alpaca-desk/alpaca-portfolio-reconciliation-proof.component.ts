import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { CurrencyPipe, DecimalPipe } from '@angular/common';

import type { PortfolioHistoryProof } from '../../../api/alpaca.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

type AttributionRow = NonNullable<
  PortfolioHistoryProof['attribution']
>['attribution_rows'][number];

/** Presentation-only C1/C2/C3 proof; values and classifications are server-owned. */
@Component({
  selector: 'app-alpaca-portfolio-reconciliation-proof',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    CurrencyPipe,
    DecimalPipe,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-portfolio-reconciliation-proof.component.html',
  styleUrl: './alpaca-portfolio-reconciliation-proof.component.scss',
})
export class AlpacaPortfolioReconciliationProofComponent {
  readonly proof = input<PortfolioHistoryProof | undefined>(undefined);
  readonly unavailable = input(false);

  trackAttribution = (_: number, row: AttributionRow): string =>
    `${row.entry_strategy_instance_id}:${row.opened_at_ms}:${row.exit_strategy_instance_id}:${row.closed_at_ms}:${row.quantity}`;
}

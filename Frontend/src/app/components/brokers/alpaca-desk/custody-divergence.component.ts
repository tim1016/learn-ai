import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { CustodyDivergence, CustodyPositionDelta } from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

/**
 * Single custody-divergence card: kind/state chip, explanation, position
 * deltas, possible causes, prerequisite detail, and evidence refs. Shared
 * presentational block for the two custody-resolution surfaces that render
 * a divergence list — the read-only Accounts-page card
 * (`AlpacaCustodyResolutionComponent`) and the resolve confirm dialog
 * (`CustodyResolutionConfirmDialogComponent`) — extracted so both render the
 * same full content instead of two hand-maintained copies drifting apart.
 */
@Component({
  selector: 'app-custody-divergence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './custody-divergence.component.html',
  styleUrl: './custody-divergence.component.scss',
  host: { class: 'block' },
})
export class CustodyDivergenceComponent {
  readonly divergence = input.required<CustodyDivergence>();

  protected readonly positionDeltas = computed<CustodyPositionDelta[]>(
    () => this.divergence().position_deltas ?? [],
  );
  protected readonly evidenceRefs = computed<string[]>(
    () => this.divergence().evidence_refs ?? [],
  );
}

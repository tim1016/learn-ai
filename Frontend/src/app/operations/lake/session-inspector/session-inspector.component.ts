import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { PriceAdjustmentMode } from '../../../shared/data-lake';
import type { CoverageCellSelection } from '../coverage-heatmap/coverage-heatmap.component';
import type { RebuildRequest } from '../lib/lake-readiness';
import { sessionChecks, sessionVerdict } from '../lib/session-checks';

/**
 * One session, checked the way a run checks it: the price file, the
 * adjustment record, the catalog entry, and whether a run can read the
 * session as it stands.
 */
@Component({
  selector: 'app-session-inspector',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './session-inspector.component.html',
  styleUrl: './session-inspector.component.scss',
  imports: [AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent],
})
export class SessionInspectorComponent {
  readonly selection = input<CoverageCellSelection | null>(null);
  readonly mode = input.required<PriceAdjustmentMode>();
  /** The selected symbol's rebuild, when a backfill can rebuild any of it. */
  readonly rebuildRequest = input<RebuildRequest | null>(null);
  /** True while any backfill runs, so a second one cannot be started over it. */
  readonly busy = input(false);

  readonly rebuild = output<RebuildRequest>();
  readonly receiptOpened = output<CoverageCellSelection>();

  protected readonly checks = computed(() => {
    const selected = this.selection();
    return selected === null ? [] : sessionChecks(selected.cell, this.mode());
  });

  protected readonly verdict = computed(() => {
    const selected = this.selection();
    return selected === null ? null : sessionVerdict(selected.cell);
  });
}

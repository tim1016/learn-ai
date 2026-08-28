import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type {
  BackfillError,
  BackfillPhase,
  BackfillProgress,
} from '../lib/data-lake-backfill.store';
import type { BackfillDayEvent, BackfillFailure } from '../lib/data-lake.types';

/**
 * Live narration of one backfill run: the per-day progress tick, each
 * session's outcome, and every typed failure.
 *
 * Failure `reason` codes and the job's terminal error `code` are backend
 * identifiers and render through the receipt-label pipe; the free-text
 * `detail` beside them is the provider's own words and is reproduced as-is.
 * A failure is attributed to its symbol, because a multi-symbol backfill
 * produces one per symbol per session and an unattributed receipt cannot be
 * acted on.
 */
@Component({
  selector: 'app-backfill-run-log',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './backfill-run-log.component.html',
  styleUrl: './backfill-run-log.component.scss',
  imports: [AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent],
})
export class BackfillRunLogComponent {
  readonly phase = input.required<BackfillPhase>();
  readonly percent = input(0);
  readonly progress = input<BackfillProgress | null>(null);
  readonly days = input<readonly BackfillDayEvent[]>([]);
  readonly failures = input<readonly BackfillFailure[]>([]);
  readonly error = input<BackfillError | null>(null);
  readonly jobId = input<string | null>(null);
  /** True when the panel adopted a run already in flight rather than starting it. */
  readonly reattached = input(false);

  /**
   * Identity of one failure row.
   *
   * `(date, kind, reason)` is not unique: a two-symbol backfill that hits
   * the same provider error on the same session emits one failure per
   * symbol, identical in all three. Angular rejects duplicate `@for`
   * identities outright, so the symbol and data type — the two axes
   * `expand_required_artifacts` fans out over — are part of the key.
   */
  protected failureKey(failure: BackfillFailure): string {
    return [
      failure.trading_date_ms ?? 'none',
      failure.symbol ?? 'none',
      failure.data_type ?? 'none',
      failure.artifact_kind,
      failure.reason,
    ].join('/');
  }
}

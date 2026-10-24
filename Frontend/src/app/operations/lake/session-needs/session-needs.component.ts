import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { PriceAdjustmentMode } from '../../../shared/data-lake';
import { needsAdjustmentRecord } from '../lib/lake-readiness';

/**
 * The three things a run needs for one session, and that a backfill writes
 * every one of them. The adjustment record is the one an operator never
 * sees and cannot create by hand, so it is the one drawn loud when symbols
 * are missing it.
 */
@Component({
  selector: 'app-session-needs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './session-needs.component.html',
  styleUrl: './session-needs.component.scss',
})
export class SessionNeedsComponent {
  readonly mode = input.required<PriceAdjustmentMode>();
  /** Symbols with sessions a backfill can rebuild. */
  readonly missingSymbolCount = input(0);

  protected readonly needsRecord = computed(() => needsAdjustmentRecord(this.mode()));

  protected readonly missing = computed(() => {
    const count = this.missingSymbolCount();
    if (!this.needsRecord() || count === 0) return null;
    return count === 1 ? 'Missing or out of date for 1 symbol' : `Missing or out of date for ${count} symbols`;
  });
}

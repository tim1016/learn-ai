import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { LakeReadinessTotals, RebuildRequest } from '../lib/lake-readiness';

/** Where a "rebuild all" run stands: the symbol being rebuilt and its place in the list. */
export interface RebuildProgress {
  readonly symbol: string;
  readonly position: number;
  readonly total: number;
}

/**
 * Says, before anything else on the page, that symbols on disk cannot be
 * read by a run, why, and that a backfill writes what is missing.
 *
 * Shown only while a backfill can fix something; sessions too old to fetch
 * again are named in the symbols table, where no button promises a fix.
 */
@Component({
  selector: 'app-readiness-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './readiness-banner.component.html',
  styleUrl: './readiness-banner.component.scss',
  imports: [DecimalPipe],
})
export class ReadinessBannerComponent {
  readonly totals = input.required<LakeReadinessTotals>();
  /** The selected symbol's rebuild, when it has one. */
  readonly selected = input<RebuildRequest | null>(null);
  /** True while any backfill runs, so a second one cannot be started over it. */
  readonly busy = input(false);
  readonly progress = input<RebuildProgress | null>(null);

  readonly rebuildAll = output();
  readonly rebuildSelected = output<RebuildRequest>();

  protected readonly title = computed(() => {
    const { rebuildSymbolCount, symbolCount } = this.totals();
    const one = rebuildSymbolCount === 1;
    return `${rebuildSymbolCount} of ${symbolCount} symbols ${one ? 'needs' : 'need'} a rebuild before a run can read ${one ? 'it' : 'them'}`;
  });
}

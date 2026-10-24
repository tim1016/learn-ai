import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { RebuildRequest, SymbolReadinessRow } from '../lib/lake-readiness';

type ReadinessFilter = 'all' | 'rebuild' | 'gaps' | 'ready';

interface FilterChip {
  readonly key: ReadinessFilter;
  readonly label: string;
  readonly count: number;
}

/**
 * Every symbol the lake holds in this view, answering one question per row:
 * can a run read it? A symbol whose sessions stop and start again says so:
 * a run laid over the gap is refused. A row is also how the session grid's
 * symbol is chosen.
 */
@Component({
  selector: 'app-symbol-readiness-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './symbol-readiness-table.component.html',
  styleUrl: './symbol-readiness-table.component.scss',
  imports: [AssetIdentityComponent, DecimalPipe, TimestampDisplayComponent],
})
export class SymbolReadinessTableComponent {
  readonly rows = input.required<readonly SymbolReadinessRow[]>();
  readonly selectedSymbol = input<string | null>(null);
  /** True while any backfill runs, so a second one cannot be started over it. */
  readonly busy = input(false);

  readonly symbolSelected = output<string>();
  readonly rebuild = output<RebuildRequest>();

  protected readonly filter = signal<ReadinessFilter>('all');

  protected readonly chips = computed<readonly FilterChip[]>(() => {
    const rows = this.rows();
    return [
      { key: 'all', label: 'All', count: rows.length },
      { key: 'rebuild', label: 'Needs rebuild', count: rows.filter(needsRebuild).length },
      { key: 'gaps', label: 'Has gaps', count: rows.filter(hasGaps).length },
      { key: 'ready', label: 'Ready', count: rows.filter(isReady).length },
    ];
  });

  protected readonly visible = computed(() => {
    const rows = this.rows();
    switch (this.filter()) {
      case 'all':
        return rows;
      case 'rebuild':
        return rows.filter(needsRebuild);
      case 'gaps':
        return rows.filter(hasGaps);
      case 'ready':
        return rows.filter(isReady);
    }
  });

  /** The answer to "can a run read it?", then what stands in the way. */
  protected verdict(row: SymbolReadinessRow): string {
    if (isReady(row)) return 'Yes';
    if (row.state === 'ready') return row.gapCount === 1 ? 'Has a gap' : 'Has gaps';
    const answer = row.state === 'partial' ? 'Partly' : 'No';
    return row.rebuildableCount > 0 ? `${answer} · needs rebuild` : `${answer} · too old to rebuild`;
  }

  /** The pill's colour: every held session of a symbol with a gap is readable, and a run over the gap is still refused. */
  protected pillState(row: SymbolReadinessRow): SymbolReadinessRow['state'] {
    return row.state === 'ready' && hasGaps(row) ? 'partial' : row.state;
  }
}

function needsRebuild(row: SymbolReadinessRow): boolean {
  return row.rebuildableCount > 0;
}

function hasGaps(row: SymbolReadinessRow): boolean {
  return row.gapSessionCount > 0;
}

function isReady(row: SymbolReadinessRow): boolean {
  return row.state === 'ready' && !hasGaps(row);
}

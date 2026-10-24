import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { InstrumentDataReport } from '../../shared/data-lake';
import { TimestampDisplayPipe } from '../../shared/timestamp';

interface Tile {
  readonly label: string;
  readonly value: number;
  readonly hint?: string;
}

/**
 * What the lake holds for the window, said in the numbers Python counted:
 * the sessions the calendar schedules, how many a run can read, and each
 * thing holding the rest back, plus the provider's history floor and the
 * derived daily file, which are separate facts.
 */
@Component({
  selector: 'app-data-summary',
  imports: [TimestampDisplayPipe],
  templateUrl: './data-summary.component.html',
  styleUrl: './data-summary.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataSummaryComponent {
  readonly report = input.required<InstrumentDataReport>();

  protected readonly summary = computed(() => this.report().summary);
  /** Sessions whose tape is finished: the ones a run can be refused. */
  protected readonly finished = computed(() => this.summary().session_count - this.summary().forming - this.summary().upcoming);
  protected readonly unreadable = computed(() => this.finished() - this.summary().ready);

  protected readonly tiles = computed<readonly Tile[]>(() => {
    const s = this.summary();
    const held = [
      { label: 'Ready', value: s.ready, hint: 'A run can read these' },
      { label: 'Needs rebuild', value: s.needs_rebuild, hint: 'The catalog calls them complete; a run refuses them' },
      { label: 'Missing', value: s.missing, hint: 'No catalog row' },
      { label: 'Fetch failed', value: s.failed, hint: 'The last fetch failed' },
      { label: 'Being written', value: s.building, hint: 'Another run holds the claim' },
      { label: 'Not finished', value: s.forming + s.upcoming, hint: 'Today and later: nothing immutable exists yet' },
    ];
    return held.filter((tile) => tile.label === 'Ready' || tile.value > 0);
  });

  protected readonly rollup = computed(() => this.report().daily_rollup);
}

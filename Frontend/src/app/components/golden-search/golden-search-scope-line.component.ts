import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { StudyScope } from './golden-search.types';

/**
 * The scope line every study step shows (#2696): the development dates the
 * evidence comes from, the final-test dates and whether they are still
 * locked or were opened once, and the capital and costs every comparison
 * shares. All of it is the server's `scope`; interval ends are exclusive.
 */
@Component({
  selector: 'app-golden-search-scope-line',
  imports: [DecimalPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let s = scope();
    <p class="scope" role="note" aria-label="Study scope">
      <span><i class="pi pi-calendar" aria-hidden="true"></i> Development: <app-timestamp-display [value]="s.development_label_start_ms" mode="date-et" /> – <app-timestamp-display [value]="s.development_end_ms - 1" mode="date-et" /></span>
      <span><i class="pi" [class.pi-lock]="s.final_state === 'locked'" [class.pi-eye]="s.final_state === 'opened_once'" aria-hidden="true"></i> Final test <app-timestamp-display [value]="s.final_start_ms" mode="date-et" /> – <app-timestamp-display [value]="s.final_end_ms - 1" mode="date-et" />: {{ s.final_state === 'locked' ? 'locked' : 'opened once' }}</span>
      <span>\${{ s.capital | number: '1.0-0' }} starting capital · {{ s.costs_sentence }}</span>
    </p>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .scope { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--space-1) var(--space-4); margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); }
  `,
})
export class GoldenSearchScopeLineComponent {
  readonly scope = input.required<StudyScope>();
}

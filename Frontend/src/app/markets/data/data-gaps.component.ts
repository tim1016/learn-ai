import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { InstrumentSession } from './instrument-data';
import { TimestampDisplayPipe } from '../../shared/timestamp';
import { SESSION_STATE_LABELS, gapCauseText } from './gap-copy';
import { gapStretches, type GapStretch } from './instrument-data';

/**
 * The window's gaps before anything fills them: each stretch of neighbouring
 * sessions that a run cannot read, with its dates, how many sessions it is,
 * what holds it back and what a Fill does about it. Every stretch is listed;
 * nothing is summed away.
 */
@Component({
  selector: 'app-data-gaps',
  imports: [TimestampDisplayPipe],
  templateUrl: './data-gaps.component.html',
  styleUrl: './data-gaps.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataGapsComponent {
  readonly sessions = input.required<readonly InstrumentSession[]>();

  protected readonly stretches = computed(() => gapStretches(this.sessions()));

  protected stateLabel(stretch: GapStretch): string {
    return SESSION_STATE_LABELS[stretch.state];
  }

  protected causeText(stretch: GapStretch): string {
    return gapCauseText(stretch);
  }
}

import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { rehearsalSpec } from './golden-search-rehearsal-charts';
import type { CandidateRef, ChanceRehearsal, MeasuredChanceRehearsal } from './golden-search.types';

/**
 * Compare's final-test rehearsal panel (#3012): the selected fit's four-rung
 * ladder — the share of resampled development sessions that would meet every
 * modeled final rule, each rule's own share, the owner's 80% reading
 * heuristic, and Python's guidance on which rule limits the ladder. Every
 * count, share, floor and sentence is the server's stored reading; a
 * missing, unsupported or failed rehearsal says so in the recorded words,
 * and the panel never suggests a concrete date range or a test extension.
 */
@Component({
  selector: 'app-golden-search-rehearsal-panel',
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-rehearsal-panel.component.html',
  styleUrl: './golden-search-rehearsal-panel.component.scss',
})
export class GoldenSearchRehearsalPanelComponent {
  /** The fit the panel explains: its key for colour and its label for words. */
  readonly candidate = input.required<CandidateRef>();
  /** The fit's stored final-test rehearsal reading. */
  readonly rehearsal = input.required<ChanceRehearsal>();

  protected readonly measured = computed<MeasuredChanceRehearsal | null>(() => {
    const rehearsal = this.rehearsal();
    return rehearsal.status === 'measured' ? rehearsal : null;
  });

  protected readonly spec = computed(
    () => {
      const measured = this.measured();
      return measured === null ? null : rehearsalSpec(this.candidate(), measured, this.candidate().key);
    },
    // Equal while it draws the same stored reading, so a study poll (new objects, same evidence) does not redraw it.
    { equal: sameChart },
  );

  /** What the panel says when no rehearsal was drawn, narrowed here so the template only words it. */
  protected readonly note = computed<{ readonly kind: 'missing' | 'failed'; readonly text: string } | null>(() => {
    const rehearsal = this.rehearsal();
    switch (rehearsal.status) {
      case 'missing':
        return { kind: 'missing', text: rehearsal.reason };
      case 'failed':
        return { kind: 'failed', text: rehearsal.sentence };
      default:
        return null;
    }
  });
}

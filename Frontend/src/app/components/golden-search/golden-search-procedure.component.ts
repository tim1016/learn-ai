import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { knobsByName, pointEntries } from './golden-search-display';
import { GoldenSearchMetricsComponent } from './golden-search-metrics.component';
import { GoldenSearchProcedurePathComponent } from './golden-search-procedure-path.component';
import type { GoldenSearchMethod, ProcedureView, StrategyCapability } from './golden-search.types';

/**
 * One search procedure's evidence (#2696): the evaluated/cached/invalid
 * counts, each searched knob's starting value, retained value and why it
 * stopped, why the whole search stopped, knobs that ended at the edge of
 * their searched range, the retained settings with their development
 * metrics, and — for Zoom — the round-by-round path on request. Every number
 * is the server's.
 */
@Component({
  selector: 'app-golden-search-procedure',
  imports: [DecimalPipe, GoldenSearchMetricsComponent, GoldenSearchProcedurePathComponent, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-procedure.component.html',
  styleUrl: './golden-search-procedure.component.scss',
})
export class GoldenSearchProcedureComponent {
  readonly procedure = input.required<ProcedureView>();
  readonly heading = input.required<string>();
  readonly description = input.required<string>();
  readonly method = input.required<GoldenSearchMethod>();
  readonly capability = input<StrategyCapability | null>(null);
  /** Unique per page; ties the section to its heading. */
  readonly headingId = input.required<string>();

  protected readonly winner = computed(() => pointEntries(this.procedure().winner, this.capability()));
  protected readonly edgeHits = computed(() => {
    const knobs = knobsByName(this.capability());
    return this.procedure().edge_hits.map((name) => knobs.get(name)?.label ?? name);
  });
  protected readonly passes = computed(() => {
    const passes = this.procedure().passes_completed;
    return `${passes} complete ${passes === 1 ? 'pass' : 'passes'}.`;
  });
}

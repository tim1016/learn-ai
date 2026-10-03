import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { pairMapView } from './golden-search-compare';
import { pairLandscapeSpec } from './golden-search-search-charts';
import type { PairMap, Point, StrategyCapability } from './golden-search.types';

/**
 * The pair landscape (#2696, #2821 V11): two knobs' development net return
 * through one candidate, every other setting held at its value, one pair at a
 * time when the plan audited more than one. Cells the rules could not test
 * say why; the candidate's own cell is outlined. It shows a slice, never
 * robustness.
 */
@Component({
  selector: 'app-golden-search-pair-landscape',
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (view(); as v) {
      <app-golden-search-panel chart="pair-landscape" [instance]="instance()">
        @if (views().length > 1) {
          <div legend class="choices" role="group" aria-label="Which pair of knobs">
            @for (option of views(); track option.id; let i = $index) {
              <button type="button" [attr.aria-pressed]="option.id === v.id" (click)="chosen.set(i)">{{ option.title }}</button>
            }
          </div>
        }
        @if (spec(); as s) { <app-golden-search-chart [spec]="s" [height]="height()" [minWidth]="360" /> }
        <p caption class="caption">
          All other settings held at {{ centerLabel() }}@if (v.heldFixed) {: {{ v.heldFixed }}}. A two-knob slice of development data: it cannot establish robustness across every setting or future market.
        </p>
      </app-golden-search-panel>
    }
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .choices { display: flex; flex-wrap: wrap; gap: var(--space-1); }
    .choices button { border: 1px solid var(--border-light); border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); padding: var(--space-1) var(--space-2); font: inherit; font-size: var(--fs-xs); cursor: pointer; }
    .choices button[aria-pressed='true'] { background: var(--bg-elevated); color: var(--text-primary); }
    .choices button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .caption { margin: 0; color: var(--text-secondary); font-size: var(--fs-xs); }
  `,
})
export class GoldenSearchPairLandscapeComponent {
  readonly maps = input.required<readonly PairMap[]>();
  readonly capability = input<StrategyCapability | null>(null);
  /** The settings the maps are drawn through. */
  readonly center = input<Point | null>(null);
  /** Who `center` is, e.g. "the all-period result". */
  readonly centerLabel = input.required<string>();
  readonly instance = input<string | null>(null);

  protected readonly chosen = signal(0);
  protected readonly views = computed(() => this.maps().map((map) => pairMapView(map, this.capability(), this.center())));
  protected readonly view = computed(() => {
    const views = this.views();
    return views[Math.min(this.chosen(), views.length - 1)] ?? null;
  });
  protected readonly spec = computed(() => {
    const view = this.view();
    return view === null ? null : pairLandscapeSpec(view);
  }, { equal: sameChart });
  /** A row per value of the knob down the side. */
  protected readonly height = computed(() => 64 + 30 * (this.view()?.yValues.length ?? 0));
}

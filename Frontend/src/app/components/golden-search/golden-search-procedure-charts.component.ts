import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { GoldenSearchPairLandscapeComponent } from './golden-search-pair-landscape.component';
import { convergenceSpec, eligibilityMapSpec, knobMovesSpec, profilesMinWidth, profilesSpec } from './golden-search-search-charts';
import type { PairMap, Point, ProcedureCharts, StrategyCapability } from './golden-search.types';

const LABELS: Readonly<Record<ProcedureCharts['key'], string>> = { search: 'All-period', recent: 'Recent window' };

/**
 * One Search procedure's panels (#2821): the path Zoom took, each knob's move
 * and profile, every point scored against the rules, and — for the
 * all-period search — the pair landscape. The recent fit's panels are named
 * apart (the panel's instance). Each chart stays drawn while a reload brings
 * the same values.
 */
@Component({
  selector: 'app-golden-search-procedure-charts',
  imports: [GoldenSearchChartComponent, GoldenSearchGridComponent, GoldenSearchPairLandscapeComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let zoom = procedure().method === 'zoom';
    <app-golden-search-grid>
      @if (zoom) {
        <app-golden-search-panel chart="search-path" data-span="8" [instance]="instance()">
          @if (path(); as spec) {
            <app-golden-search-chart [spec]="spec" [height]="260" />
          } @else if (pathNote(); as note) {
            <p class="muted">{{ note }}</p>
          }
          <p caption class="caption">Each dot is a point the search scored, in order; the line is the best result so far that meets the rules.</p>
        </app-golden-search-panel>
      }
      <app-golden-search-panel chart="knob-moves" [attr.data-span]="zoom ? 4 : 6" [instance]="instance()">
        @if (moves(); as spec) { <app-golden-search-chart [spec]="spec" [height]="220" /> }
        <p caption class="caption">Hollow: the starting value. Filled: the value kept; amber at an end of its range.</p>
      </app-golden-search-panel>
      <app-golden-search-panel chart="eligibility-map" data-span="6" [instance]="instance()">
        @if (eligibility(); as spec) { <app-golden-search-chart [spec]="spec" [height]="260" /> }
        <p caption class="caption">
          Every point scored on this window, the winner outlined: blue meets the rules, amber too few trades, red too deep a fall, light blue no profit, grey failed or undefined. The dashed lines are the trade floor{{ procedure().policy.require_positive_net ? ' and $0' : '' }}.
        </p>
      </app-golden-search-panel>
      @if (maps().length > 0) {
        <app-golden-search-pair-landscape data-span="6" [maps]="maps()" [capability]="capability()" [center]="center()" centerLabel="the all-period result" [instance]="instance()" />
      }
      <app-golden-search-panel chart="knob-profiles" data-span="12" [instance]="instance()">
        @if (profiles(); as spec) {
          <app-golden-search-chart [spec]="spec" [height]="220" [minWidth]="profilesMinWidth()" />
        } @else {
          <p class="muted">No profile can be drawn: the search path could not be rebuilt.</p>
        }
        <p caption class="caption">{{ zoom ? 'Each knob as its last pass searched it, the other knobs held at their values then — not at the final winner.' : 'Each knob through the winner, the other knobs at the winner’s values.' }}</p>
      </app-golden-search-panel>
    </app-golden-search-grid>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    p { margin: 0; }
    .muted, .caption { color: var(--text-secondary); font-size: var(--fs-xs); }
  `,
})
export class GoldenSearchProcedureChartsComponent {
  readonly procedure = input.required<ProcedureCharts>();
  readonly capability = input<StrategyCapability | null>(null);
  /** The pair maps to draw beside this procedure; empty for the recent fit. */
  readonly maps = input<readonly PairMap[]>([]);
  readonly center = input<Point | null>(null);

  private readonly label = computed(() => LABELS[this.procedure().key]);
  /** Null for the all-period search; names the recent fit's panels apart from it. */
  protected readonly instance = computed(() => (this.procedure().key === 'search' ? null : this.label()));
  protected readonly path = computed(
    () => {
      const procedure = this.procedure();
      return procedure.convergence.status === 'measured' ? convergenceSpec(procedure, procedure.convergence.tried, this.capability(), this.label()) : null;
    },
    { equal: sameChart },
  );
  protected readonly pathNote = computed(() => {
    const convergence = this.procedure().convergence;
    return convergence.status === 'missing' ? convergence.reason : null;
  });
  protected readonly moves = computed(() => (this.procedure().moves.length === 0 ? null : knobMovesSpec(this.procedure(), this.label())), { equal: sameChart });
  protected readonly profiles = computed(() => (this.procedure().profiles.length === 0 ? null : profilesSpec(this.procedure(), this.label())), { equal: sameChart });
  protected readonly profilesMinWidth = computed(() => profilesMinWidth(this.procedure().profiles.length));
  protected readonly eligibility = computed(
    () => (this.procedure().points.length === 0 ? null : eligibilityMapSpec(this.procedure(), this.capability(), this.label())),
    { equal: sameChart },
  );
}

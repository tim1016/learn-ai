import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import type { GoldenSearchChartId } from './charts/golden-search-chart-guides';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { reloadOnProgress } from './charts/golden-search-reload';
import { runCurvesSpec } from './golden-search-run-curves-chart';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateKey, CurveSetKey, StudyDetail } from './golden-search.types';

/**
 * One step's runs drawn together (#2930): every point a search scored behind
 * its winner and the current settings, each fold's test run beside the
 * current settings', or a candidate with its cost-stress and neighbor runs.
 * The server names the set's lines and their roles; this panel reads the set
 * once per study (and again when the study records more), draws it, and says
 * how many runs the read left out or kept no curve for.
 */
@Component({
  selector: 'app-golden-search-curve-set',
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-golden-search-panel [chart]="chart()" [instance]="instance()">
      @if (error(); as message) {
        <div role="alert" class="failed">
          <p>{{ message }}</p>
          <button type="button" class="link" (click)="lines.reload()">Try again</button>
        </div>
      } @else if (spec(); as drawn) {
        <app-golden-search-chart [spec]="drawn" [height]="340" />
      } @else if (set(); as read) {
        <p class="muted">{{ read.not_recorded > 0 ? 'These runs were recorded before runs kept their curve.' : empty() }}</p>
      } @else {
        <p class="muted" role="status">Loading the curves…</p>
      }
      <p caption class="caption">
        {{ caption() }}
        @if (set(); as read) {
          @if (read.left_out > 0) { {{ read.left_out }} more scored points are not drawn here; All runs lists every one. }
          @if (read.not_recorded > 0 && read.lines.length > 0) { {{ read.not_recorded }} of these runs kept no curve. }
        }
      </p>
    </app-golden-search-panel>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    p { margin: 0; }
    .muted, .caption { color: var(--text-secondary); font-size: var(--fs-xs); }
    .failed { display: grid; gap: var(--space-1); color: var(--bear); font-size: var(--fs-sm); }
    .link { all: unset; justify-self: start; color: var(--accent-text); font-size: var(--fs-xs); cursor: pointer; }
    .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  `,
})
export class GoldenSearchCurveSetComponent {
  readonly study = input.required<StudyDetail>();
  /** Which runs to draw. */
  readonly curves = input.required<CurveSetKey>();
  /** Whose runs the candidate set holds; null for the other sets. */
  readonly candidate = input<CandidateKey | null>(null);
  readonly chart = input.required<GoldenSearchChartId>();
  /** Tells apart two panels of one chart on a page. */
  readonly instance = input<string | null>(null);
  readonly caption = input.required<string>();
  /** What the panel says when the set holds no run yet. */
  readonly empty = input('No run is recorded for this chart yet.');

  private readonly service = inject(GoldenSearchService);

  /** The read follows the study's id, not the study object a poll replaces. */
  private readonly studyId = computed(() => this.study().id);
  protected readonly lines = resource({
    params: () => ({ studyId: this.studyId(), curves: this.curves(), candidate: this.candidate() }),
    loader: ({ params }) => this.service.curveSet(params.studyId, params.curves, params.candidate),
  });
  protected readonly set = computed(() => (this.lines.hasValue() ? this.lines.value() : null));
  protected readonly error = computed(() => {
    const error = this.lines.error();
    return error === undefined ? null : extractServerMessage(error, 'These runs’ curves could not be loaded.');
  });
  protected readonly spec = computed(
    () => {
      const set = this.set();
      if (set === null || set.lines.length === 0) return null;
      const drawn = set.lines.map((line) => ({ key: line.run.evaluation_key, label: line.label, curve: line.curve, role: line.role }));
      return runCurvesSpec(drawn, this.study().protocol.policy.max_drawdown_ceiling);
    },
    { equal: sameChart },
  );

  constructor() {
    reloadOnProgress(() => `${this.study().revision}|${this.study().consumed_evaluations}`, this.lines);
  }
}

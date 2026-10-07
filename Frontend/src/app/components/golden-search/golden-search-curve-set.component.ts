import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import type { GoldenSearchChartId } from './charts/golden-search-chart-guides';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { reloadOnProgress } from './charts/golden-search-reload';
import { stepCurvesSpec } from './golden-search-step-curves-chart';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateKey, CurveSetKey, StudyDetail } from './golden-search.types';

/**
 * What each set's runs move with, so a panel reads again only when a step that feeds it records more. A
 * search's set also follows the later steps: its current-settings line can be a run one of them made.
 */
function setProgress(study: StudyDetail, curves: CurveSetKey): string {
  const results = study.results;
  const folds = (results.validation?.folds ?? []).map((fold) => fold.status).join();
  const evidence = results.evidence === null ? 'no evidence' : 'evidence';
  if (curves === 'fold_tests') return folds;
  if (curves === 'candidate') return evidence;
  const own = results[curves];
  return `${own?.evaluations ?? 0}|${own?.winner_hash ?? ''}|${folds}|${evidence}`;
}

/**
 * One step's runs drawn together (#2930): every point a search scored behind
 * its winner and the current settings, each fold's test run beside the
 * current settings', or a candidate with its cost-stress and neighbor runs.
 * The server names the set's lines and their roles; this panel reads the set
 * once per study (and again when its own step records more), draws it, and
 * says what the set could not show: points past its cap, completed runs
 * that kept no curve, runs that failed, and the server's own notes.
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
      } @else if (set()) {
        <p class="muted">{{ nothingDrawn() }}</p>
      } @else {
        <p class="muted" role="status">Loading the curves…</p>
      }
      <p caption class="caption">
        {{ caption() }}
        @if (set(); as read) {
          @for (note of read.notes; track note) { {{ note }} }
          @if (read.left_out > 0) { An even spread of the scored points is drawn; {{ read.left_out }} more are not. All runs lists every one. }
          @if (read.not_recorded > 0 && read.lines.length > 0) { {{ read.not_recorded }} completed runs of this set have no curve recorded. }
          @if (read.failed > 0 && read.lines.length > 0) { Runs of this set that failed: {{ read.failed }}. }
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
  /** Read on its own, so a study poll that changes nothing else does not rebuild the chart. */
  private readonly ceiling = computed(() => this.study().protocol.policy.max_drawdown_ceiling);
  /** Whose colour the bold line takes: the candidate's own, the recent fit's for its search, else the all-period fit's. */
  private readonly accent = computed<CandidateKey>(() => this.candidate() ?? (this.curves() === 'recent' ? 'recent' : 'all_period'));
  protected readonly spec = computed(
    () => {
      const set = this.set();
      return set === null || set.lines.length === 0 ? null : stepCurvesSpec(set.lines, this.ceiling(), this.accent());
    },
    { equal: sameChart },
  );

  /** Why a read set draws nothing: its runs failed, kept no curve, or have not run yet. */
  protected readonly nothingDrawn = computed(() => {
    const read = this.set();
    if (read === null || (read.failed === 0 && read.not_recorded === 0)) return this.empty();
    if (read.not_recorded === 0) return 'These runs failed, so they have no curve.';
    return read.failed === 0 ? 'These runs have no curve recorded.' : 'These runs have no curve: some failed, the rest kept none.';
  });

  constructor() {
    reloadOnProgress(() => setProgress(this.study(), this.curves()), this.lines);
  }
}

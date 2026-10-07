import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from '../golden-search/charts/golden-search-chart.component';
import { sameChart } from '../golden-search/charts/golden-search-chart-spec';
import { reloadOnProgress } from '../golden-search/charts/golden-search-reload';
import { isTerminal } from '../grid-search/grid-search.types';
import { foldCurvesSpec } from './walk-forward-fold-curves-chart';
import { WalkForwardStudyService } from './walk-forward-study.service';
import { winnerText, type WalkForwardStudyDetail } from './walk-forward-study.types';

/**
 * A study's out-of-sample record as curves (#2936): each completed fold's
 * winner over its test window, fold after fold on one date axis. Every fold
 * starts from the study's starting capital afresh, so each line starts flat
 * and none carries the last one's gain. The axis runs over every fold's test
 * window: a fold that did not complete leaves a gap, and a fold whose test run
 * kept no curve is named, never drawn flat.
 * The set is read again when another fold completes.
 */
@Component({
  selector: 'app-walk-forward-fold-curves',
  imports: [GoldenSearchChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="curves" aria-labelledby="walk-forward-fold-curves-title">
      <h3 id="walk-forward-fold-curves-title">Test-window equity, fold by fold</h3>
      @if (error(); as message) {
        <div role="alert" class="failed">
          <p>{{ message }}</p>
          <button type="button" class="link" (click)="curves.reload()">Try again</button>
        </div>
      } @else if (chart(); as spec) {
        <app-golden-search-chart [spec]="spec" [height]="340" />
      } @else if (nothingDrawn(); as message) {
        <p class="muted">{{ message }}</p>
      } @else {
        <p class="muted" role="status">Loading the curves…</p>
      }
      <p class="muted">
        Each fold’s winner over its test window at every session close, from the study’s starting capital afresh, and beneath, its fall from its own peak. A fold that did not complete leaves a gap.
        @if (unrecorded(); as folds) { No curve recorded for {{ folds }}. }
      </p>
    </section>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .curves { display: grid; gap: var(--space-2); }
    h3 { margin: 0; font-size: var(--fs-sm); }
    p { margin: 0; }
    .muted { color: var(--text-secondary); font-size: var(--fs-xs); }
    .failed { display: grid; gap: var(--space-1); color: var(--danger); font-size: var(--fs-sm); }
    .link { all: unset; justify-self: start; color: var(--accent-text); font-size: var(--fs-xs); cursor: pointer; }
    .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  `,
})
export class WalkForwardFoldCurvesComponent {
  readonly study = input.required<WalkForwardStudyDetail>();

  private readonly service = inject(WalkForwardStudyService);

  /** The read follows the study's id, not the study object a poll replaces. */
  private readonly studyId = computed(() => this.study().id);
  protected readonly curves = resource({
    params: () => ({ studyId: this.studyId() }),
    loader: ({ params }) => this.service.foldCurves(params.studyId),
  });
  protected readonly read = computed(() => (this.curves.hasValue() ? this.curves.value() : null));
  protected readonly error = computed(() => {
    const error = this.curves.error();
    return error === undefined ? null : extractServerMessage(error, 'The folds’ curves could not be loaded.');
  });
  protected readonly chart = computed(
    () => {
      const set = this.read();
      if (set === null) return null;
      const curves = new Map(set.curves.map((curve) => [curve.fold_index, curve] as const));
      const folds = this.study().folds.flatMap((fold) => {
        const curve = curves.get(fold.fold_index);
        return curve === undefined ? [] : [{ foldIndex: fold.fold_index, settings: winnerText(fold), curve }];
      });
      return folds.length === 0 ? null : foldCurvesSpec(set.sessions_ms, folds);
    },
    { equal: sameChart },
  );
  /** Why a read set draws nothing: its folds kept no curve, none completed, or none has run yet. */
  protected readonly nothingDrawn = computed(() => {
    const set = this.read();
    if (set === null) return null;
    if (set.not_recorded.length > 0) return 'No curve was recorded for this study’s folds.';
    return isTerminal(this.study().status) ? 'No fold completed, so there is no curve to draw.' : 'A fold’s curve appears here once its test window has run.';
  });
  /** The completed folds with no curve, named when some other fold is drawn; the empty state says it when none is. */
  protected readonly unrecorded = computed(() => {
    const set = this.read();
    if (set === null || set.curves.length === 0 || set.not_recorded.length === 0) return null;
    return set.not_recorded.map((index) => `fold ${index + 1}`).join(', ');
  });

  constructor() {
    reloadOnProgress(() => this.study().completed_folds, this.curves);
  }
}

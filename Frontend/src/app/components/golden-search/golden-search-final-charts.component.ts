import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { comparisonHeight, finalComparisonSpec, finalEquitySpec, finalMonthsSpec, type FinalRun } from './golden-search-final-charts';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateKey, ExamCheck, ExamView, StudyDetail } from './golden-search.types';

const CHECK_WORDS: Readonly<Record<ExamCheck['status'], string>> = { pass: 'Pass', fail: 'Fail', not_available: 'Not available' };
const CHECK_ICONS: Readonly<Record<ExamCheck['status'], string>> = { pass: 'pi-check-circle', fail: 'pi-times-circle', not_available: 'pi-minus-circle' };

/**
 * The final test's charts (#2821): its rule checks as a status list (V35),
 * each measure on the development period beside the final test (V33), and
 * the candidate's and the current settings' returns through the final test
 * (V34) and by month (V36), read from both runs' detail reads once the
 * final test has a result.
 */
@Component({
  selector: 'app-golden-search-final-charts',
  imports: [GoldenSearchChartComponent, GoldenSearchGridComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-golden-search-grid>
      <app-golden-search-panel chart="final-checks" data-span="6">
        @if (exam().checks.length > 0) {
          <ul class="checks" aria-label="Final-test checks">
            @for (check of exam().checks; track check.code) {
              <li [attr.data-status]="check.status">
                <i class="pi" [class]="icons[check.status]" aria-hidden="true"></i>
                <span><strong>{{ words[check.status] }}</strong> · {{ check.label }} — {{ check.detail }}</span>
              </li>
            }
          </ul>
        } @else {
          <p class="muted">No check was recorded for this final test.</p>
        }
        <p caption class="caption">Each rule the final test is judged by, in words. Retention is described beside the result, never checked.</p>
      </app-golden-search-panel>
      @if (error(); as message) {
        <div data-span="6" role="alert" class="failed">
          <p>{{ message }}</p>
          <button type="button" class="link" (click)="runs.reload()">Try again</button>
        </div>
      } @else if (comparison(); as spec) {
        <app-golden-search-panel chart="final-comparison" data-span="6">
          <app-golden-search-chart [spec]="spec" [height]="height()" />
          <p caption class="caption">Each measure on its own scale; annual rates over each window's own trading years.</p>
        </app-golden-search-panel>
        @if (equity(); as spec) {
          <app-golden-search-panel chart="final-equity" data-span="6">
            <app-golden-search-chart [spec]="spec" [height]="240" />
            <p caption class="caption">Each run from its fresh starting capital, at every session close of the final test.</p>
          </app-golden-search-panel>
        }
        @if (months(); as spec) {
          <app-golden-search-panel chart="final-months" data-span="6">
            <app-golden-search-chart [spec]="spec" [height]="220" />
            <p caption class="caption">A final result spread across months is steadier than one carried by a single month.</p>
          </app-golden-search-panel>
        }
      } @else {
        <p class="muted" data-span="6">Loading the final-test runs…</p>
      }
    </app-golden-search-grid>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    p { margin: 0; }
    .muted, .caption { color: var(--text-secondary); font-size: var(--fs-xs); }
    .checks { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; font-size: var(--fs-sm); }
    .checks li { display: flex; gap: var(--space-2); align-items: baseline; }
    .checks li[data-status='pass'] i { color: var(--bull); }
    .checks li[data-status='fail'] i { color: var(--bear); }
    .checks li[data-status='not_available'] i { color: var(--text-secondary); }
    .failed { display: grid; gap: var(--space-1); color: var(--bear); font-size: var(--fs-sm); }
    .link { all: unset; justify-self: start; color: var(--accent-text); font-size: var(--fs-xs); cursor: pointer; }
    .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  `,
})
export class GoldenSearchFinalChartsComponent {
  readonly study = input.required<StudyDetail>();
  readonly exam = input.required<ExamView>();
  readonly candidateLabel = input.required<string>();

  private readonly service = inject(GoldenSearchService);
  protected readonly words = CHECK_WORDS;
  protected readonly icons = CHECK_ICONS;

  /** The final test's two runs: the candidate's, then the current settings' (one when they are the same). */
  private readonly keys = computed<CandidateKey[]>(() => {
    const candidate = this.exam().candidate_key;
    return candidate === 'incumbent' ? ['incumbent'] : [candidate, 'incumbent'];
  });
  protected readonly runs = resource({
    params: () => ({ studyId: this.study().id, keys: this.keys() }),
    loader: async ({ params }) => Promise.all(params.keys.map(async (key) => ({ key, detail: await this.service.candidate(params.studyId, key) }))),
  });
  protected readonly error = computed(() => {
    const error = this.runs.error();
    return error === undefined ? null : extractServerMessage(error, 'The final-test runs could not be loaded.');
  });
  private readonly finalRuns = computed<FinalRun[] | null>(() => {
    if (!this.runs.hasValue()) return null;
    return this.runs.value().flatMap(({ key, detail }) =>
      detail.exam === null
        ? []
        : [{ key, label: key === 'incumbent' ? 'Current settings' : this.candidateLabel(), comparison: detail.exam.comparison ?? [], returns: detail.exam.cumulative_return, monthly: detail.exam.monthly }],
    );
  });
  protected readonly comparison = computed(() => {
    const runs = this.finalRuns();
    return runs === null || runs.length === 0 ? null : finalComparisonSpec(runs);
  }, { equal: sameChart });
  protected readonly equity = computed(() => {
    const runs = this.finalRuns();
    return runs === null || runs.every((run) => run.returns.length === 0) ? null : finalEquitySpec(runs);
  }, { equal: sameChart });
  protected readonly months = computed(() => {
    const runs = this.finalRuns();
    return runs === null || runs.every((run) => run.monthly.length === 0) ? null : finalMonthsSpec(runs);
  }, { equal: sameChart });
  protected readonly height = computed(() => comparisonHeight(this.finalRuns()?.[0]?.comparison.length ?? 0));
}

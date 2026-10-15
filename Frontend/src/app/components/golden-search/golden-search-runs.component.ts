import { ChangeDetectionStrategy, Component, computed, effect, inject, input, resource, signal, untracked } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { reloadOnProgress } from './charts/golden-search-reload';
import { curveStore } from './golden-search-curve-store';
import { percentText, ratioText, signedPercentText } from './golden-search-display';
import { runCurvesSpec, type RunLine } from './golden-search-run-curves-chart';
import { GoldenSearchService } from './golden-search.service';
import type { RunCurve, RunGroupKey, RunSort, RunsPage, RunsQuery, StudyDetail, StudyRun } from './golden-search.types';

const PAGE_SIZE = 100;
/** The most runs drawn at once: a page of them. */
const MAX_DRAWN = 100;

interface SortColumn {
  readonly key: Exclude<RunSort, 'recorded'>;
  readonly label: string;
  /** Which way the first click sorts: the better end first. */
  readonly bestFirstDescending: boolean;
}

const COLUMNS: readonly SortColumn[] = [
  { key: 'total_return_pct', label: 'Net return', bestFirstDescending: true },
  { key: 'sharpe_ratio', label: 'Sharpe', bestFirstDescending: true },
  { key: 'max_drawdown_pct', label: 'Worst fall', bestFirstDescending: false },
  { key: 'total_trades', label: 'Trades', bestFirstDescending: true },
];

interface RunRow {
  readonly run: StudyRun;
  readonly ticked: boolean;
  /** The run has a curve to draw: the listing says it kept one, and no read has found it not recorded. */
  readonly canDraw: boolean;
  readonly netReturn: string;
  readonly sharpe: string;
  readonly worstFall: string;
  /** Why the run has no numbers or cannot be drawn; null for a completed run with its curve. */
  readonly note: string | null;
}

/**
 * All runs (#2930): every engine run of the study under the step that first
 * asked for it — each search point, each fold's training and test runs, the
 * candidate, neighbor and stress runs, the final test's — with its settings,
 * window and numbers. The owner filters by what asked for the runs, sorts by
 * a measure, and ticks runs to draw their cumulative return and fall from
 * peak together; a fold group can be listed one fold at a time. A run with
 * no curve is listed with its numbers and says its curve was not recorded.
 * Ticks survive a change of filter, sort or page; the list is read again
 * when the study records more. The study page builds this anew for each
 * study, so nothing here outlives the study it was opened on.
 */
@Component({
  selector: 'app-golden-search-runs',
  imports: [ButtonModule, DecimalPipe, GoldenSearchChartComponent, GoldenSearchPanelComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-runs.component.html',
  styleUrl: './golden-search-runs.component.scss',
})
export class GoldenSearchRunsComponent {
  readonly study = input.required<StudyDetail>();

  private readonly service = inject(GoldenSearchService);

  protected readonly columns = COLUMNS;
  protected readonly maxDrawn = MAX_DRAWN;
  protected readonly group = signal<RunGroupKey | null>(null);
  /** One fold of a fold group; null for every fold. */
  protected readonly fold = signal<number | null>(null);
  protected readonly sort = signal<RunSort>('recorded');
  protected readonly descending = signal(false);
  protected readonly page = signal(1);
  /** The runs ticked, in the order ticked, kept across filters, sorts and pages. */
  protected readonly ticked = signal<readonly StudyRun[]>([]);

  private readonly studyId = computed(() => this.study().id);
  private readonly query = computed<RunsQuery>(() => ({
    group: this.group(),
    fold_index: this.fold(),
    sort: this.sort(),
    descending: this.descending(),
    page: this.page(),
    page_size: PAGE_SIZE,
  }));
  private readonly listing = resource({
    params: () => ({ studyId: this.studyId(), query: this.query() }),
    loader: ({ params }) => this.service.runs(params.studyId, params.query),
  });
  /** The page last read and the filter, sort and page it answers; it stays shown while the next one loads. */
  private readonly shown = signal<{ readonly page: RunsPage; readonly query: RunsQuery } | null>(null);
  protected readonly runs = computed(() => this.shown()?.page ?? null);
  protected readonly listError = computed(() => {
    const error = this.listing.error();
    return error === undefined ? null : extractServerMessage(error, 'The study’s runs could not be loaded.');
  });
  /** The read for the controls now set failed, so the page last read answers other controls: its rows are not shown. */
  protected readonly mismatched = computed(() => this.listError() !== null && this.shown()?.query !== this.query());

  private readonly curves = curveStore<RunCurve>({
    scope: this.studyId,
    keys: () => this.ticked().map((run) => run.evaluation_key),
    read: (studyId, keys) => this.service.runCurves(studyId, keys),
    keyOf: (curve) => curve.evaluation_key,
  });
  protected readonly curvesError = computed(() => {
    const error = this.curves.reads.error();
    return error === undefined ? null : extractServerMessage(error, 'The ticked runs’ curves could not be loaded.');
  });
  /** The ticked runs whose curves are read, in the order ticked. */
  private readonly lines = computed<RunLine[]>(() =>
    this.ticked().flatMap((run) => {
      const held = this.curves.of(run.evaluation_key);
      return held === undefined || held === null ? [] : [{ key: run.evaluation_key, label: run.label, settings: run.params_sentence, ...held }];
    }),
  );
  /** Read on its own, so a study poll that changes nothing else does not rebuild the chart. */
  private readonly ceiling = computed(() => this.study().protocol.policy.max_drawdown_ceiling);
  protected readonly chart = computed(
    () => {
      const lines = this.lines();
      return lines.length === 0 ? null : runCurvesSpec(lines, this.ceiling());
    },
    { equal: sameChart },
  );
  protected readonly loadingCurves = computed(() => this.curves.reads.isLoading());

  private readonly tickedKeys = computed(() => new Set(this.ticked().map((run) => run.evaluation_key)));
  protected readonly full = computed(() => this.ticked().length >= MAX_DRAWN);
  protected readonly rows = computed<RunRow[]>(() =>
    (this.mismatched() ? [] : (this.runs()?.rows ?? [])).map((run) => ({
      run,
      ticked: this.tickedKeys().has(run.evaluation_key),
      canDraw: this.canDraw(run),
      netReturn: signedPercentText(run.total_return_pct),
      sharpe: ratioText(run.sharpe_ratio),
      worstFall: percentText(run.max_drawdown_pct),
      note: run.status === 'failed' ? `Run failed — ${run.error ?? 'no reason recorded'}` : run.status === 'pending' ? 'Still running' : this.canDraw(run) ? null : 'Curve not recorded',
    })),
  );
  protected readonly totalRuns = computed(() => (this.runs()?.groups ?? []).reduce((sum, group) => sum + group.runs, 0));
  /** Completed runs with no curve to draw. A failed run never had one to keep. */
  protected readonly withoutCurve = computed(() => (this.runs()?.groups ?? []).reduce((sum, group) => sum + group.completed - group.with_curve, 0));
  /** The folds to choose between, when the group shown is one of the fold groups. */
  protected readonly folds = computed(() => {
    const group = this.group();
    const count = group === 'fold_training' || group === 'fold_test' ? (this.runs()?.folds ?? 0) : 0;
    return Array.from({ length: count }, (_, index) => index);
  });
  protected readonly pages = computed(() => Math.max(1, Math.ceil((this.runs()?.total ?? 0) / PAGE_SIZE)));
  protected readonly firstShown = computed(() => (this.runs()?.total ? (this.page() - 1) * PAGE_SIZE + 1 : 0));
  protected readonly lastShown = computed(() => Math.min(this.page() * PAGE_SIZE, this.runs()?.total ?? 0));
  /** The runs on this page that can still be ticked. */
  protected readonly drawable = computed(() => this.rows().filter((row) => row.canDraw && !row.ticked).length);

  constructor() {
    effect(() => {
      if (this.listing.hasValue()) this.shown.set({ page: this.listing.value(), query: untracked(this.query) });
    });
    // A tick whose curve the read found not recorded draws nothing: it leaves the ticks rather than count as drawn.
    effect(() => {
      const missing = this.ticked().filter((run) => !this.canDraw(run));
      if (missing.length > 0) untracked(() => this.ticked.update((ticked) => ticked.filter((run) => !missing.includes(run))));
    });
    reloadOnProgress(() => `${this.study().revision}|${this.study().consumed_evaluations}`, this.listing);
  }

  /** The listing's `has_curve` is its word at that read; the curve read that follows can still find the run's curve not recorded. */
  private canDraw(run: StudyRun): boolean {
    return run.has_curve && this.curves.of(run.evaluation_key) !== null;
  }

  protected chooseGroup(group: RunGroupKey | null): void {
    this.group.set(group);
    this.fold.set(null);
    this.page.set(1);
  }

  protected chooseFold(fold: number | null): void {
    this.fold.set(fold);
    this.page.set(1);
  }

  /** A measure's first click puts its better end first; a second turns it round. */
  protected sortBy(column: SortColumn): void {
    if (this.sort() === column.key) this.descending.update((descending) => !descending);
    else {
      this.sort.set(column.key);
      this.descending.set(column.bestFirstDescending);
    }
    this.page.set(1);
  }

  protected recordedOrder(): void {
    this.sort.set('recorded');
    this.descending.set(false);
    this.page.set(1);
  }

  protected ariaSort(key: RunSort): 'ascending' | 'descending' | 'none' {
    if (this.sort() !== key) return 'none';
    return this.descending() ? 'descending' : 'ascending';
  }

  protected toggle(run: StudyRun): void {
    if (this.tickedKeys().has(run.evaluation_key)) this.ticked.update((ticked) => ticked.filter((other) => other.evaluation_key !== run.evaluation_key));
    else if (this.canDraw(run) && !this.full()) this.ticked.update((ticked) => [...ticked, run]);
  }

  /** Ticks every run on this page that has a curve, up to the most that can be drawn. */
  protected drawPage(): void {
    const room = MAX_DRAWN - this.ticked().length;
    const more = this.rows().filter((row) => row.canDraw && !row.ticked).slice(0, room).map((row) => row.run);
    if (more.length > 0) this.ticked.update((ticked) => [...ticked, ...more]);
  }

  protected clear(): void {
    this.ticked.set([]);
  }

  protected turn(by: number): void {
    this.page.update((page) => Math.min(this.pages(), Math.max(1, page + by)));
  }

  protected retry(): void {
    this.listing.reload();
  }

  protected retryCurves(): void {
    this.curves.reads.reload();
  }
}

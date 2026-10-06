import { ChangeDetectionStrategy, Component, computed, effect, inject, input, resource, signal, untracked } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { reloadOnProgress } from './charts/golden-search-reload';
import { percentText, ratioText, signedPercentText } from './golden-search-display';
import { runCurvesSpec, type RunLine } from './golden-search-run-curves-chart';
import { GoldenSearchService } from './golden-search.service';
import type { RunCurve, RunGroupKey, RunSort, RunsPage, RunsQuery, StudyDetail, StudyRun } from './golden-search.types';

const PAGE_SIZE = 100;
/** The most runs drawn at once: a page of them. */
const MAX_DRAWN = 100;

interface SortColumn {
  readonly key: Exclude<RunSort, 'recorded' | 'net_profit'>;
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
 * peak together. A run recorded before runs kept their curve is listed with
 * its numbers and says its curve was not recorded. Ticks survive a change of
 * filter, sort or page; the list is read again when the study records more.
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
  protected readonly sort = signal<RunSort>('recorded');
  protected readonly descending = signal(false);
  protected readonly page = signal(1);
  /** The runs ticked, in the order ticked, kept across filters, sorts and pages. */
  protected readonly ticked = signal<readonly StudyRun[]>([]);

  private readonly studyId = computed(() => this.study().id);
  private readonly query = computed<RunsQuery>(() => ({ group: this.group(), sort: this.sort(), descending: this.descending(), page: this.page(), page_size: PAGE_SIZE }));
  private readonly listing = resource({
    params: () => ({ studyId: this.studyId(), query: this.query() }),
    loader: ({ params }) => this.service.runs(params.studyId, params.query),
  });
  /** The page last read; it stays shown while the next one loads. */
  private readonly shown = signal<RunsPage | null>(null);
  protected readonly runs = this.shown.asReadonly();
  protected readonly listError = computed(() => {
    const error = this.listing.error();
    return error === undefined ? null : extractServerMessage(error, 'The study’s runs could not be loaded.');
  });

  /** Each read curve by run; null for a run that kept none. Curves never change once recorded. */
  private readonly cache = new Map<string, RunCurve | null>();
  private readonly curves = resource({
    // Nothing ticked reads nothing.
    params: () => (this.ticked().length === 0 ? undefined : { studyId: this.studyId(), keys: this.ticked().map((run) => run.evaluation_key) }),
    loader: async ({ params }) => {
      const missing = params.keys.filter((key) => !this.cache.has(key));
      if (missing.length > 0) {
        const read = await this.service.runCurves(params.studyId, missing);
        for (const curve of read.curves) this.cache.set(curve.evaluation_key, curve);
        for (const key of read.not_recorded) this.cache.set(key, null);
      }
      return params.keys.length;
    },
  });
  protected readonly curvesError = computed(() => {
    const error = this.curves.error();
    return error === undefined ? null : extractServerMessage(error, 'The ticked runs’ curves could not be loaded.');
  });
  /** The ticked runs whose curves are read, in the order ticked. */
  private readonly lines = computed<RunLine[]>(() => {
    // Read again once a load fills the cache.
    this.curves.value();
    return this.ticked().flatMap((run) => {
      const curve = this.cache.get(run.evaluation_key);
      return curve === undefined || curve === null ? [] : [{ key: run.evaluation_key, label: run.label, curve }];
    });
  });
  protected readonly chart = computed(
    () => {
      const lines = this.lines();
      return lines.length === 0 ? null : runCurvesSpec(lines, this.study().protocol.policy.max_drawdown_ceiling);
    },
    { equal: sameChart },
  );
  protected readonly loadingCurves = computed(() => this.curves.isLoading());

  private readonly tickedKeys = computed(() => new Set(this.ticked().map((run) => run.evaluation_key)));
  protected readonly full = computed(() => this.ticked().length >= MAX_DRAWN);
  protected readonly rows = computed<RunRow[]>(() =>
    (this.runs()?.rows ?? []).map((run) => ({
      run,
      ticked: this.tickedKeys().has(run.evaluation_key),
      netReturn: signedPercentText(run.total_return_pct),
      sharpe: ratioText(run.sharpe_ratio),
      worstFall: percentText(run.max_drawdown_pct),
      note: run.status === 'failed' ? `Run failed — ${run.error ?? 'no reason recorded'}` : run.status === 'pending' ? 'Still running' : run.has_curve ? null : 'Curve not recorded',
    })),
  );
  protected readonly totalRuns = computed(() => (this.runs()?.groups ?? []).reduce((sum, group) => sum + group.runs, 0));
  /** Completed runs with no curve: the ones recorded before runs kept it. A failed run never had one to keep. */
  protected readonly withoutCurve = computed(() => (this.runs()?.groups ?? []).reduce((sum, group) => sum + group.completed - group.with_curve, 0));
  protected readonly pages = computed(() => Math.max(1, Math.ceil((this.runs()?.total ?? 0) / PAGE_SIZE)));
  protected readonly firstShown = computed(() => (this.runs()?.total ? (this.page() - 1) * PAGE_SIZE + 1 : 0));
  protected readonly lastShown = computed(() => Math.min(this.page() * PAGE_SIZE, this.runs()?.total ?? 0));
  /** The runs on this page that can still be ticked. */
  protected readonly drawable = computed(() => this.rows().filter((row) => row.run.has_curve && !row.ticked).length);

  constructor() {
    // Another study starts from the top: its own groups, no ticks, no curves of the last one.
    effect(() => {
      this.studyId();
      untracked(() => {
        this.cache.clear();
        this.shown.set(null);
        this.ticked.set([]);
        this.group.set(null);
        this.sort.set('recorded');
        this.descending.set(false);
        this.page.set(1);
      });
    });
    effect(() => {
      if (this.listing.hasValue()) this.shown.set(this.listing.value());
    });
    reloadOnProgress(() => `${this.study().revision}|${this.study().consumed_evaluations}`, this.listing);
  }

  protected chooseGroup(group: RunGroupKey | null): void {
    this.group.set(group);
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
    else if (run.has_curve && !this.full()) this.ticked.update((ticked) => [...ticked, run]);
  }

  /** Ticks every run on this page that has a curve, up to the most that can be drawn. */
  protected drawPage(): void {
    const room = MAX_DRAWN - this.ticked().length;
    const more = this.rows().filter((row) => row.run.has_curve && !row.ticked).slice(0, room).map((row) => row.run);
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
    this.curves.reload();
  }
}

import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from '../golden-search/charts/golden-search-chart.component';
import { sameChart } from '../golden-search/charts/golden-search-chart-spec';
import { runCurvesSpec, type RunLine } from '../golden-search/golden-search-run-curves-chart';
import { GridSearchService } from './grid-search.service';
import type { GridSearchCellCurve } from './grid-search.types';

/** One cell to draw: what it is among the others (empty for a plain cell) and its settings in words. */
export interface DrawnCell {
  readonly params_hash: string;
  readonly label: string;
  readonly settings: string;
}

interface HeldCurve {
  readonly sessions: readonly number[];
  readonly curve: GridSearchCellCurve;
}

const CELLS = { one: 'cell', many: 'cells' } as const;

/**
 * The equity curves of a search's ticked cells (#2935), drawn together on the
 * session chart Golden Search's All runs page uses: each cell's cumulative
 * return on the search's starting capital, and beneath, its fall from its own
 * peak. It reads each curve once and keeps it, since a recorded curve never
 * changes. A cell with no curve recorded is never drawn flat: it has no line.
 */
@Component({
  selector: 'app-grid-search-curves',
  imports: [ButtonModule, GoldenSearchChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="curves" aria-labelledby="grid-search-curves-title">
      <header>
        <h3 id="grid-search-curves-title">Equity curves</h3>
        <span class="muted">{{ cells().length }} of at most {{ max() }} drawn</span>
        <button pButton type="button" size="small" severity="secondary" [outlined]="true" [disabled]="drawableOnPage() === 0 || cells().length >= max()" (click)="drawPage.emit()"><span>Draw every cell on this page</span></button>
        <button pButton type="button" size="small" severity="secondary" [text]="true" [disabled]="cells().length === 0" (click)="cleared.emit()"><span>Clear</span></button>
      </header>
      @if (error(); as message) {
        <div role="alert" class="failed">
          <p>{{ message }}</p>
          <button type="button" class="link" (click)="curves.reload()">Try again</button>
        </div>
      } @else if (chart(); as spec) {
        <app-golden-search-chart [spec]="spec" [height]="380" />
      } @else if (curves.isLoading()) {
        <p class="muted" role="status">Loading the curves…</p>
      } @else if (cells().length > 0) {
        <p class="muted">Curve not recorded for {{ cells().length === 1 ? 'this cell' : 'these cells' }}.</p>
      } @else {
        <p class="muted">Tick cells in the table below to draw them here.</p>
      }
      <p class="muted">Each cell’s return on the search’s starting capital at every session close, and beneath, its fall from its own peak.</p>
    </section>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .curves { display: grid; gap: var(--space-2); }
    header { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
    h3 { margin: 0; margin-inline-end: auto; font-size: var(--fs-sm); }
    p { margin: 0; }
    .muted { color: var(--text-secondary); font-size: var(--fs-xs); }
    .failed { display: grid; gap: var(--space-1); color: var(--danger); font-size: var(--fs-sm); }
    .link { all: unset; justify-self: start; color: var(--accent-text); font-size: var(--fs-xs); cursor: pointer; }
    .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  `,
})
export class GridSearchCurvesComponent {
  readonly searchId = input.required<string>();
  /** The cells to draw, in the order ticked. */
  readonly cells = input.required<readonly DrawnCell[]>();
  /** The most cells drawn at once. */
  readonly max = input.required<number>();
  /** Cells on the table's page that could still be ticked. */
  readonly drawableOnPage = input(0);
  readonly drawPage = output();
  readonly cleared = output();

  private readonly service = inject(GridSearchService);

  /**
   * Each read curve with the session closes of its read, by search and cell; null for a cell that kept none.
   * A walk-forward fold's training and test sweeps hold the same cells, so the search is part of the key.
   */
  private readonly cache = signal<ReadonlyMap<string, HeldCurve | null>>(new Map());
  protected readonly curves = resource({
    // Nothing ticked reads nothing.
    params: () => (this.cells().length === 0 ? undefined : { searchId: this.searchId(), hashes: this.cells().map((cell) => cell.params_hash) }),
    loader: async ({ params }) => {
      const held = this.cache();
      const missing = params.hashes.filter((hash) => !held.has(keyOf(params.searchId, hash)));
      if (missing.length === 0) return;
      const read = await this.service.cellCurves(params.searchId, missing);
      const kept = read.curves.map((curve) => [keyOf(params.searchId, curve.params_hash), { sessions: read.sessions_ms, curve }] as const);
      this.cache.update((cache) => new Map([...cache, ...kept, ...read.not_recorded.map((hash) => [keyOf(params.searchId, hash), null] as const)]));
    },
  });
  protected readonly error = computed(() => {
    const error = this.curves.error();
    return error === undefined ? null : extractServerMessage(error, 'The curves could not be loaded.');
  });
  private readonly lines = computed<RunLine[]>(() => {
    const cache = this.cache();
    const searchId = this.searchId();
    return this.cells().flatMap((cell) => {
      const held = cache.get(keyOf(searchId, cell.params_hash));
      return held === undefined || held === null ? [] : [{ key: cell.params_hash, label: cell.label, settings: cell.settings, ...held }];
    });
  });
  protected readonly chart = computed(
    () => {
      const lines = this.lines();
      return lines.length === 0 ? null : runCurvesSpec(lines, null, CELLS);
    },
    { equal: sameChart },
  );
}

function keyOf(searchId: string, paramsHash: string): string {
  return `${searchId}\n${paramsHash}`;
}

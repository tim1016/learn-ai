import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from '../golden-search/charts/golden-search-chart.component';
import { sameChart } from '../golden-search/charts/golden-search-chart-spec';
import { curveStore } from '../golden-search/golden-search-curve-store';
import { runCurvesSpec, type RunLine } from '../golden-search/golden-search-run-curves-chart';
import { GridSearchService } from './grid-search.service';
import type { GridSearchCellCurve } from './grid-search.types';

/** One cell to draw: what it is among the others (null for a plain cell) and its settings in words. */
export interface DrawnCell {
  readonly params_hash: string;
  readonly label: string | null;
  readonly settings: string;
}

const CELLS = { one: 'cell', many: 'cells' } as const;

/**
 * The equity curves of a search's ticked cells (#2935), drawn together on the
 * session chart Golden Search's All runs page uses: each cell's cumulative
 * return on the search's starting capital, and beneath, its fall from its own
 * peak. A cell with no curve recorded is never drawn flat: it has no line,
 * and it is not counted among the drawn.
 */
@Component({
  selector: 'app-grid-search-curves',
  imports: [ButtonModule, GoldenSearchChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="curves" aria-labelledby="grid-search-curves-title">
      <header>
        <h3 id="grid-search-curves-title">Equity curves</h3>
        <span class="muted">{{ lines().length }} of at most {{ max() }} drawn</span>
        <button pButton type="button" size="small" severity="secondary" [outlined]="true" [disabled]="drawableOnPage() === 0 || cells().length >= max()" (click)="drawPage.emit()"><span>Draw every cell on this page</span></button>
        <button pButton type="button" size="small" severity="secondary" [text]="true" [disabled]="cells().length === 0" (click)="cleared.emit()"><span>Clear</span></button>
      </header>
      @if (error(); as message) {
        <div role="alert" class="failed">
          <p>{{ message }}</p>
          <button type="button" class="link" (click)="curves.reads.reload()">Try again</button>
        </div>
      } @else if (chart(); as spec) {
        <app-golden-search-chart [spec]="spec" [height]="380" />
      } @else if (curves.reads.isLoading()) {
        <p class="muted" role="status">Loading the curves…</p>
      } @else if (cells().length > 0) {
        <p class="muted">No curve was recorded for {{ unrecorded() }}.</p>
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

  protected readonly curves = curveStore<GridSearchCellCurve>({
    scope: this.searchId,
    keys: () => this.cells().map((cell) => cell.params_hash),
    read: (searchId, hashes) => this.service.cellCurves(searchId, hashes),
    keyOf: (curve) => curve.params_hash,
  });
  protected readonly error = computed(() => {
    const error = this.curves.reads.error();
    return error === undefined ? null : extractServerMessage(error, 'The curves could not be loaded.');
  });
  /** The ticked cells whose curves are read, in the order ticked. */
  protected readonly lines = computed<RunLine[]>(() =>
    this.cells().flatMap((cell) => {
      const held = this.curves.of(cell.params_hash);
      return held === undefined || held === null ? [] : [{ key: cell.params_hash, label: cell.label, settings: cell.settings, ...held }];
    }),
  );
  /** What to call the ticked cells when none of them kept a curve: the leader by its name, since the owner did not tick it. */
  protected readonly unrecorded = computed(() => {
    const cells = this.cells();
    if (cells.length !== 1) return 'these cells';
    return cells[0].label === null ? 'this cell' : `the ${cells[0].label.toLowerCase()}`;
  });
  protected readonly chart = computed(
    () => {
      const lines = this.lines();
      return lines.length === 0 ? null : runCurvesSpec(lines, null, CELLS);
    },
    { equal: sameChart },
  );
}

import { ChangeDetectionStrategy, Component, computed, ElementRef, inject, input, signal } from '@angular/core';

import { cellDetail, initialCell, pairMapView, type PairCellView, type PairMapView } from './golden-search-compare';
import type { PairMap, Point, StrategyCapability } from './golden-search.types';

const ARROW_STEPS: Readonly<Record<string, readonly [number, number]>> = {
  ArrowUp: [-1, 0],
  ArrowDown: [1, 0],
  ArrowLeft: [0, -1],
  ArrowRight: [0, 1],
};

/**
 * The parameter map (#2696): a two-knob landscape of development net return
 * through one candidate, every other setting held at that candidate's value.
 * Tested cells are buttons (Tab or the arrow keys move between them; the
 * pressed one is described below the map), invalid pairs read "—" and are
 * named as invalid, and the same values follow as a table. A deeper fill is a
 * higher return — the numbers themselves are the server's. It shows a slice,
 * never robustness.
 */
@Component({
  selector: 'app-golden-search-pair-map',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-pair-map.component.html',
  styleUrl: './golden-search-pair-map.component.scss',
})
export class GoldenSearchPairMapComponent {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly maps = input.required<readonly PairMap[]>();
  readonly capability = input<StrategyCapability | null>(null);
  /** The settings the maps are drawn through. */
  readonly center = input<Point | null>(null);
  /** Who `center` is, e.g. "the All-period fit". */
  readonly centerLabel = input.required<string>();
  /** Unique per page; ties the section to its heading. */
  readonly headingId = input.required<string>();

  private readonly chosenMap = signal(0);
  private readonly chosenCell = signal<string | null>(null);

  protected readonly views = computed<PairMapView[]>(() => this.maps().map((map) => pairMapView(map, this.capability(), this.center())));
  protected readonly view = computed<PairMapView | null>(() => {
    const views = this.views();
    return views[Math.min(this.chosenMap(), views.length - 1)] ?? null;
  });
  protected readonly columns = computed(() => `auto repeat(${this.view()?.xValues.length ?? 0}, minmax(0, 1fr))`);
  protected readonly selected = computed<PairCellView | null>(() => {
    const view = this.view();
    if (view === null) return null;
    const key = this.chosenCell();
    return view.rows.flat().find((cell) => cell.key === key) ?? initialCell(view);
  });
  protected readonly detail = computed(() => {
    const view = this.view();
    const cell = this.selected();
    return view === null || cell === null ? '' : cellDetail(view, cell);
  });

  protected chooseMap(index: number): void {
    this.chosenMap.set(index);
    this.chosenCell.set(null);
  }

  protected select(cell: PairCellView): void {
    this.chosenCell.set(cell.key);
  }

  /** Arrow keys move focus to the nearest selectable cell in that direction. */
  protected onKey(event: KeyboardEvent): void {
    const step = ARROW_STEPS[event.key];
    const target = event.target;
    if (step === undefined || !(target instanceof HTMLElement)) return;
    let row = Number(target.dataset['row']);
    let column = Number(target.dataset['column']);
    if (!Number.isInteger(row) || !Number.isInteger(column)) return;
    event.preventDefault();
    for (;;) {
      row += step[0];
      column += step[1];
      const next = this.host.nativeElement.querySelector<HTMLButtonElement>(`button[data-row="${row}"][data-column="${column}"]`);
      if (next !== null) {
        next.focus();
        return;
      }
      if (this.host.nativeElement.querySelector(`[data-row="${row}"][data-column="${column}"]`) === null) return;
    }
  }
}

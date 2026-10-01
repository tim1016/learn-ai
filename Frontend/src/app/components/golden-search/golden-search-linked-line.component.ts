import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { LinkedFoldReturn } from './golden-search.types';

const WIDTH = 320;
const HEIGHT = 80;
const PAD = 6;

interface Plot {
  readonly segments: readonly string[];
  readonly benchmarkSegments: readonly string[];
  readonly dots: readonly { key: number; x: number; y: number }[];
  readonly zeroY: number;
}

/** SVG path data for each unbroken run of values; a null ends the run and nothing joins across it. */
function segmentsOf(values: readonly (number | null)[], x: (i: number) => number, y: (v: number) => number): string[] {
  const segments: string[] = [];
  let current: string[] = [];
  values.forEach((value, i) => {
    if (value === null) {
      if (current.length > 1) segments.push(current.join(' '));
      current = [];
      return;
    }
    current.push(`${current.length === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(value).toFixed(1)}`);
  });
  if (current.length > 1) segments.push(current.join(' '));
  return segments;
}

/**
 * A sparkline of the server's linked fold returns (#2696): it only places
 * the given values on screen, with the frozen incumbent's linked returns on
 * the same folds as a dashed benchmark. A missing fold breaks a line —
 * nothing is interpolated across it. The host renders the same values as a
 * table, which is the accessible alternative.
 */
@Component({
  selector: 'app-golden-search-linked-line',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-linked-line.component.html',
  styleUrl: './golden-search-linked-line.component.scss',
})
export class GoldenSearchLinkedLineComponent {
  readonly points = input.required<readonly LinkedFoldReturn[]>();
  /** The incumbent's linked returns, matched to `points` by fold. */
  readonly benchmark = input<readonly LinkedFoldReturn[]>([]);

  protected readonly width = WIDTH;
  protected readonly height = HEIGHT;
  protected readonly plot = computed<Plot | null>(() => {
    const points = this.points();
    const values = points.map((p) => p.linked_return);
    const byFold = new Map(this.benchmark().map((p) => [p.fold_index, p.linked_return]));
    const benchmark = points.map((p) => byFold.get(p.fold_index) ?? null);
    const defined = [...values, ...benchmark].filter((v): v is number => v !== null);
    if (defined.length === 0) return null;
    const top = Math.max(0, ...defined);
    const bottom = Math.min(0, ...defined);
    const span = top - bottom || 1;
    const x = (i: number): number => (points.length === 1 ? WIDTH / 2 : PAD + (i * (WIDTH - 2 * PAD)) / (points.length - 1));
    const y = (v: number): number => PAD + ((top - v) / span) * (HEIGHT - 2 * PAD);
    const dots = points.flatMap((p, i) => (p.linked_return === null ? [] : [{ key: p.fold_index, x: x(i), y: y(p.linked_return) }]));
    return { segments: segmentsOf(values, x, y), benchmarkSegments: segmentsOf(benchmark, x, y), dots, zeroY: y(0) };
  });
}

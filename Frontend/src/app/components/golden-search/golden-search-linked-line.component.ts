import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { LinkedFoldReturn } from './golden-search.types';

const WIDTH = 320;
const HEIGHT = 80;
const PAD = 6;

interface Plot {
  readonly segments: readonly string[];
  readonly dots: readonly { key: number; x: number; y: number }[];
  readonly zeroY: number;
}

/**
 * A sparkline of the server's linked fold returns (#2696): it only places
 * the given values on screen. A missing fold breaks the line — nothing is
 * interpolated across it. The host renders the same values as a table, which
 * is the accessible alternative.
 */
@Component({
  selector: 'app-golden-search-linked-line',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-linked-line.component.html',
  styleUrl: './golden-search-linked-line.component.scss',
})
export class GoldenSearchLinkedLineComponent {
  readonly points = input.required<readonly LinkedFoldReturn[]>();

  protected readonly width = WIDTH;
  protected readonly height = HEIGHT;
  protected readonly plot = computed<Plot | null>(() => {
    const points = this.points();
    const defined = points.flatMap((p) => (p.linked_return === null ? [] : [p.linked_return]));
    if (defined.length === 0) return null;
    const top = Math.max(0, ...defined);
    const bottom = Math.min(0, ...defined);
    const span = top - bottom || 1;
    const x = (i: number): number => (points.length === 1 ? WIDTH / 2 : PAD + (i * (WIDTH - 2 * PAD)) / (points.length - 1));
    const y = (v: number): number => PAD + ((top - v) / span) * (HEIGHT - 2 * PAD);
    const segments: string[] = [];
    let current: string[] = [];
    points.forEach((p, i) => {
      if (p.linked_return === null) {
        if (current.length > 1) segments.push(current.join(' '));
        current = [];
        return;
      }
      current.push(`${current.length === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.linked_return).toFixed(1)}`);
    });
    if (current.length > 1) segments.push(current.join(' '));
    const dots = points.flatMap((p, i) => (p.linked_return === null ? [] : [{ key: p.fold_index, x: x(i), y: y(p.linked_return) }]));
    return { segments, dots, zeroY: y(0) };
  });
}

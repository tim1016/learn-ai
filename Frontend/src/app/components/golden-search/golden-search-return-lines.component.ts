import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { TimestampDisplayPipe } from '../../shared/timestamp';
import { signedPercentText } from './golden-search-display';
import type { CumulativeReturnPoint } from './golden-search.types';

export type ReturnLineTone = 'primary' | 'secondary' | 'benchmark';

export interface ReturnLineSeries {
  readonly key: string;
  readonly label: string;
  readonly tone: ReturnLineTone;
  readonly points: readonly CumulativeReturnPoint[];
}

const WIDTH = 640;
const HEIGHT = 220;
const LEFT = 52;
const RIGHT = 12;
const TOP = 12;
const BOTTOM = 190;
const TICKS = 4;

interface Plot {
  readonly lines: readonly { key: string; tone: ReturnLineTone; d: string }[];
  readonly ticks: readonly { y: number; label: string }[];
  readonly firstMs: number;
  readonly lastMs: number;
}

interface TableRow {
  readonly ms: number;
  readonly values: readonly string[];
}

/**
 * Cumulative development return of each candidate on one chart (#2696): the
 * server's daily values placed on screen, nothing interpolated or smoothed;
 * the incumbent is the dashed benchmark. The legend names every line, and
 * the daily values are offered as a table, the accessible alternative.
 */
@Component({
  selector: 'app-golden-search-return-lines',
  imports: [TimestampDisplayPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-return-lines.component.html',
  styleUrl: './golden-search-return-lines.component.scss',
})
export class GoldenSearchReturnLinesComponent {
  readonly series = input.required<readonly ReturnLineSeries[]>();
  /** Names the chart for assistive technology, e.g. "Development cumulative return". */
  readonly chartLabel = input.required<string>();

  protected readonly width = WIDTH;
  protected readonly height = HEIGHT;
  protected readonly left = LEFT;
  protected readonly right = WIDTH - RIGHT;
  protected readonly showTable = signal(false);

  protected readonly plot = computed<Plot | null>(() => {
    const series = this.series().filter((s) => s.points.length > 0);
    const all = series.flatMap((s) => s.points);
    if (all.length === 0) return null;
    const firstMs = Math.min(...all.map((p) => p.ms));
    const lastMs = Math.max(...all.map((p) => p.ms));
    const top = Math.max(0, ...all.map((p) => p.value));
    const bottom = Math.min(0, ...all.map((p) => p.value));
    const span = top - bottom || 1;
    const x = (ms: number): number => (lastMs === firstMs ? (LEFT + WIDTH - RIGHT) / 2 : LEFT + ((ms - firstMs) / (lastMs - firstMs)) * (WIDTH - RIGHT - LEFT));
    const y = (v: number): number => TOP + ((top - v) / span) * (BOTTOM - TOP);
    const lines = series.map((s) => ({
      key: s.key,
      tone: s.tone,
      d: s.points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.ms).toFixed(1)},${y(p.value).toFixed(1)}`).join(' '),
    }));
    const ticks = Array.from({ length: TICKS }, (_, i) => {
      const value = bottom + (span * i) / (TICKS - 1);
      return { y: y(value), label: signedPercentText(value) };
    });
    return { lines, ticks, firstMs, lastMs };
  });

  /** What the chart ends on, in words: each line's last value as the server sent it. */
  protected readonly summary = computed(() =>
    this.series()
      .filter((s) => s.points.length > 0)
      .map((s) => `${s.label} ${signedPercentText(s.points[s.points.length - 1].value)}`)
      .join(', '),
  );

  /** Every day any line has a value, each line's value that day (— where it has none). */
  protected readonly table = computed<TableRow[]>(() => {
    const series = this.series();
    const byLine = series.map((s) => new Map(s.points.map((p) => [p.ms, p.value])));
    const days = [...new Set(series.flatMap((s) => s.points.map((p) => p.ms)))].sort((a, b) => a - b);
    return days.map((ms) => ({ ms, values: byLine.map((line) => signedPercentText(line.get(ms))) }));
  });

  protected toggleTable(): void {
    this.showTable.update((shown) => !shown);
  }
}

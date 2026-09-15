import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';

import { etIsoDate } from '../../../../shared/date/et-midnight';
import { DayReturns, HistogramBin, ReturnKind } from '../returns-distribution.service';

/** The five session segments, in chronological order. */
const SEGMENTS: readonly { key: keyof DayReturns; label: string; hint: string }[] = [
  { key: 'overnightPct', label: 'Overnight gap', hint: 'Previous close → today’s open' },
  { key: 'preMarketPct', label: 'Pre-market', hint: 'First extended bar → the open' },
  { key: 'morningPct', label: 'Morning', hint: 'Open → 12:00 ET' },
  { key: 'afternoonPct', label: 'Afternoon', hint: '12:00 ET → close' },
  { key: 'afterHoursPct', label: 'After hours', hint: 'Close → last extended bar' },
];

interface SegmentView {
  label: string;
  hint: string;
  valuePct: number | null;
  /** Width as a share of the row scale — a pure rendering mapping of the
   * Python-provided percentage, never a new number a user compares. */
  widthShare: number;
  negative: boolean;
}

interface DayView {
  etDate: string;
  sessionOpenMsUtc: number;
  dayPct: number | null;
  volume: number;
  segments: readonly SegmentView[];
}

/** Field routing for display only: which Python-computed percentage the
 * active return kind labels. Bin *membership* is never derived here — the
 * days are selected by Python's stamped ``binIndices``. */
function valueForKind(day: DayReturns, kind: ReturnKind): number | null {
  if (kind === 'close_to_close') return day.closeToClosePct;
  if (kind === 'session') return day.sessionPct;
  return day.overnightPct;
}

/**
 * The clicked basket, opened up: every date Python stamped into it, each
 * with the full 24-hour session breakdown (overnight gap, pre-market,
 * morning, afternoon, after-hours). Segment bars are center-split: red
 * grows left, green grows right, width proportional to |segment| against
 * the widest segment shown. Clicking a date emits its session anchor for
 * the candle pane.
 */
@Component({
  selector: 'app-bin-drill-down',
  templateUrl: './bin-drill-down.component.html',
  styleUrl: './bin-drill-down.component.scss',
  imports: [DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BinDrillDownComponent {
  readonly days = input.required<readonly DayReturns[]>();
  readonly bin = input.required<HistogramBin>();
  readonly kind = input.required<ReturnKind>();
  /** The clicked bin's index in the kind's bins array — matched against
   * each day's Python-stamped ``binIndices`` so the drill-down day list is
   * the histogram count's own membership, not a client re-derivation. */
  readonly binIndex = input.required<number>();
  readonly daySelected = output<number>();

  readonly segmentLegend = SEGMENTS;

  readonly binTitle = computed(() => {
    const bin = this.bin();
    if (bin.lowerPct === null) return `worse than ${bin.upperPct!.toFixed(1)}%`;
    if (bin.upperPct === null) return `${bin.lowerPct.toFixed(1)}% or better`;
    return `${bin.lowerPct.toFixed(1)}% to ${bin.upperPct.toFixed(1)}%`;
  });

  readonly basketDays = computed<readonly DayView[]>(() => {
    const index = this.binIndex();
    const kind = this.kind();
    const members = this.days().filter((d) => d.binIndices[kind] === index);
    const scale = Math.max(
      0.0001,
      ...members.flatMap((d) =>
        SEGMENTS.map(({ key }) => Math.abs((d[key] as number | null) ?? 0)),
      ),
    );
    return members
      .map((d) => ({
        etDate: etIsoDate(d.sessionOpenMsUtc),
        sessionOpenMsUtc: d.sessionOpenMsUtc,
        dayPct: valueForKind(d, kind),
        volume: d.volume,
        segments: SEGMENTS.map(({ key, label, hint }) => {
          const valuePct = d[key] as number | null;
          return {
            label,
            hint,
            valuePct,
            widthShare: valuePct === null ? 0 : Math.abs(valuePct) / scale,
            negative: valuePct !== null && valuePct < 0,
          };
        }),
      }))
      .sort((a, b) => (b.dayPct ?? 0) - (a.dayPct ?? 0));
  });
}

import {
  Component, input, computed,
  ChangeDetectionStrategy
} from '@angular/core';
import { BacktestTrade } from '../../../graphql/types';

@Component({
  selector: 'app-backtest-svg-chart',
  standalone: true,
  template: `
    <div class="svg-chart-wrapper">
      <svg
        [attr.viewBox]="'0 0 ' + width() + ' ' + height()"
        [attr.width]="'100%'"
        [attr.height]="height()"
        preserveAspectRatio="xMidYMid meet"
      >
        <!-- Grid lines -->
        @for (y of gridLinesY(); track y.value) {
          <line
            [attr.x1]="padding"
            [attr.y1]="y.pixel"
            [attr.x2]="width() - padding"
            [attr.y2]="y.pixel"
            stroke="#f0f0f0"
            stroke-width="1"
          />
          <text
            [attr.x]="padding - 6"
            [attr.y]="y.pixel + 4"
            text-anchor="end"
            fill="#999"
            font-size="11"
          >{{ formatValue(y.value) }}</text>
        }

        <!-- Zero line -->
        @if (zeroLineY() !== null) {
          <line
            [attr.x1]="padding"
            [attr.y1]="zeroLineY()"
            [attr.x2]="width() - padding"
            [attr.y2]="zeroLineY()"
            stroke="#aaa"
            stroke-width="1"
            stroke-dasharray="4,3"
          />
        }

        <!-- Area fill -->
        @if (areaPath()) {
          <path
            [attr.d]="areaPath()"
            [attr.fill]="isPositive() ? 'rgba(46,125,50,0.10)' : 'rgba(192,57,43,0.10)'"
          />
        }

        <!-- Equity curve line -->
        @if (linePath()) {
          <polyline
            [attr.points]="linePath()"
            fill="none"
            [attr.stroke]="isPositive() ? '#2e7d32' : '#c0392b'"
            stroke-width="2"
            stroke-linejoin="round"
            stroke-linecap="round"
          />
        }

        <!-- Data points (if few enough) -->
        @if (points().length <= 60) {
          @for (p of points(); track $index) {
            <circle
              [attr.cx]="p.x"
              [attr.cy]="p.y"
              r="3"
              [attr.fill]="p.value >= 0 ? '#2e7d32' : '#c0392b'"
              stroke="white"
              stroke-width="1"
            />
          }
        }

        <!-- X-axis labels -->
        @for (label of xLabels(); track label.index) {
          <text
            [attr.x]="label.x"
            [attr.y]="height() - 4"
            text-anchor="middle"
            fill="#999"
            font-size="10"
          >{{ label.text }}</text>
        }
      </svg>
    </div>
  `,
  styles: [`
    .svg-chart-wrapper {
      background: white;
      border: 1px solid #e9ecef;
      border-radius: 8px;
      padding: 12px;
      overflow: hidden;
    }
    svg {
      display: block;
    }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BacktestSvgChartComponent {
  trades = input<BacktestTrade[]>([]);
  height = input(300);
  width = input(800);

  readonly padding = 60;
  readonly topPad = 16;
  readonly bottomPad = 24;

  isPositive = computed(() => {
    const t = this.trades();
    if (!t.length) return true;
    return t[t.length - 1].cumulativePnl >= 0;
  });

  points = computed(() => {
    const t = this.trades();
    if (!t.length) return [];

    const values = t.map(tr => tr.cumulativePnl);
    const minVal = Math.min(0, ...values);
    const maxVal = Math.max(0, ...values);
    const range = maxVal - minVal || 1;

    const w = this.width();
    const h = this.height();
    const chartW = w - this.padding * 2;
    const chartH = h - this.topPad - this.bottomPad;

    return t.map((tr, i) => ({
      x: this.padding + (i / Math.max(t.length - 1, 1)) * chartW,
      y: this.topPad + chartH - ((tr.cumulativePnl - minVal) / range) * chartH,
      value: tr.cumulativePnl,
    }));
  });

  linePath = computed(() => {
    const pts = this.points();
    if (pts.length < 2) return '';
    return pts.map(p => `${p.x},${p.y}`).join(' ');
  });

  areaPath = computed(() => {
    const pts = this.points();
    if (pts.length < 2) return '';

    const t = this.trades();
    const values = t.map(tr => tr.cumulativePnl);
    const minVal = Math.min(0, ...values);
    const maxVal = Math.max(0, ...values);
    const range = maxVal - minVal || 1;
    const h = this.height();
    const chartH = h - this.topPad - this.bottomPad;
    const baseY = this.topPad + chartH - ((0 - minVal) / range) * chartH;

    const first = pts[0];
    const last = pts[pts.length - 1];
    let d = `M ${first.x},${baseY}`;
    for (const p of pts) {
      d += ` L ${p.x},${p.y}`;
    }
    d += ` L ${last.x},${baseY} Z`;
    return d;
  });

  zeroLineY = computed<number | null>(() => {
    const t = this.trades();
    if (!t.length) return null;
    const values = t.map(tr => tr.cumulativePnl);
    const minVal = Math.min(0, ...values);
    const maxVal = Math.max(0, ...values);
    if (minVal >= 0) return null; // all positive, no zero line needed
    const range = maxVal - minVal || 1;
    const h = this.height();
    const chartH = h - this.topPad - this.bottomPad;
    return this.topPad + chartH - ((0 - minVal) / range) * chartH;
  });

  gridLinesY = computed(() => {
    const t = this.trades();
    if (!t.length) return [];
    const values = t.map(tr => tr.cumulativePnl);
    const minVal = Math.min(0, ...values);
    const maxVal = Math.max(0, ...values);
    const range = maxVal - minVal || 1;
    const h = this.height();
    const chartH = h - this.topPad - this.bottomPad;
    const lines: { value: number; pixel: number }[] = [];
    const step = range / 5;
    for (let i = 0; i <= 5; i++) {
      const val = minVal + step * i;
      lines.push({
        value: val,
        pixel: this.topPad + chartH - ((val - minVal) / range) * chartH,
      });
    }
    return lines;
  });

  xLabels = computed(() => {
    const t = this.trades();
    if (!t.length) return [];
    const w = this.width();
    const chartW = w - this.padding * 2;
    const maxLabels = 6;
    const step = Math.max(1, Math.floor(t.length / maxLabels));
    const labels: { index: number; x: number; text: string }[] = [];
    for (let i = 0; i < t.length; i += step) {
      labels.push({
        index: i,
        x: this.padding + (i / Math.max(t.length - 1, 1)) * chartW,
        text: `#${i + 1}`,
      });
    }
    return labels;
  });

  formatValue(val: number): string {
    return val.toFixed(2);
  }
}

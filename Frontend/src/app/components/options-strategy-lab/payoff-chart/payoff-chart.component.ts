import {
  Component, input, computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { UIChart } from 'primeng/chart';
import {
  PayoffPoint, ChartCurveData, GreekCurvePoint, GreekType,
} from '../../../graphql/types';
import { lognormalCdf } from '../../../utils/black-scholes';

const GREEK_LABELS: Record<GreekType, string> = {
  delta: 'Delta (Δ)',
  gamma: 'Gamma (Γ)',
  theta: 'Theta (Θ)',
  vega: 'Vega (V)',
  rho: 'Rho (ρ)',
};

@Component({
  selector: 'app-payoff-chart',
  standalone: true,
  imports: [UIChart],
  templateUrl: './payoff-chart.component.html',
  styleUrls: ['./payoff-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PayoffChartComponent {
  expirationCurve = input<PayoffPoint[]>([]);
  currentPnlCurve = input<PayoffPoint[]>([]);
  whatIfCurves = input<ChartCurveData[]>([]);
  greekCurve = input<GreekCurvePoint[]>([]);
  selectedGreek = input<GreekType>('delta');
  breakevens = input<number[]>([]);
  spotPrice = input(0);
  weightedIv = input(0.2);
  timeToExpiry = input(0);
  riskFreeRate = input(0.043);
  height = input(450);

  chartData = computed(() => {
    const pts = this.expirationCurve();
    if (!pts.length) return null;

    const profitColor = 'rgba(34, 197, 94, 0.9)';
    const lossColor = 'rgba(239, 68, 68, 0.9)';
    const profitFill = 'rgba(34, 197, 94, 0.15)';
    const lossFill = 'rgba(239, 68, 68, 0.15)';

    const datasets: any[] = [
      // 1. Expiration P&L (green/red with fill)
      {
        label: 'Expiration P&L',
        data: pts.map(p => ({ x: p.price, y: p.pnl })),
        yAxisID: 'y',
        fill: {
          target: 'origin',
          above: profitFill,
          below: lossFill,
        },
        segment: {
          borderColor: (ctx: any) => {
            const yPrev = ctx.p0?.parsed?.y ?? 0;
            const yCurr = ctx.p1?.parsed?.y ?? 0;
            return (yPrev >= 0 && yCurr >= 0) ? profitColor : lossColor;
          },
        },
        borderColor: profitColor,
        borderWidth: 2.5,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0,
        order: 2,
      },
    ];

    // 2. Current P&L (blue dashed)
    const currentCurve = this.currentPnlCurve();
    if (currentCurve.length > 0) {
      datasets.push({
        label: 'Current P&L',
        data: currentCurve.map(p => ({ x: p.price, y: p.pnl })),
        yAxisID: 'y',
        borderColor: 'rgba(59, 130, 246, 0.9)',
        borderWidth: 2,
        borderDash: [8, 4],
        pointRadius: 0,
        pointHoverRadius: 4,
        cubicInterpolationMode: 'monotone',
        tension: 0.4,
        fill: false,
        order: 3,
      });
    }

    // 3. What-if curves (monotone cubic)
    for (const curve of this.whatIfCurves()) {
      datasets.push({
        label: curve.label,
        data: curve.points.map(p => ({ x: p.price, y: p.pnl })),
        yAxisID: 'y',
        borderColor: curve.color,
        borderWidth: 1.8,
        borderDash: curve.borderDash ?? [6, 3],
        pointRadius: 0,
        pointHoverRadius: 3,
        cubicInterpolationMode: 'monotone',
        tension: 0.4,
        fill: false,
        order: 4,
      });
    }

    // 4. Greek curve (right Y-axis, orange, monotone cubic)
    const greekPts = this.greekCurve();
    if (greekPts.length > 0) {
      const greekLabel = GREEK_LABELS[this.selectedGreek()];
      datasets.push({
        label: greekLabel,
        data: greekPts.map(p => ({ x: p.price, y: p.value })),
        yAxisID: 'y1',
        borderColor: 'rgba(234, 88, 12, 0.9)',
        borderWidth: 2,
        borderDash: [4, 4],
        pointRadius: 0,
        pointHoverRadius: 3,
        cubicInterpolationMode: 'monotone',
        tension: 0.4,
        fill: false,
        order: 5,
      });
    }

    // 5. Zero reference line (two endpoints spanning the range)
    datasets.push({
      label: 'Zero',
      data: [
        { x: pts[0].price, y: 0 },
        { x: pts[pts.length - 1].price, y: 0 },
      ],
      yAxisID: 'y',
      borderColor: 'rgba(120, 120, 120, 0.5)',
      borderWidth: 1,
      borderDash: [4, 4],
      pointRadius: 0,
      pointHoverRadius: 0,
      fill: false,
      order: 10,
    });

    // 6. Spot price marker (gold triangle)
    const spot = this.spotPrice();
    if (spot > 0) {
      const maxPnl = Math.max(...pts.map(p => p.pnl));
      datasets.push({
        label: `Spot: $${spot.toFixed(2)}`,
        data: [{ x: spot, y: maxPnl }],
        yAxisID: 'y',
        borderWidth: 0,
        pointRadius: 7,
        pointBackgroundColor: 'rgba(217, 119, 6, 0.9)',
        pointStyle: 'triangle',
        showLine: false,
        fill: false,
        order: 0,
      });
    }

    // 7. Breakeven markers (purple diamond)
    const bes = this.breakevens();
    if (bes.length > 0) {
      datasets.push({
        label: `BE: ${bes.map(b => '$' + b.toFixed(2)).join(', ')}`,
        data: bes.map(be => ({ x: be, y: 0 })),
        yAxisID: 'y',
        borderColor: 'transparent',
        borderWidth: 0,
        pointRadius: 8,
        pointBackgroundColor: 'rgba(147, 51, 234, 0.9)',
        pointStyle: 'rectRot',
        showLine: false,
        fill: false,
        order: 0,
      });
    }

    return { datasets };
  });

  chartOptions = computed(() => {
    const hasGreek = this.greekCurve().length > 0;
    const greekLabel = GREEK_LABELS[this.selectedGreek()];
    const spot = this.spotPrice();
    const iv = this.weightedIv();
    const t = this.timeToExpiry();
    const r = this.riskFreeRate();

    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index' as const,
        intersect: false,
      },
      plugins: {
        legend: {
          display: true,
          position: 'top' as const,
          labels: {
            usePointStyle: true,
            filter: (item: any) => item.text !== 'Zero',
          },
        },
        tooltip: {
          filter: (item: any) => {
            const lbl = item.dataset?.label ?? '';
            return lbl !== 'Zero' && !lbl.startsWith('Spot:') && !lbl.startsWith('BE:');
          },
          callbacks: {
            title: (items: any[]) => {
              const x = items[0]?.parsed?.x;
              return x != null ? `Underlying: $${Number(x).toFixed(2)}` : '';
            },
            label: (ctx: any) => {
              const val = ctx.parsed?.y;
              if (val == null) return '';
              if (ctx.dataset.yAxisID === 'y1') {
                return ` ${ctx.dataset.label}: ${val.toFixed(4)}`;
              }
              const prefix = val >= 0 ? '+' : '';
              return ` ${ctx.dataset.label}: ${prefix}$${val.toFixed(2)}`;
            },
            afterBody: (items: any[]) => {
              if (!items.length || spot <= 0 || iv <= 0 || t <= 0) return '';
              const price = items[0]?.parsed?.x;
              if (price == null || price <= 0) return '';
              const pBelow = lognormalCdf(price, spot, r, iv, t);
              const pAbove = 1 - pBelow;
              return `\n← ${(pBelow * 100).toFixed(1)}%  ·  ${(pAbove * 100).toFixed(1)}% →`;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'linear' as const,
          display: true,
          title: {
            display: true,
            text: 'Underlying Price',
          },
          ticks: {
            maxTicksLimit: 12,
            callback: (value: any) => '$' + Number(value).toFixed(0),
          },
        },
        y: {
          display: true,
          position: 'left' as const,
          title: {
            display: true,
            text: 'Profit / Loss ($)',
          },
          ticks: {
            callback: (value: any) => {
              const v = Number(value);
              const prefix = v >= 0 ? '+' : '';
              return `${prefix}$${v.toFixed(0)}`;
            },
          },
        },
        y1: {
          display: hasGreek,
          position: 'right' as const,
          title: {
            display: hasGreek,
            text: greekLabel,
            color: 'rgba(234, 88, 12, 0.9)',
          },
          grid: { drawOnChartArea: false },
          ticks: {
            color: 'rgba(234, 88, 12, 0.8)',
            callback: (value: any) => Number(value).toFixed(3),
          },
        },
      },
    };
  });

}

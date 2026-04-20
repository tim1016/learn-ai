import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";
import { CommonModule } from "@angular/common";

export interface CalibrationBucket {
  bucket: string;
  count: number;
  correct: number;
  accuracy: number;
}

interface PlotPoint {
  bucket: string;
  count: number;
  confidence: number; // midpoint 0..1
  accuracy: number;   // 0..1
  gap: number;        // accuracy - confidence
  bias: "over" | "under" | "neutral";
  radius: number;     // scaled by count
  // SVG coordinates (0..100 range before viewBox)
  cx: number;
  cy: number;
}

const PADDING = 10; // SVG padding in viewBox units (0-100 space)
const PLOT_W = 100 - 2 * PADDING;

/**
 * Reliability diagram. X-axis is emitted confidence, Y-axis is observed
 * accuracy. The y = x line is perfect calibration: points above are
 * underconfident (safe but leaves alpha on the table), points below are
 * overconfident (dangerous under Kelly sizing).
 *
 * Rendered as inline SVG so there is zero new dependency cost.
 */
@Component({
  selector: "app-reliability-diagram",
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./reliability-diagram.component.html",
  styleUrls: ["./reliability-diagram.component.scss"],
})
export class ReliabilityDiagramComponent {
  readonly buckets = input<CalibrationBucket[]>([]);

  readonly points = computed<PlotPoint[]>(() => {
    const rows = this.buckets();
    const maxCount = rows.reduce((m, r) => Math.max(m, r.count), 0) || 1;

    return rows.map((r): PlotPoint => {
      const midpoint = bucketMidpoint(r.bucket);
      const gap = r.accuracy - midpoint;
      const bias: PlotPoint["bias"] =
        Math.abs(gap) < 0.05 ? "neutral" : gap > 0 ? "under" : "over";
      // Radius: 1.5 at count=1, grows with sqrt up to ~4.5 at max
      const radius = 1.5 + 3 * Math.sqrt(r.count / maxCount);
      return {
        bucket: r.bucket,
        count: r.count,
        confidence: midpoint,
        accuracy: r.accuracy,
        gap,
        bias,
        radius,
        cx: PADDING + midpoint * PLOT_W,
        // SVG y-axis is inverted — accuracy 1.0 should be at top of plot
        cy: PADDING + (1 - r.accuracy) * PLOT_W,
      };
    });
  });

  readonly hasData = computed<boolean>(() => this.buckets().length > 0);

  /** Gridline positions (0, 0.25, 0.5, 0.75, 1.0) in viewBox coordinates. */
  readonly gridlines = [0, 0.25, 0.5, 0.75, 1];

  gridX(v: number): number { return PADDING + v * PLOT_W; }
  gridY(v: number): number { return PADDING + (1 - v) * PLOT_W; }

  readonly corner0 = PADDING;
  readonly corner1 = 100 - PADDING;
  readonly plotWidth = PLOT_W;

  tooltipFor(p: PlotPoint): string {
    const label = p.bias === "under" ? "Underconfident" : p.bias === "over" ? "Overconfident" : "Well-calibrated";
    const gapPct = (p.gap * 100).toFixed(1);
    const sign = p.gap >= 0 ? "+" : "";
    return `${p.bucket}: confidence ${(p.confidence * 100).toFixed(0)}%, actual ${(p.accuracy * 100).toFixed(1)}%, gap ${sign}${gapPct}pp — ${label} · n=${p.count}`;
  }
}

function bucketMidpoint(bucket: string): number {
  const parts = bucket.split("-").map(Number);
  if (parts.length !== 2 || parts.some(Number.isNaN)) return 0.5;
  return (parts[0] + parts[1]) / 2;
}

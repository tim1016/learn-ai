/* eslint-disable @typescript-eslint/no-explicit-any */
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReliabilityDiagramComponent } from './reliability-diagram/reliability-diagram.component';
import { AccuracyHeatmapComponent } from './accuracy-heatmap/accuracy-heatmap.component';

/**
 * InsightPanelComponent — displays per-prediction scoring analytics
 * from the LEAN-ported Insight framework.
 *
 * Shows:
 *  - Summary KPIs (direction accuracy, avg magnitude score, confidence)
 *  - Confidence calibration table (predicted vs actual accuracy per bucket)
 *  - Accuracy by hour-of-day
 *  - Accuracy by quarter
 *  - Magnitude bias
 *  - Scrollable insight detail table with per-prediction scores
 */

interface InsightRecord {
  id: string;
  symbol: string;
  direction: string;
  period_minutes: number;
  magnitude: number | null;
  confidence: number | null;
  source_model: string;
  tag: string;
  generated_time: string;
  close_time: string;
  reference_value: number;
  reference_value_final: number;
  score: {
    direction: number;
    magnitude: number;
    is_final: boolean;
  };
}

interface CalibrationBucket {
  bucket: string;
  count: number;
  correct: number;
  accuracy: number;
}

interface InsightSummaryData {
  total_insights: number;
  scored_insights: number;
  direction_accuracy: number;
  avg_magnitude_score: number;
  avg_confidence_emitted: number;
  confidence_calibration: CalibrationBucket[];
  accuracy_by_hour: Record<string, { count: number; correct: number; accuracy: number }>;
  accuracy_by_quarter: Record<string, { count: number; correct: number; accuracy: number }>;
  magnitude_bias: number;
}

@Component({
  selector: 'app-insight-panel',
  standalone: true,
  imports: [CommonModule, ReliabilityDiagramComponent, AccuracyHeatmapComponent],
  templateUrl: './insight-panel.component.html',
  styleUrls: ['./insight-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InsightPanelComponent {
  insights = input<Record<string, any>[]>([]);
  summary = input<Record<string, any>>({});

  collapsed = signal(false);
  showTable = signal(false);

  // ── Derived calibration diagnostics ──
  // Expected Calibration Error (ECE) = Σ (nᵢ/N) × |accuracyᵢ − confidenceᵢ|
  // Institutional thresholds:
  //   < 0.05  green — production-grade calibration
  //   < 0.10  amber — usable with caution
  //   ≥ 0.10  red   — confidence scores are not probabilities (Doc §32)
  readonly ece = computed<number | null>(() => {
    const rows = this.summaryData?.confidence_calibration ?? [];
    if (rows.length === 0) return null;
    const total = rows.reduce((acc, r) => acc + r.count, 0);
    if (total === 0) return null;
    let ece = 0;
    for (const r of rows) {
      const mid = bucketMid(r.bucket);
      ece += (r.count / total) * Math.abs(r.accuracy - mid);
    }
    return ece;
  });

  readonly eceBand = computed<'green' | 'amber' | 'red' | 'na'>(() => {
    const v = this.ece();
    if (v === null) return 'na';
    if (v < 0.05) return 'green';
    if (v < 0.10) return 'amber';
    return 'red';
  });

  readonly eceVerdict = computed<string>(() => {
    const v = this.ece();
    if (v === null) return 'No calibration buckets to score.';
    if (v < 0.05) return 'Production-grade calibration — confidence scores usable as probabilities.';
    if (v < 0.10) return 'Usable, but recalibration would improve Kelly sizing.';
    return 'Confidence scores are not reliable probabilities — recalibrate before wiring into position sizing.';
  });

  readonly magnitudeBand = computed<'green' | 'amber' | 'red' | 'na'>(() => {
    const bias = this.summaryData?.magnitude_bias;
    if (typeof bias !== 'number') return 'na';
    const mag = Math.abs(bias);
    if (mag < 0.0005) return 'green';      // <0.05 pp absolute bias
    if (mag < 0.002) return 'amber';       // <0.2 pp
    return 'red';
  });

  readonly magnitudeVerdict = computed<string>(() => {
    const bias = this.summaryData?.magnitude_bias;
    if (typeof bias !== 'number') return 'Magnitude not scored.';
    if (Math.abs(bias) < 0.0005) return 'Model predicts move magnitude accurately on average.';
    const dir = bias > 0 ? 'over' : 'under';
    const pp = Math.abs(bias * 100).toFixed(3);
    return `Model ${dir}estimates moves by ~${pp} pp on average. ${bias < 0 ? 'Safer, but indicators may be lagging true volatility.' : 'Risk of fat-tail blowups if sizing scales with predicted magnitude.'}`;
  });

  // Typed access
  get summaryData(): InsightSummaryData | null {
    const s = this.summary();
    return (s && s['total_insights'] !== undefined) ? s as unknown as InsightSummaryData : null;
  }

  get insightRecords(): InsightRecord[] {
    return this.insights() as unknown as InsightRecord[];
  }

  get calibrationRows(): CalibrationBucket[] {
    return this.summaryData?.confidence_calibration ?? [];
  }

  get hourlyRows(): { hour: number; count: number; correct: number; accuracy: number }[] {
    const byHour = this.summaryData?.accuracy_by_hour ?? {};
    return Object.entries(byHour)
      .map(([h, data]) => ({ hour: Number(h), ...data }))
      .sort((a, b) => a.hour - b.hour);
  }

  get quarterlyRows(): { quarter: string; count: number; correct: number; accuracy: number }[] {
    const byQ = this.summaryData?.accuracy_by_quarter ?? {};
    return Object.entries(byQ)
      .map(([q, data]) => ({ quarter: q, ...data }))
      .sort((a, b) => a.quarter.localeCompare(b.quarter));
  }

  pct(val: number | undefined): string {
    if (val === undefined || val === null) return '—';
    return (val * 100).toFixed(1) + '%';
  }

  formatTime(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, '');
  }

  toggleCollapse(): void {
    this.collapsed.update(v => !v);
  }

  toggleTable(): void {
    this.showTable.update(v => !v);
  }

  directionLabel(dir: string): string {
    if (dir === 'up') return '▲ UP';
    if (dir === 'down') return '▼ DOWN';
    return '— FLAT';
  }

  scoreColor(score: number): string {
    if (score >= 0.8) return '#00c896';
    if (score >= 0.5) return '#ffc107';
    return '#e5334e';
  }

  calibrationGap(bucket: CalibrationBucket): string {
    // gap = |midpoint of bucket - actual accuracy|
    const parts = bucket.bucket.split('-').map(Number);
    const midpoint = (parts[0] + parts[1]) / 2;
    const gap = Math.abs(midpoint - bucket.accuracy);
    return gap.toFixed(2);
  }

  biasLabel(bias: number): string {
    if (Math.abs(bias) < 0.0001) return 'Neutral';
    return bias > 0 ? 'Overestimates' : 'Underestimates';
  }
}

function bucketMid(bucket: string): number {
  const parts = bucket.split('-').map(Number);
  if (parts.length !== 2 || parts.some(Number.isNaN)) return 0.5;
  return (parts[0] + parts[1]) / 2;
}

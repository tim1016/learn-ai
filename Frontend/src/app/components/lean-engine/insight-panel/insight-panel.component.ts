import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';

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
  imports: [CommonModule],
  templateUrl: './insight-panel.component.html',
  styleUrls: ['./insight-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InsightPanelComponent {
  insights = input<Record<string, any>[]>([]);
  summary = input<Record<string, any>>({});

  collapsed = signal(false);
  showTable = signal(false);

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

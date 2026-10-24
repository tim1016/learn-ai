import {
  Component, input, output, ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { QualityReport } from '../data-lab-chart/data-lab-chart.component';

@Component({
  selector: 'app-quality-modal',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './quality-modal.component.html',
  styleUrls: ['./quality-modal.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QualityModalComponent {
  quality = input.required<QualityReport>();
  closed = output();

  /** Format a ms epoch as a readable ET datetime string. */
  formatTs(ms: number): string {
    const d = new Date(ms);
    return d.toLocaleString('en-US', {
      timeZone: 'America/New_York',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }

  get hasIssues(): boolean {
    const q = this.quality();
    return q.gaps_found > 0
      || q.duplicates_removed > 0
      || q.missing_sessions > 0
      || q.synthetic_bars > 0;
  }

  get hasProcessingDetails(): boolean {
    const q = this.quality();
    return (q.flat_bars_detected ?? 0) > 0
      || (q.ohlc_violations_detected ?? 0) > 0
      || (q.out_of_order_fixed ?? 0) > 0;
  }

  get gapSummary(): string {
    const q = this.quality();
    if (!q.gap_details?.length) return '';
    const counts: Record<string, number> = {};
    for (const g of q.gap_details) {
      const cls = g.classification ?? 'unknown';
      counts[cls] = (counts[cls] ?? 0) + 1;
    }
    return Object.entries(counts)
      .map(([cls, n]) => `${n} ${cls}`)
      .join(', ');
  }

  get coverageClass(): string {
    const pct = this.quality().session_coverage_pct;
    if (pct >= 99) return 'good';
    if (pct >= 95) return 'ok';
    return 'warn';
  }

  close(): void {
    this.closed.emit();
  }

  onBackdrop(event: MouseEvent): void {
    if ((event.target as HTMLElement).classList.contains('modal-backdrop')) {
      this.close();
    }
  }

  onBackdropKeydown(event: Event): void {
    if (!(event.target as HTMLElement).classList.contains('modal-backdrop')) return;
    event.preventDefault();
    this.close();
  }
}

import {
  Component, input, output, ChangeDetectionStrategy, computed,
} from '@angular/core';
import { CommonModule, DatePipe } from '@angular/common';
import { QualityReport, GapDetail } from '../data-lab-chart/data-lab-chart.component';

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
  closed = output<void>();

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
}

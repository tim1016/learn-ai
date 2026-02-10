import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AggregatesSummary } from '../../../graphql/types';

@Component({
  selector: 'app-summary-stats',
  standalone: true,
  imports: [CommonModule],
  template: `
    @if (summary) {
      <div class="stats-grid">
        <div class="stat-card">
          <span class="stat-label">Period High</span>
          <span class="stat-value">{{ summary.periodHigh | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Period Low</span>
          <span class="stat-value">{{ summary.periodLow | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Avg Volume</span>
          <span class="stat-value">{{ summary.averageVolume | number:'1.0-0' }}</span>
        </div>
        @if (summary.averageVwap) {
          <div class="stat-card">
            <span class="stat-label">Avg VWAP</span>
            <span class="stat-value">{{ summary.averageVwap | number:'1.2-2' }}</span>
          </div>
        }
        <div class="stat-card">
          <span class="stat-label">Open</span>
          <span class="stat-value">{{ summary.openPrice | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Close</span>
          <span class="stat-value">{{ summary.closePrice | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Change</span>
          <span class="stat-value"
                [class.positive]="summary.priceChange >= 0"
                [class.negative]="summary.priceChange < 0">
            {{ summary.priceChange | number:'1.2-2' }}
            ({{ summary.priceChangePercent | number:'1.2-2' }}%)
          </span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Total Bars</span>
          <span class="stat-value">{{ summary.totalBars }}</span>
        </div>
      </div>
    }
  `,
  styles: [`
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .stat-card {
      padding: 16px;
      background: #f8f9fa;
      border-radius: 8px;
      border: 1px solid #e9ecef;
      display: flex;
      flex-direction: column;
    }
    .stat-label {
      font-size: 12px;
      color: #666;
      margin-bottom: 4px;
      text-transform: uppercase;
    }
    .stat-value {
      font-size: 20px;
      font-weight: 600;
    }
    .positive { color: #26a69a; }
    .negative { color: #ef5350; }
  `]
})
export class SummaryStatsComponent {
  @Input() summary: AggregatesSummary | null = null;
}

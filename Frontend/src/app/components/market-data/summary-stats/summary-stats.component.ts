import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AggregatesSummary } from '../../../graphql/types';

@Component({
  selector: 'app-summary-stats',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (summary(); as s) {
      <div class="stats-grid">
        <div class="stat-card">
          <span class="stat-label">Period High</span>
          <span class="stat-value">{{ s.periodHigh | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Period Low</span>
          <span class="stat-value">{{ s.periodLow | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Avg Volume</span>
          <span class="stat-value">{{ s.averageVolume | number:'1.0-0' }}</span>
        </div>
        @if (s.averageVwap) {
          <div class="stat-card">
            <span class="stat-label">Avg VWAP</span>
            <span class="stat-value">{{ s.averageVwap | number:'1.2-2' }}</span>
          </div>
        }
        <div class="stat-card">
          <span class="stat-label">Open</span>
          <span class="stat-value">{{ s.openPrice | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Close</span>
          <span class="stat-value">{{ s.closePrice | number:'1.2-2' }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Change</span>
          <span class="stat-value"
                [class.positive]="s.priceChange >= 0"
                [class.negative]="s.priceChange < 0">
            {{ s.priceChange | number:'1.2-2' }}
            ({{ s.priceChangePercent | number:'1.2-2' }}%)
          </span>
        </div>
        <div class="stat-card">
          <span class="stat-label">Total Bars</span>
          <span class="stat-value">{{ s.totalBars }}</span>
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
  summary = input<AggregatesSummary | null>(null);
}

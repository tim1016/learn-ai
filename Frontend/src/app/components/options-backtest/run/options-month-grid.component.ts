import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, signal } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { heatStrengths, isLoss } from './run-chart-scale';

export interface MonthMetric {
  readonly key: string;
  readonly label: string;
  readonly unit: 'usd' | 'count';
  readonly signed: boolean;
}
export interface MonthCell {
  readonly year: number;
  readonly month: number;
  readonly values: Readonly<Record<string, string>>;
}

/** Calendar placement and color only; every quantity is authored by Python. */
@Component({
  selector: 'app-options-month-grid',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-month-grid.component.html',
  styleUrl: './options-month-grid.component.scss',
})
export class OptionsMonthGridComponent {
  readonly cells = input.required<readonly MonthCell[]>();
  readonly metrics = input.required<readonly MonthMetric[]>();
  protected readonly months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  protected readonly selectedMetric = linkedSignal<readonly MonthMetric[], MonthMetric>({ source: this.metrics, computation: (metrics, previous) => metrics.find((metric) => metric.key === previous?.value.key) ?? metrics[0] });
  protected readonly selectedMonth = signal<{ year: number; month: number } | null>(null);
  protected readonly years = computed(() => {
    const metric = this.selectedMetric();
    const cells = this.cells();
    const values = cells.map((cell) => cell.values[metric.key]);
    const strengths = heatStrengths(values);
    const indexed = new Map(cells.map((cell, index) => [`${cell.year}-${cell.month}`, { ...cell, label: `${this.months[cell.month - 1]} ${cell.year}`, buttonLabel: `${this.months[cell.month - 1]} ${cell.year}, ${metric.label}: ${values[index]}`, value: values[index], strength: strengths[index], loss: metric.signed && isLoss(values[index]) }]));
    return [...new Set(cells.map((cell) => cell.year))].sort((a, b) => a - b).map((year) => ({ year, months: this.months.map((_, index) => indexed.get(`${year}-${index + 1}`) ?? null) }));
  });
  protected readonly inspected = computed(() => {
    const selected = this.selectedMonth();
    if (selected === null) return null;
    const cell = this.cells().find((cell) => cell.year === selected.year && cell.month === selected.month);
    return cell ? { label: `${this.months[cell.month - 1]} ${cell.year}`, values: this.metrics().map((metric) => ({ ...metric, value: cell.values[metric.key] })) } : null;
  });
}

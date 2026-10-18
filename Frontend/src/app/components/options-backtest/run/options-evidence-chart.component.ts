import { ChangeDetectionStrategy, Component, computed, input, linkedSignal } from '@angular/core';

import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { equityPlot } from './run-chart-scale';

export interface EvidencePoint {
  readonly timestamp_ms: number;
  readonly value: string;
}

/** ADR 0068: project authored decimals to pixels; the inspected value stays exact. */
@Component({
  selector: 'app-options-evidence-chart',
  imports: [TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-evidence-chart.component.html',
  styleUrl: './options-evidence-chart.component.scss',
})
export class OptionsEvidenceChartComponent {
  readonly title = input.required<string>();
  readonly explanation = input.required<string>();
  readonly points = input.required<readonly EvidencePoint[]>();
  readonly risk = input(false);
  protected readonly lastIndex = computed(() => this.points().length > 0 ? this.points().length - 1 : 0);
  protected readonly selected = linkedSignal(this.lastIndex);
  protected readonly plot = computed(() => {
    const points = this.points();
    const scale = equityPlot(points.map((point) => point.value));
    if (scale === null) return null;
    return { line: scale.coordinates.map((point) => `${point.x},${point.y}`).join(' '), geometry: scale.coordinates, high: scale.high, low: scale.low, first: points[0].timestamp_ms, last: points.at(-1)?.timestamp_ms };
  });
  protected readonly inspected = computed(() => {
    const point = this.points()[this.selected()];
    const pixel = this.plot()?.geometry[this.selected()];
    return point && pixel ? { ...point, ...pixel } : null;
  });
  protected inspect(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.selected.set(event.target.valueAsNumber);
  }
}

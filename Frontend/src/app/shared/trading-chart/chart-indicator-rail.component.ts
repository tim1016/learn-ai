import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import type {
  IndicatorCategory,
  IndicatorInfo,
  IndicatorParamConfig,
} from '../indicator-catalog/indicator-catalog.service';
import {
  IndicatorPickerComponent,
  type IndicatorPickerAdd,
} from '../indicator-picker/indicator-picker.component';
import type { TradingIndicatorChip } from './trading-chart.types';

export interface ChartIndicatorColorChange {
  id: string;
  color: string;
}

/** Shared expanded-chart rail for selecting and removing indicator series. */
@Component({
  selector: 'app-chart-indicator-rail',
  imports: [IndicatorPickerComponent],
  templateUrl: './chart-indicator-rail.component.html',
  styleUrl: './chart-indicator-rail.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    role: 'complementary',
    'aria-label': 'Indicator picker rail',
  },
})
export class ChartIndicatorRailComponent {
  readonly activeIndicators = input<readonly TradingIndicatorChip[]>([]);
  readonly activeKeys = input<readonly string[]>([]);
  readonly categories = input<readonly IndicatorCategory[]>([]);
  readonly catalogLoading = input(false);
  readonly calculationLoading = input(false);
  readonly error = input<string | null>(null);
  readonly allowAdditionalInstances = input(true);
  readonly allowColorChanges = input(false);

  readonly indicatorAdded = output<IndicatorPickerAdd>();
  readonly indicatorRemoved = output<string>();
  readonly indicatorColorChanged = output<ChartIndicatorColorChange>();

  protected readonly pendingIndicator = signal<IndicatorPickerAdd | null>(null);
  protected readonly pendingInfo = computed<IndicatorInfo | null>(() => {
    const entry = this.pendingIndicator();
    return entry ? this.findIndicator(entry.name) : null;
  });

  protected requestIndicator(entry: IndicatorPickerAdd): void {
    const info = this.findIndicator(entry.name);
    if (!info?.configurable_params.length) {
      this.indicatorAdded.emit(entry);
      return;
    }
    this.pendingIndicator.set({ name: entry.name, params: { ...entry.params } });
  }

  protected updatePendingParam(param: IndicatorParamConfig, event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const value = Number(event.target.value);
    if (!Number.isFinite(value)) return;
    this.pendingIndicator.update((entry) => entry && {
      ...entry,
      params: { ...entry.params, [param.name]: value },
    });
  }

  protected pendingParamValue(param: IndicatorParamConfig): number {
    return this.pendingIndicator()?.params[param.name] ?? param.default;
  }

  protected confirmPendingIndicator(): void {
    const entry = this.pendingIndicator();
    if (!entry) return;
    this.indicatorAdded.emit(entry);
    this.pendingIndicator.set(null);
  }

  protected cancelPendingIndicator(): void {
    this.pendingIndicator.set(null);
  }

  protected updateIndicatorColor(id: string, event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    this.indicatorColorChanged.emit({ id, color: event.target.value });
  }

  private findIndicator(name: string): IndicatorInfo | null {
    for (const category of this.categories()) {
      const info = category.indicators.find((indicator) => indicator.name === name);
      if (info) return info;
    }
    return null;
  }
}

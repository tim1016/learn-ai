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
  protected readonly paramErrors = signal<Record<string, string>>({});
  protected readonly pendingInfo = computed<IndicatorInfo | null>(() => {
    const entry = this.pendingIndicator();
    return entry ? this.findIndicator(entry.name) : null;
  });
  protected readonly pendingHasInvalidParam = computed(
    () => Object.keys(this.paramErrors()).length > 0,
  );

  protected requestIndicator(entry: IndicatorPickerAdd): void {
    const info = this.findIndicator(entry.name);
    if (!info?.configurable_params.length) {
      this.indicatorAdded.emit(entry);
      return;
    }
    this.paramErrors.set({});
    this.pendingIndicator.set({ name: entry.name, params: { ...entry.params } });
  }

  /**
   * Reject a parameter here rather than letting the calculation path reject it.
   * The catalog advertises a type and a range; a fraction in an `int` field or
   * a value outside min/max is refused by the Python indicator service with a
   * 400, which stranded the freshly added indicator in an error state until the
   * operator removed it. Validating at the control keeps the configuration open
   * and says what is wrong.
   */
  protected validateParam(param: IndicatorParamConfig, raw: string): string | null {
    const trimmed = raw.trim();
    if (trimmed === '') return `${param.name} is required`;
    const value = Number(trimmed);
    if (!Number.isFinite(value)) return `${param.name} must be a number`;
    if (param.type === 'int' && !Number.isInteger(value)) {
      return `${param.name} must be a whole number`;
    }
    if (value < param.min || value > param.max) {
      return `${param.name} must be between ${param.min} and ${param.max}`;
    }
    return null;
  }

  protected updatePendingParam(param: IndicatorParamConfig, event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const raw = event.target.value;
    const message = this.validateParam(param, raw);
    this.paramErrors.update((errors) =>
      message === null
        ? Object.fromEntries(Object.entries(errors).filter(([name]) => name !== param.name))
        : { ...errors, [param.name]: message },
    );
    if (message !== null) return;
    this.pendingIndicator.update((entry) => entry && {
      ...entry,
      params: { ...entry.params, [param.name]: Number(raw.trim()) },
    });
  }

  protected pendingParamValue(param: IndicatorParamConfig): number {
    return this.pendingIndicator()?.params[param.name] ?? param.default;
  }

  protected paramError(param: IndicatorParamConfig): string | null {
    return this.paramErrors()[param.name] ?? null;
  }

  protected confirmPendingIndicator(): void {
    const entry = this.pendingIndicator();
    if (!entry || this.pendingHasInvalidParam()) return;
    this.indicatorAdded.emit(entry);
    this.pendingIndicator.set(null);
    this.paramErrors.set({});
  }

  protected cancelPendingIndicator(): void {
    this.pendingIndicator.set(null);
    this.paramErrors.set({});
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

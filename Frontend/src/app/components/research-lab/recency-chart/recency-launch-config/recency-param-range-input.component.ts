import { ChangeDetectionStrategy, Component, computed, input, model } from "@angular/core";
import type { LowHighStepRange, ParamRange, ValueListRange } from "./recency-param-range";

function parseValuesList(raw: string): number[] {
  return raw
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => !Number.isNaN(n));
}

/**
 * One numeric strategy parameter's sweep-range editor: value-list or
 * low/high/step (design spec D4). A plain model()-bound presentational
 * control, not a Signal Forms field — the compound value has no
 * validation surface beyond what the two modes' inputs already enforce.
 */
@Component({
  selector: "app-recency-param-range-input",
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-param-range-input.component.html",
  styleUrl: "./recency-param-range-input.component.scss",
})
export class RecencyParamRangeInputComponent {
  readonly paramName = input.required<string>();
  readonly title = input<string>("");
  readonly defaultValue = input<number>(0);

  readonly range = model.required<ParamRange>();

  readonly isListMode = computed(() => this.range().type === "value_list");
  readonly isRangeMode = computed(() => this.range().type === "low_high_step");

  readonly valuesText = computed(() => {
    const r = this.range();
    return r.type === "value_list" ? r.values.join(", ") : "";
  });

  readonly lowValue = computed(() => {
    const r = this.range();
    return r.type === "low_high_step" ? r.low : this.defaultValue();
  });

  readonly highValue = computed(() => {
    const r = this.range();
    return r.type === "low_high_step" ? r.high : this.defaultValue();
  });

  readonly stepValue = computed(() => {
    const r = this.range();
    return r.type === "low_high_step" ? r.step : 1;
  });

  switchToListMode(): void {
    if (this.isListMode()) return;
    const list: ValueListRange = { type: "value_list", values: [this.defaultValue()] };
    this.range.set(list);
  }

  switchToRangeMode(): void {
    if (this.isRangeMode()) return;
    const range: LowHighStepRange = { type: "low_high_step", low: this.defaultValue(), high: this.defaultValue(), step: 1 };
    this.range.set(range);
  }

  onValuesTextInput(raw: string): void {
    this.range.set({ type: "value_list", values: parseValuesList(raw) });
  }

  onLowInput(raw: string): void {
    this.range.set({ type: "low_high_step", low: Number(raw), high: this.highValue(), step: this.stepValue() });
  }

  onHighInput(raw: string): void {
    this.range.set({ type: "low_high_step", low: this.lowValue(), high: Number(raw), step: this.stepValue() });
  }

  onStepInput(raw: string): void {
    this.range.set({ type: "low_high_step", low: this.lowValue(), high: this.highValue(), step: Number(raw) });
  }
}

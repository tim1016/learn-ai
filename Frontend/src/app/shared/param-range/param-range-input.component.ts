import { ChangeDetectionStrategy, Component, computed, input, model, signal } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { InputText } from "primeng/inputtext";
import { parseValueList, type LowHighStepRange, type ParamRange, type ValueListRange } from "./param-range";

/**
 * One numeric strategy parameter's sweep-range editor: value-list or
 * low/high/step (design spec D4). A plain model()-bound presentational
 * control, not a Signal Forms field — the compound value has no
 * validation surface beyond what the two modes' inputs already enforce.
 */
@Component({
  selector: "app-param-range-input",
  imports: [ButtonModule, InputText],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./param-range-input.component.html",
  styleUrl: "./param-range-input.component.scss",
})
export class ParamRangeInputComponent {
  readonly paramName = input.required<string>();
  readonly title = input<string>("");
  readonly defaultValue = input<number>(0);

  readonly range = model.required<ParamRange>();

  readonly isListMode = computed(() => this.range().type === "value_list");
  readonly isRangeMode = computed(() => this.range().type === "low_high_step");

  /** What the operator has typed since the last external range change; null renders the model. */
  private readonly valuesDraft = signal<string | null>(null);

  readonly valuesText = computed(() => {
    const draft = this.valuesDraft();
    if (draft !== null) return draft;
    const r = this.range();
    return r.type === "value_list" ? r.values.join(", ") : "";
  });

  /** Why the typed list is refused, naming the entry; null while it parses. */
  readonly valuesProblem = computed(() => {
    const draft = this.valuesDraft();
    return draft === null ? null : parseValueList(draft).problem;
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
    this.valuesDraft.set(null);
    const list: ValueListRange = { type: "value_list", values: [this.defaultValue()] };
    this.range.set(list);
  }

  switchToRangeMode(): void {
    if (this.isRangeMode()) return;
    const range: LowHighStepRange = { type: "low_high_step", low: this.defaultValue(), high: this.defaultValue(), step: 1 };
    this.range.set(range);
  }

  onValuesTextInput(raw: string): void {
    // A refused list is an empty one: no consumer can launch it, and the
    // field keeps the raw text so the operator can see and fix the entry.
    this.valuesDraft.set(raw);
    this.range.set({ type: "value_list", values: parseValueList(raw).values });
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

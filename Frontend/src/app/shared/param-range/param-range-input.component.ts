import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, model } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { InputText } from "primeng/inputtext";
import { parseValueList, type LowHighStepRange, type ParamRange, type ValueListRange } from "./param-range";

/**
 * One numeric strategy parameter's sweep-range editor: value-list or
 * low/high/step (design spec D4). A plain model()-bound presentational
 * control, not a Signal Forms field. Its one validation surface is the
 * value list: text that does not parse stays in the field, is marked
 * invalid with the entry named, and publishes an empty list, which
 * `rangeProblem` reports to every consumer (#1940).
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

  /** The field's text: what the operator typed, until the parent writes a new range. */
  readonly valuesText = linkedSignal(() => {
    const r = this.range();
    return r.type === "value_list" ? r.values.join(", ") : "";
  });

  /** Why the typed list is refused, naming the entry; null while it parses. */
  readonly valuesProblem = computed(() => {
    if (!this.isListMode()) return null;
    const parsed = parseValueList(this.valuesText());
    return "problem" in parsed ? parsed.problem : null;
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
    // A refused list is published as an empty one (see ValueListRange); the
    // field keeps the raw text so the operator can see and fix the entry.
    const parsed = parseValueList(raw);
    this.range.set({ type: "value_list", values: "values" in parsed ? parsed.values : [] });
    this.valuesText.set(raw);
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

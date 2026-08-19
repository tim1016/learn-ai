import { ChangeDetectionStrategy, Component, effect, input, output } from "@angular/core";
import { FormControl, ReactiveFormsModule } from "@angular/forms";
import { Checkbox } from "primeng/checkbox";

import type { StrategyInfo } from "../../../strategy-lab/strategy-lab.models";
import { RecencyParamRangeInputComponent } from "./recency-param-range-input.component";
import {
  defaultNumericValue,
  numericStrategyParams,
  type ParamRange,
} from "./recency-param-range";

export interface RecencyRangeChange {
  readonly strategyKey: string;
  readonly paramName: string;
  readonly range: ParamRange;
}

@Component({
  selector: "app-recency-strategy-selection",
  imports: [ReactiveFormsModule, Checkbox, RecencyParamRangeInputComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-strategy-selection.component.html",
  styleUrl: "./recency-strategy-selection.component.scss",
})
export class RecencyStrategySelectionComponent {
  readonly eligibleStrategies = input.required<readonly StrategyInfo[]>();
  readonly selectedStrategyKeys = input.required<readonly string[]>();
  readonly selectedStrategies = input.required<readonly StrategyInfo[]>();
  readonly rangesByStrategy = input.required<Readonly<Record<string, Record<string, ParamRange>>>>();

  readonly strategyToggle = output<StrategyInfo>();
  readonly rangeChange = output<RecencyRangeChange>();

  protected readonly selectionControl = new FormControl<string[]>([], { nonNullable: true });
  protected readonly numericParams = numericStrategyParams;
  protected readonly defaultNumericValue = defaultNumericValue;

  constructor() {
    effect(() => {
      const selected = [...this.selectedStrategyKeys()];
      const current = this.selectionControl.value;
      if (selected.length !== current.length || selected.some((key, index) => key !== current[index])) {
        this.selectionControl.setValue(selected, { emitEvent: false });
      }
    });
  }

  protected isSelected(strategy: StrategyInfo): boolean {
    return this.selectedStrategyKeys().includes(strategy.name);
  }

  protected rangeFor(strategyKey: string, paramName: string): ParamRange {
    return this.rangesByStrategy()[strategyKey]?.[paramName] ?? { type: "value_list", values: [0] };
  }
}

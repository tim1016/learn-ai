import { ChangeDetectionStrategy, Component, effect, input, output } from "@angular/core";
import { FormControl, ReactiveFormsModule } from "@angular/forms";
import { InputText } from "primeng/inputtext";
import { RadioButton } from "primeng/radiobutton";

export type DurationPreset = "3m" | "6m" | "12m" | "24m" | "custom";

const DURATION_OPTIONS: readonly { readonly preset: DurationPreset; readonly label: string }[] = [
  { preset: "3m", label: "3 months" },
  { preset: "6m", label: "6 months" },
  { preset: "12m", label: "12 months" },
  { preset: "24m", label: "24 months" },
  { preset: "custom", label: "Custom" },
];

@Component({
  selector: "app-recency-duration-input",
  imports: [ReactiveFormsModule, InputText, RadioButton],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-duration-input.component.html",
  styleUrl: "./recency-duration-input.component.scss",
})
export class RecencyDurationInputComponent {
  readonly preset = input.required<DurationPreset>();
  readonly customMonths = input.required<number>();
  readonly customMonthsError = input.required<string | null>();

  readonly presetChange = output<DurationPreset>();
  readonly customMonthsChange = output<string>();

  protected readonly durationOptions = DURATION_OPTIONS;
  protected readonly durationControl = new FormControl<DurationPreset>("6m", { nonNullable: true });

  constructor() {
    effect(() => {
      const preset = this.preset();
      if (this.durationControl.value !== preset) {
        this.durationControl.setValue(preset, { emitEvent: false });
      }
    });
  }

  protected onCustomMonthsInput(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.customMonthsChange.emit(event.target.value);
    }
  }
}

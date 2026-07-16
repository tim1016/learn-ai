import {
  ChangeDetectionStrategy,
  Component,
  effect,
  input,
  model,
  viewChild,
} from "@angular/core";
import { SelectButton, SelectButtonModule } from "primeng/selectbutton";

import type { AccountDeskLens } from "../../../api/operator-blocker.types";

export interface AccountDeskLensOption {
  label: string;
  value: AccountDeskLens;
}

/** Signal-backed adapter for PrimeNG's account-desk lens selector. */
@Component({
  selector: "app-account-desk-lens-select",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SelectButtonModule],
  templateUrl: "./account-desk-lens-select.component.html",
})
export class AccountDeskLensSelectComponent {
  readonly options = input.required<AccountDeskLensOption[]>();
  readonly value = model<AccountDeskLens>("trader");
  readonly disabled = input(false);
  private readonly selectButton = viewChild(SelectButton);

  constructor() {
    effect(() => {
      const lens = this.value();
      // PrimeNG exposes its selected value only through its ControlValueAccessor.
      this.selectButton()?.writeControlValue(lens, () => undefined);
    });
  }

  selectLens(value: unknown): void {
    if (value !== "trader" && value !== "operator") return;
    this.value.set(value);
  }
}

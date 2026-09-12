import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { GoldenValidation } from "../../services/golden-validation.types";
import { AssetIdentityComponent } from "../../shared/asset-identity/asset-identity.component";
import { ReceiptLabelPipe } from "../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayPipe } from "../../shared/timestamp";
import { GoldenValidationScopeComponent } from "./golden-validation-scope.component";

@Component({
  selector: "app-golden-validation-case",
  imports: [AssetIdentityComponent, GoldenValidationScopeComponent, ReceiptLabelPipe, TimestampDisplayPipe],
  templateUrl: "./golden-validation-case.component.html",
  styleUrl: "./golden-validation-case.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GoldenValidationCaseComponent {
  readonly golden = input.required<GoldenValidation>();
}

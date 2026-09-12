import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { GoldenValidation } from "../../services/golden-validation.types";
import { AssetIdentityComponent } from "../../shared/asset-identity/asset-identity.component";
import { formatReceiptLabel, formatReceiptValue, isOpaqueReceiptValueLabel } from "../../shared/pipes/receipt-label.pipe";

interface ScopeFact {
  label: string;
  value: string;
  opaque: boolean;
  symbol: string | null;
}

interface ParameterScopeView {
  symbol: string | null;
  values: Record<string, unknown>;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value))
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function flattenScope(value: unknown, path: string[] = []): ScopeFact[] {
  const object = record(value);
  if (object !== null) {
    return Object.entries(object).flatMap(([key, child]) => flattenScope(child, [...path, key]));
  }
  if (path.length === 0 || (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean" && value !== null)) {
    return [];
  }
  const key = path[path.length - 1];
  return [{
    label: path.map((segment) => formatReceiptLabel(segment)).join(" · "),
    value: value === null ? "Not recorded" : formatReceiptValue(key, value),
    opaque: isOpaqueReceiptValueLabel(key),
    symbol: key === "symbol" && typeof value === "string" ? value : null,
  }];
}

function separateParameterSymbol(value: Record<string, unknown>): ParameterScopeView {
  const { symbol, ...values } = value;
  return {
    symbol: stringValue(symbol),
    values: typeof symbol === "string" ? values : value,
  };
}

@Component({
  selector: "app-golden-validation-scope",
  imports: [AssetIdentityComponent],
  templateUrl: "./golden-validation-scope.component.html",
  styleUrl: "./golden-validation-scope.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GoldenValidationScopeComponent {
  readonly golden = input.required<GoldenValidation>();

  protected readonly dataPolicyFacts = computed(() => flattenScope(this.golden().validation_case.data_policy));
  protected readonly executionFacts = computed(() => flattenScope(this.golden().validation_case.execution));
  protected readonly parameterScope = computed(() => separateParameterSymbol(this.golden().validation_case.parameters));

  protected json(value: unknown): string {
    return JSON.stringify(value, null, 2);
  }
}

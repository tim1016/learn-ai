import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { GoldenValidation } from "../../services/golden-validation.types";
import { ReceiptLabelPipe } from "../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayPipe } from "../../shared/timestamp";

interface ParityCheckView {
  label: string;
  status: string;
  reason: string | null;
}

interface DivergenceView {
  category: string;
  tradeNumber: number | null;
  atMs: number | null;
  message: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value))
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

@Component({
  selector: "app-golden-parity-evidence",
  imports: [ReceiptLabelPipe, TimestampDisplayPipe],
  templateUrl: "./golden-parity-evidence.component.html",
  styleUrl: "./golden-parity-evidence.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GoldenParityEvidenceComponent {
  readonly golden = input.required<GoldenValidation>();

  protected readonly parityEnvelope = computed(() => record(this.golden().parity_evidence["parity_verdict"]));
  protected readonly parityPayload = computed(() => record(this.parityEnvelope()?.["payload"]) ?? this.parityEnvelope());
  protected readonly parityStatus = computed(() => stringValue(this.parityEnvelope()?.["status"]) ?? stringValue(this.parityPayload()?.["status"]));
  protected readonly parityReason = computed(() => stringValue(this.parityPayload()?.["reason"]));
  protected readonly parityLeftRunId = computed(() => numberValue(this.parityEnvelope()?.["left_run_id"]));
  protected readonly parityRightRunId = computed(() => numberValue(this.parityEnvelope()?.["right_run_id"]));
  protected readonly qualificationWarnings = computed(() => {
    const raw = this.golden().parity_evidence["qualification_warnings"];
    return Array.isArray(raw) ? raw.filter((entry): entry is string => typeof entry === "string") : [];
  });
  protected readonly parityChecks = computed<ParityCheckView[]>(() => {
    const payload = this.parityPayload();
    if (payload === null) return [];
    return [["Native metrics", "native_metric_parity"], ["Readiness", "readiness_parity"], ["Inputs", "input_parity"], ["Parameters", "parameter_parity"], ["Program version", "program_version_parity"]]
      .flatMap(([label, key]) => {
        const check = record(payload[key]);
        const status = stringValue(check?.["status"]);
        return status === null ? [] : [{ label, status, reason: stringValue(check?.["reason"]) }];
      });
  });
  protected readonly divergences = computed<DivergenceView[]>(() => {
    const raw = this.parityPayload()?.["divergences"];
    if (!Array.isArray(raw)) return [];
    return raw.flatMap((entry) => {
      const item = record(entry);
      const category = stringValue(item?.["category"]);
      const message = stringValue(item?.["message"]);
      return category === null || message === null ? [] : [{ category, tradeNumber: numberValue(item?.["trade_number"]), atMs: numberValue(item?.["ms_utc"]), message }];
    });
  });
}

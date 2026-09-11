import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
} from "@angular/core";
import { HttpErrorResponse } from "@angular/common/http";
import { firstValueFrom } from "rxjs";

import { GoldenValidationService } from "../../services/golden-validation.service";
import type { GoldenReviewDecision, GoldenValidation } from "../../services/golden-validation.types";
import { AssetIdentityComponent } from "../../shared/asset-identity/asset-identity.component";
import { formatReceiptLabel, ReceiptLabelPipe } from "../../shared/pipes/receipt-label.pipe";
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
    ? value as Record<string, unknown>
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * A focused research workbench for an immutable Validation Golden Run.
 * It deliberately keeps computed parity evidence and a human promotion
 * decision in separate panels; a manual override never changes the evidence.
 */
@Component({
  selector: "app-golden-validation-workbench",
  imports: [AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayPipe],
  templateUrl: "./golden-validation-workbench.component.html",
  styleUrl: "./golden-validation-workbench.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GoldenValidationWorkbenchComponent {
  private readonly golden = inject(GoldenValidationService);

  /** When supplied by Strategy Lab, lets a researcher designate that Python run. */
  readonly sourceRunId = input<number | null>(null);
  /** Lets a History host refresh its visible Golden badge after a saved decision. */
  readonly changed = output<GoldenValidation>();
  private readonly selectedId = signal<number | null>(null);
  private readonly latestResponse = signal<GoldenValidation | null>(null);
  private readonly designationCommandId = signal(this.commandId("designate"));
  private readonly reviewCommandId = signal(this.commandId("review"));
  private lastSourceRunId: number | null = null;
  protected readonly designationLabel = signal("");
  protected readonly designationRationale = signal("");
  protected readonly reviewDecision = signal<GoldenReviewDecision>("accept");
  protected readonly reviewReason = signal("");
  protected readonly quantConnectBacktestId = signal("");
  protected readonly authorizedProgramVersion = signal("");
  protected readonly busy = signal(false);
  protected readonly message = signal<string | null>(null);
  protected readonly error = signal<string | null>(null);

  protected readonly catalog = resource({
    loader: () => firstValueFrom(this.golden.list()),
  });

  protected readonly catalogCases = computed(() => {
    const catalog = this.catalog.hasValue() ? this.catalog.value() : [];
    const latest = this.latestResponse();
    return latest === null ? catalog : [latest, ...catalog.filter((entry) => entry.id !== latest.id)];
  });

  protected readonly selected = computed(() => {
    const cases = this.catalogCases();
    const sourceRunId = this.sourceRunId();
    if (sourceRunId !== null) {
      return cases.find((entry) => entry.source_run_id === sourceRunId) ?? null;
    }
    return cases.find((entry) => entry.id === this.selectedId()) ?? cases[0] ?? null;
  });

  protected readonly canDesignate = computed(() => this.sourceRunId() !== null && this.selected() === null);
  protected readonly parityEnvelope = computed(() => record(this.selected()?.parity_evidence["parity_verdict"]));
  protected readonly parityPayload = computed(() => {
    const envelope = this.parityEnvelope();
    return record(envelope?.["payload"]) ?? envelope;
  });
  protected readonly parityStatus = computed(() =>
    stringValue(this.parityEnvelope()?.["status"]) ?? stringValue(this.parityPayload()?.["status"]),
  );
  protected readonly parityReason = computed(() => stringValue(this.parityPayload()?.["reason"]));
  protected readonly parityLeftRunId = computed(() => numberValue(this.parityEnvelope()?.["left_run_id"]));
  protected readonly parityRightRunId = computed(() => numberValue(this.parityEnvelope()?.["right_run_id"]));
  protected readonly qualificationWarnings = computed(() => {
    const raw = this.selected()?.parity_evidence["qualification_warnings"];
    return Array.isArray(raw) ? raw.filter((entry): entry is string => typeof entry === "string") : [];
  });
  protected readonly parityChecks = computed<ParityCheckView[]>(() => {
    const payload = this.parityPayload();
    if (payload === null) return [];
    return [
      ["Native metrics", "native_metric_parity"],
      ["Readiness", "readiness_parity"],
      ["Inputs", "input_parity"],
      ["Parameters", "parameter_parity"],
      ["Program version", "program_version_parity"],
    ].flatMap(([label, key]) => {
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
      return category === null || message === null
        ? []
        : [{
          category,
          tradeNumber: numberValue(item?.["trade_number"]),
          atMs: numberValue(item?.["ms_utc"]),
          message,
        }];
    });
  });

  constructor() {
    effect(() => {
      const sourceRunId = this.sourceRunId();
      if (sourceRunId !== this.lastSourceRunId) {
        this.lastSourceRunId = sourceRunId;
        this.designationCommandId.set(this.commandId("designate"));
      }
    });
  }

  selectCase(entry: GoldenValidation): void {
    this.selectedId.set(entry.id);
    this.reviewDecision.set("accept");
    this.reviewReason.set("");
    this.quantConnectBacktestId.set("");
    this.authorizedProgramVersion.set("");
    this.reviewCommandId.set(this.commandId("review"));
    this.clearNotice();
  }

  setDesignationLabel(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.designationLabel.set(event.target.value);
      this.designationCommandId.set(this.commandId("designate"));
    }
  }

  setDesignationRationale(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) {
      this.designationRationale.set(event.target.value);
      this.designationCommandId.set(this.commandId("designate"));
    }
  }

  setReviewReason(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) {
      this.reviewReason.set(event.target.value);
      this.reviewCommandId.set(this.commandId("review"));
    }
  }

  setQuantConnectBacktestId(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.quantConnectBacktestId.set(event.target.value);
      this.reviewCommandId.set(this.commandId("review"));
    }
  }

  setAuthorizedProgramVersion(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.authorizedProgramVersion.set(event.target.value);
      this.reviewCommandId.set(this.commandId("review"));
    }
  }

  setReviewDecision(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    if (event.target.value === "accept" || event.target.value === "reject") {
      this.reviewDecision.set(event.target.value);
      this.reviewCommandId.set(this.commandId("review"));
    }
  }

  async designate(): Promise<void> {
    const sourceRunId = this.sourceRunId();
    const rationale = this.designationRationale().trim();
    if (sourceRunId === null || this.busy()) return;
    if (rationale.length < 3) {
      this.error.set("Explain why this is the selected validation baseline.");
      return;
    }
    this.busy.set(true);
    this.clearNotice();
    try {
      const result = await firstValueFrom(this.golden.designate({
        source_run_id: sourceRunId,
        command_id: this.designationCommandId(),
        ...(this.designationLabel().trim() ? { label: this.designationLabel().trim() } : {}),
        rationale,
      }));
      this.latestResponse.set(result);
      this.changed.emit(result);
      this.selectedId.set(result.id);
      this.designationLabel.set("");
      this.designationRationale.set("");
      this.designationCommandId.set(this.commandId("designate"));
      this.message.set("Validation Golden Run designated. Review the frozen case and current evidence below.");
      this.catalog.reload();
    } catch {
      this.error.set("This run could not be designated. Only completed Python baselines are eligible.");
    } finally {
      this.busy.set(false);
    }
  }

  async submitReview(): Promise<void> {
    const selected = this.selected();
    const reason = this.reviewReason().trim();
    if (selected === null || this.busy()) return;
    if (reason.length < 3) {
      this.error.set("A review note is required for either acceptance or rejection.");
      return;
    }
    const authorizedProgramVersion = this.authorizedProgramVersion().trim();
    if (
      this.reviewDecision() === "accept"
      && selected.validation_case.strategy.program_version === null
      && !authorizedProgramVersion
    ) {
      this.error.set("Enter the exact Signal Program version this historical run is authorized to represent.");
      return;
    }
    this.busy.set(true);
    this.clearNotice();
    try {
      const result = await firstValueFrom(this.golden.review(selected.id, {
        command_id: this.reviewCommandId(),
        expected_evidence_revision: selected.evidence_revision,
        decision: this.reviewDecision(),
        reason,
        ...(this.quantConnectBacktestId().trim()
          ? { quantconnect_backtest_id: this.quantConnectBacktestId().trim() }
          : {}),
        ...(authorizedProgramVersion ? { authorized_program_version: authorizedProgramVersion } : {}),
      }));
      this.latestResponse.set(result);
      this.changed.emit(result);
      this.reviewReason.set("");
      this.quantConnectBacktestId.set("");
      this.authorizedProgramVersion.set("");
      this.reviewCommandId.set(this.commandId("review"));
      this.message.set(
        result.latest_review?.decision === "accept"
          ? `Golden validation accepted as ${formatReceiptLabel(result.latest_review.classification ?? "manual_override")}.`
          : "Golden validation rejected. The computed evidence remains recorded.",
      );
      this.catalog.reload();
    } catch (caught: unknown) {
      if (caught instanceof HttpErrorResponse && caught.status === 409 && this.errorCode(caught) === "STALE_GOLDEN_VALIDATION_EVIDENCE") {
        this.latestResponse.set(null);
        this.reviewCommandId.set(this.commandId("review"));
        this.catalog.reload();
        this.error.set("The engine evidence changed. The latest dossier is loading; review it before submitting again.");
      } else {
        this.error.set("The review could not be saved. Your note and command are preserved for a safe retry.");
      }
    } finally {
      this.busy.set(false);
    }
  }

  protected json(value: unknown): string {
    return JSON.stringify(value, null, 2);
  }

  refreshEvidence(): void {
    this.latestResponse.set(null);
    this.clearNotice();
    this.catalog.reload();
  }

  private clearNotice(): void {
    this.message.set(null);
    this.error.set(null);
  }

  private commandId(action: string): string {
    return `golden-${action}-${crypto.randomUUID()}`;
  }

  private errorCode(error: HttpErrorResponse): string | null {
    return stringValue(record(record(error.error)?.["detail"])?.["code"]);
  }
}

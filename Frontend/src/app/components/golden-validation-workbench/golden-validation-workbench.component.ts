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
import { GoldenParityEvidenceComponent } from "./golden-parity-evidence.component";
import { GoldenValidationCaseComponent } from "./golden-validation-case.component";
import {
  GoldenValidationReviewComponent,
  type GoldenReviewDraft,
} from "./golden-validation-review.component";

/**
 * A focused research workbench for an immutable Validation Golden Run.
 * It deliberately keeps computed parity evidence and a human promotion
 * decision in separate panels; a manual override never changes the evidence.
 */
@Component({
  selector: "app-golden-validation-workbench",
  imports: [
    AssetIdentityComponent,
    GoldenParityEvidenceComponent,
    GoldenValidationCaseComponent,
    GoldenValidationReviewComponent,
    ReceiptLabelPipe,
  ],
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
  protected readonly reviewDraft = computed<GoldenReviewDraft>(() => ({
    decision: this.reviewDecision(),
    reason: this.reviewReason(),
    quantConnectBacktestId: this.quantConnectBacktestId(),
    authorizedProgramVersion: this.authorizedProgramVersion(),
  }));

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
    this.resetReviewDraft();
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

  setReviewDraft(draft: GoldenReviewDraft): void {
    this.reviewDecision.set(draft.decision);
    this.reviewReason.set(draft.reason);
    this.quantConnectBacktestId.set(draft.quantConnectBacktestId);
    this.authorizedProgramVersion.set(draft.authorizedProgramVersion);
    this.reviewCommandId.set(this.commandId("review"));
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
        ...(this.reviewDecision() === "accept" && authorizedProgramVersion
          ? { authorized_program_version: authorizedProgramVersion } : {}),
      }));
      this.latestResponse.set(result);
      this.changed.emit(result);
      this.resetReviewDraft();
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

  private resetReviewDraft(): void {
    this.reviewDecision.set("accept");
    this.reviewReason.set("");
    this.quantConnectBacktestId.set("");
    this.authorizedProgramVersion.set("");
    this.reviewCommandId.set(this.commandId("review"));
  }

  private errorCode(error: HttpErrorResponse): string | null {
    const payload = error.error;
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return null;
    const detail = payload["detail"];
    if (typeof detail !== "object" || detail === null || Array.isArray(detail)) return null;
    const code = detail["code"];
    return typeof code === "string" && code ? code : null;
  }
}

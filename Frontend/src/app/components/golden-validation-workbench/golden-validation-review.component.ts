import { ChangeDetectionStrategy, Component, input, output } from "@angular/core";

import type { GoldenReviewDecision, GoldenValidation } from "../../services/golden-validation.types";
import { ReceiptLabelPipe } from "../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayPipe } from "../../shared/timestamp";

export interface GoldenReviewDraft {
  decision: GoldenReviewDecision;
  reason: string;
  quantConnectBacktestId: string;
  authorizedProgramVersion: string;
}

@Component({
  selector: "app-golden-validation-review",
  imports: [ReceiptLabelPipe, TimestampDisplayPipe],
  templateUrl: "./golden-validation-review.component.html",
  styleUrl: "./golden-validation-review.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GoldenValidationReviewComponent {
  readonly golden = input.required<GoldenValidation>();
  readonly busy = input(false);
  readonly draft = input.required<GoldenReviewDraft>();
  readonly submitted = output<undefined>();
  readonly draftChanged = output<GoldenReviewDraft>();

  protected setDecision(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    if (event.target.value === "accept" || event.target.value === "reject") {
      this.updateDraft({
        decision: event.target.value,
        ...(event.target.value === "reject" ? { authorizedProgramVersion: "" } : {}),
      });
    }
  }

  protected setReason(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) {
      this.updateDraft({ reason: event.target.value });
    }
  }

  protected setQuantConnectBacktestId(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.updateDraft({ quantConnectBacktestId: event.target.value });
    }
  }

  protected setAuthorizedProgramVersion(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.updateDraft({ authorizedProgramVersion: event.target.value });
    }
  }

  protected submit(): void {
    this.submitted.emit(undefined);
  }

  protected requiresHistoricalAuthorization(): boolean {
    return this.golden().validation_case.strategy.program_version === null && this.draft().decision === "accept";
  }

  protected historicalRunIsRejected(): boolean {
    return this.golden().validation_case.strategy.program_version === null && this.draft().decision === "reject";
  }

  private updateDraft(changes: Partial<GoldenReviewDraft>): void {
    this.draftChanged.emit({ ...this.draft(), ...changes });
  }
}

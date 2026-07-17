import { ChangeDetectionStrategy, Component, computed, inject } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { PanelModule } from "primeng/panel";

import type { AccountReconciliationEvidenceRef } from "../../../api/account-reconciliation.types";
import { formatReceiptLabel, ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { AccountDeskGuidanceComponent } from "./account-desk-guidance.component";
import { AccountDeskSurfaceStore } from "./account-desk-surface-store.service";

interface ReconciliationEvidenceNote {
  readonly label: string;
  readonly message: string;
}

const RECONCILIATION_BLOCKER_COPY = new Map<string, string>([
  ['source_freshness_positions_stale', 'Position data is stale.'],
]);
const ACCOUNT_TRUTH_BLOCKER_SOURCE = 'account_truth.blocker';

function presentReconciliationEvidence(
  evidence: AccountReconciliationEvidenceRef,
): ReconciliationEvidenceNote {
  if (evidence.source === ACCOUNT_TRUTH_BLOCKER_SOURCE) {
    return {
      label: 'Position evidence',
      message: RECONCILIATION_BLOCKER_COPY.get(evidence.ref) ?? evidence.detail ?? 'A required position check is unavailable.',
    };
  }
  return {
    label: formatReceiptLabel(evidence.source),
    message: evidence.detail ?? 'Supporting reconciliation evidence is available.',
  };
}

/** Reconciliation and observation evidence projected without browser-side verdict derivation. */
@Component({
  selector: "app-account-desk-operator-proof",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskGuidanceComponent,
    ButtonModule,
    PanelModule,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: "./account-desk-operator-proof.component.html",
  styleUrl: "./account-desk-operator-proof.component.scss",
})
export class AccountDeskOperatorProofComponent {
  readonly store = inject(AccountDeskSurfaceStore);
  readonly recentVerificationHistory = computed(() =>
    this.store.triage()?.account_observation.history.slice(0, 3) ?? [],
  );
  readonly earlierVerificationHistory = computed(() =>
    this.store.triage()?.account_observation.history.slice(3) ?? [],
  );
  readonly reconciliationEvidenceNotes = computed(() =>
    this.store.triage()?.account_reconciliation_receipt?.evidence_refs
      .filter((evidence) => evidence.source !== 'account_truth')
      .map(presentReconciliationEvidence) ?? [],
  );

  retry(): void {
    this.store.retry();
  }
}

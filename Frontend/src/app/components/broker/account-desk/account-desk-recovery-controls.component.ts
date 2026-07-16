import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from "@angular/core";
import { ButtonModule } from "primeng/button";
import { CardModule } from "primeng/card";

import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { AccountDeskGuidanceComponent } from "./account-desk-guidance.component";
import { AccountDeskGuidanceStore } from "./account-desk-guidance-store.service";
import { AccountDeskRecoveryConfirmDialogComponent } from "./account-desk-recovery-confirm-dialog.component";
import { AccountDeskJournalCureComponent } from "./account-desk-journal-cure.component";
import { AccountDeskLegacyClaimCureComponent } from "./account-desk-legacy-claim-cure.component";
import { AccountDeskRecoveryStore } from "./account-desk-recovery-store.service";
import { AccountDeskSurfaceStore } from "./account-desk-surface-store.service";

/** Operator-only controls for backend-declared ordinary account recovery. */
@Component({
  selector: "app-account-desk-recovery-controls",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskGuidanceComponent,
    AccountDeskJournalCureComponent,
    AccountDeskLegacyClaimCureComponent,
    AccountDeskRecoveryConfirmDialogComponent,
    ButtonModule,
    CardModule,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: "./account-desk-recovery-controls.component.html",
  styleUrl: "./account-desk-recovery-controls.component.scss",
})
export class AccountDeskRecoveryControlsComponent {
  readonly surface = inject(AccountDeskSurfaceStore);
  readonly recovery = inject(AccountDeskRecoveryStore);
  private readonly guidance = inject(AccountDeskGuidanceStore);
  readonly cureBlockers = computed(() =>
    this.guidance.blockersFor("cure_tools", null, "operator"),
  );
  readonly hasActionableCure = computed(() =>
    this.cureBlockers().some((blocker) => blocker.primary_move !== null),
  );

  requestAutomationChange(): void {
    const policy = this.surface.triage()?.reconciliation_automation_policy;
    if (policy !== undefined) this.recovery.requestAutomationChange(policy);
  }
}

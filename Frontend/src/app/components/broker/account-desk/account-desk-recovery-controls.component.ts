import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from "@angular/core";
import { ButtonModule } from "primeng/button";
import { CardModule } from "primeng/card";

import { AccountDeskGuidanceComponent } from "./account-desk-guidance.component";
import { AccountDeskGuidanceStore } from "./account-desk-guidance-store.service";
import { AccountDeskRecoveryReceiptComponent } from "./account-desk-recovery-receipt.component";
import { AccountDeskJournalCureComponent } from "./account-desk-journal-cure.component";
import { AccountDeskLegacyClaimCureComponent } from "./account-desk-legacy-claim-cure.component";
import { AccountDeskRecoveryStore } from "./account-desk-recovery-store.service";
import { AccountDeskDirectoryStore } from "./account-desk-directory-store.service";
import { AccountDeskSurfaceStore } from "./account-desk-surface-store.service";

/** Operator-only controls for backend-declared ordinary account recovery. */
@Component({
  selector: "app-account-desk-recovery-controls",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskGuidanceComponent,
    AccountDeskJournalCureComponent,
    AccountDeskLegacyClaimCureComponent,
    AccountDeskRecoveryReceiptComponent,
    ButtonModule,
    CardModule,
  ],
  templateUrl: "./account-desk-recovery-controls.component.html",
  styleUrl: "./account-desk-recovery-controls.component.scss",
})
export class AccountDeskRecoveryControlsComponent {
  readonly surface = inject(AccountDeskSurfaceStore);
  readonly directory = inject(AccountDeskDirectoryStore, { optional: true });
  readonly recovery = inject(AccountDeskRecoveryStore);
  private readonly guidance = inject(AccountDeskGuidanceStore);
  readonly cureBlockers = computed(() =>
    this.guidance.blockersFor("cure_tools", null, "operator"),
  );
  readonly hasActionableCure = computed(() =>
    this.cureBlockers().some((blocker) => blocker.primary_move !== null),
  );
  readonly ordinaryCuresAvailable = computed(() =>
    this.directory === null || this.directory.cockpit()?.mode === 'NORMAL',
  );

  requestAutomationChange(): void {
    const policy = this.surface.triage()?.reconciliation_automation_policy;
    if (policy !== undefined) this.recovery.requestAutomationChange(policy);
  }

  requestEmergencyFlatten(): void {
    const confirmation = this.surface.triage()?.emergency_flatten_confirmation;
    if (confirmation !== undefined && confirmation !== null) {
      this.recovery.requestEmergencyFlatten(confirmation);
    }
  }
}

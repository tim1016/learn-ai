import { ChangeDetectionStrategy, Component } from "@angular/core";
import { CardModule } from "primeng/card";

import { AccountDeskOperatorEventsComponent } from "./account-desk-operator-events.component";
import { AccountDeskOperatorFleetComponent } from "./account-desk-operator-fleet.component";
import { AccountDeskOperatorProofComponent } from "./account-desk-operator-proof.component";
import { AccountDeskOperatorServiceComponent } from "./account-desk-operator-service.component";
import { AccountDeskRecoveryControlsComponent } from "./account-desk-recovery-controls.component";

/** Focused operator workspace, ordered around current proof and recovery. */
@Component({
  selector: "app-account-desk-operator-workspace",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskOperatorEventsComponent,
    AccountDeskOperatorFleetComponent,
    AccountDeskOperatorProofComponent,
    AccountDeskOperatorServiceComponent,
    AccountDeskRecoveryControlsComponent,
    CardModule,
  ],
  templateUrl: "./account-desk-operator-workspace.component.html",
  styleUrl: "./account-desk-operator-workspace.component.scss",
})
export class AccountDeskOperatorWorkspaceComponent {}

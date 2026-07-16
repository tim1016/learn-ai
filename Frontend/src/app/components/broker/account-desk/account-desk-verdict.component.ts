import { ChangeDetectionStrategy, Component, input, output } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { CardModule } from "primeng/card";
import { MessageModule } from "primeng/message";
import { TagModule } from "primeng/tag";

import type {
  AccountTriageResponse,
  AccountTriageVerdictMove,
} from "../../../api/account-reconciliation.types";
import type { AccountDeskLens } from "../../../api/operator-blocker.types";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { fmtCurrency, fmtSignedCurrency } from "../format";
import { accountPostureTagSeverity } from "../lib/account-posture-tag-severity";
import type { AccountDeskHeadlineMetrics } from "./account-desk-holdings-store.service";
import { AccountDeskGuidanceComponent } from "./account-desk-guidance.component";

/** Server-owned account verdict, freshness, and headline balance projection. */
@Component({
  selector: "app-account-desk-verdict",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskGuidanceComponent,
    ButtonModule,
    CardModule,
    MessageModule,
    ReceiptLabelPipe,
    TagModule,
    TimestampDisplayComponent,
  ],
  templateUrl: "./account-desk-verdict.component.html",
  styleUrl: "./account-desk-verdict.component.scss",
})
export class AccountDeskVerdictComponent {
  readonly triage = input<AccountTriageResponse | null>(null);
  readonly loading = input(false);
  readonly error = input<unknown>(null);
  readonly showingStaleLastGood = input(false);
  readonly headlineMetrics = input<AccountDeskHeadlineMetrics | null>(null);
  readonly freshnessCountdown = input<string | null>(null);
  readonly lens = input.required<AccountDeskLens>();
  readonly operatorAttentionCount = input(0);
  readonly retryRequested = output();
  readonly primaryMoveFollowed = output<AccountTriageVerdictMove>();
  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly postureSeverity = accountPostureTagSeverity;
}

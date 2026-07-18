import { ChangeDetectionStrategy, Component, input, output } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { TableModule } from "primeng/table";

import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import type { OperatorBlockerMoveEvent } from "../shared/operator-blocker-list/operator-blocker-list.component";
import { OperatorBlockerListComponent } from "../shared/operator-blocker-list/operator-blocker-list.component";
import { fmtCurrency, fmtSignedCurrency, fmtSignedQuantity } from "../format";
import type { AccountDeskHoldingRow } from "./account-desk-holdings-store.service";

/** Broker holdings table and row-specific guidance for either Account Desk lens. */
@Component({
  selector: "app-account-desk-holdings-table",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ButtonModule,
    OperatorBlockerListComponent,
    ReceiptLabelPipe,
    TableModule,
  ],
  templateUrl: "./account-desk-holdings-table.component.html",
})
export class AccountDeskHoldingsTableComponent {
  readonly rows = input.required<AccountDeskHoldingRow[]>();
  readonly blockerMoveSelected = output<OperatorBlockerMoveEvent>();
  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly fmtSignedQuantity = fmtSignedQuantity;

  trackHolding = (_: number, row: AccountDeskHoldingRow): number =>
    row.position.con_id;
}

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from "@angular/core";
import { Router } from "@angular/router";
import { ButtonModule } from "primeng/button";
import { TableModule } from "primeng/table";

import { TimestampDisplayComponent } from "../../../shared/timestamp";
import type { OperatorBlockerMoveEvent } from "../shared/operator-blocker-list/operator-blocker-list.component";
import { OperatorBlockerListComponent } from "../shared/operator-blocker-list/operator-blocker-list.component";
import { fmtCurrency, fmtSignedCurrency, fmtSignedQuantity } from "../format";
import {
  AccountDeskHoldingsStore,
  type AccountDeskHoldingRow,
} from "./account-desk-holdings-store.service";

/** Trader-facing holdings body for an already-attested Account Desk route. */
@Component({
  selector: "app-account-desk-trader-holdings",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ButtonModule,
    OperatorBlockerListComponent,
    TableModule,
    TimestampDisplayComponent,
  ],
  templateUrl: "./account-desk-trader-holdings.component.html",
  styleUrl: "./account-desk-trader-holdings.component.scss",
})
export class AccountDeskTraderHoldingsComponent {
  private readonly router = inject(Router);
  readonly store = inject(AccountDeskHoldingsStore);
  readonly tableRows = computed(() => [...this.store.rows()]);
  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly fmtSignedQuantity = fmtSignedQuantity;

  trackHolding = (_: number, row: AccountDeskHoldingRow): number =>
    row.position.con_id;

  retry(): void {
    this.store.retry();
  }

  followBlockerMove(event: OperatorBlockerMoveEvent): void {
    const action = event.move.action;
    if (action.kind !== "navigate") return;
    void this.router.navigate([action.route], {
      fragment: action.fragment ?? undefined,
    });
  }
}

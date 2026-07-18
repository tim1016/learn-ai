import { ChangeDetectionStrategy, Component, computed, inject, input } from "@angular/core";
import { Router } from "@angular/router";
import { ButtonModule } from "primeng/button";

import type { AccountDeskLens } from "../../../api/operator-blocker.types";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import type { OperatorBlockerMoveEvent } from "../shared/operator-blocker-list/operator-blocker-list.component";
import { fmtCurrency, fmtSignedCurrency } from "../format";
import { AccountDeskHoldingsStore } from "./account-desk-holdings-store.service";
import { AccountDeskTraderHoldingsTableComponent } from "./account-desk-trader-holdings-table.component";

/** Full, attested IBKR account snapshot for either Account Desk lens. */
@Component({
  selector: "app-account-desk-broker-snapshot",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AccountDeskTraderHoldingsTableComponent, ButtonModule, TimestampDisplayComponent],
  templateUrl: "./account-desk-broker-snapshot.component.html",
  styleUrl: "./account-desk-broker-snapshot.component.scss",
})
export class AccountDeskBrokerSnapshotComponent {
  private readonly router = inject(Router);
  readonly store = inject(AccountDeskHoldingsStore);
  readonly lens = input.required<AccountDeskLens>();
  readonly tableRows = computed(() => [...this.store.rowsForLens(this.lens())]);
  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;

  retry(): void {
    this.store.retry();
  }

  followBlockerMove(event: OperatorBlockerMoveEvent): void {
    const action = event.move.action;
    if (action.kind !== "navigate") return;
    void this.router.navigate([action.route], { fragment: action.fragment ?? undefined });
  }
}

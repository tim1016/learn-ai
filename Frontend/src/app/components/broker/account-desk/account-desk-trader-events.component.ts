import { ChangeDetectionStrategy, Component, inject } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { PanelModule } from "primeng/panel";

import type { AccountEventRow } from "../../../api/account-events.types";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { AccountDeskEventsStore } from "./account-desk-events-store.service";

/** Trader feed of only backend-narrated events in the server-owned NY day. */
@Component({
  selector: "app-account-desk-trader-events",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, PanelModule, TimestampDisplayComponent],
  templateUrl: "./account-desk-trader-events.component.html",
  styleUrl: "./account-desk-trader-events.component.scss",
})
export class AccountDeskTraderEventsComponent {
  readonly store = inject(AccountDeskEventsStore);

  trackEvent = (_: number, row: AccountEventRow): string => row.event_id;

  retry(): void {
    this.store.retry();
  }
}

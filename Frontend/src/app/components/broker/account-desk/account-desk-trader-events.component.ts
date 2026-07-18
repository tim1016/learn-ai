import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { PanelModule } from "primeng/panel";
import { Timeline } from "primeng/timeline";

import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { AccountDeskEventsStore } from "./account-desk-events-store.service";

const RECENT_EVENT_COUNT = 5;

/** Trader feed of only backend-narrated events in the server-owned NY day. */
@Component({
  selector: "app-account-desk-trader-events",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, PanelModule, ReceiptLabelPipe, TimestampDisplayComponent, Timeline],
  templateUrl: "./account-desk-trader-events.component.html",
  styleUrl: "./account-desk-trader-events.component.scss",
})
export class AccountDeskTraderEventsComponent {
  readonly store = inject(AccountDeskEventsStore);
  private readonly showAllState = signal(false);
  readonly showAll = this.showAllState.asReadonly();
  readonly timelineAccessibility = {
    host: { role: "list", "aria-label": "Today at the desk activity" },
    event: { role: "listitem" },
  };
  readonly timelineRows = computed(() =>
    this.store.traderRows().slice(0, this.showAll() ? undefined : RECENT_EVENT_COUNT),
  );
  readonly hiddenCount = computed(() =>
    Math.max(0, this.store.traderRows().length - RECENT_EVENT_COUNT),
  );

  retry(): void {
    this.store.retry();
  }

  toggleHistory(): void {
    this.showAllState.update((showAll) => !showAll);
  }
}

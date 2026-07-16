import { ChangeDetectionStrategy, Component, inject } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { PanelModule } from "primeng/panel";

import type {
  AccountEventKind,
  AccountEventRow,
} from "../../../api/account-events.types";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";
import { AccountDeskGuidanceComponent } from "./account-desk-guidance.component";
import { AccountDeskEventsStore } from "./account-desk-events-store.service";

const EVENT_KINDS: readonly AccountEventKind[] = [
  "activity",
  "safety",
  "reconciliation",
  "clerk",
  "configuration",
  "other",
];

/** Operations timeline for the full backend-classified account journal. */
@Component({
  selector: "app-account-desk-operator-events",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskGuidanceComponent,
    ButtonModule,
    PanelModule,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: "./account-desk-operator-events.component.html",
  styleUrl: "./account-desk-operator-events.component.scss",
})
export class AccountDeskOperatorEventsComponent {
  readonly store = inject(AccountDeskEventsStore);
  readonly eventKinds = EVENT_KINDS;

  trackEvent = (_: number, row: AccountEventRow): string => row.event_id;
  trackKind = (_: number, kind: AccountEventKind): AccountEventKind => kind;
  trackEvidence = (
    _: number,
    evidence: AccountEventRow["evidence_refs"][number],
  ): string => `${evidence.source}:${evidence.ref}`;

  selected(kind: AccountEventKind): boolean {
    return this.store.operationKinds().includes(kind);
  }

  toggleKind(kind: AccountEventKind): void {
    this.store.toggleOperationKind(kind);
  }

  retry(): void {
    this.store.retry();
  }

  loadOlder(): void {
    this.store.loadOlder();
  }
}

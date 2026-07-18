import { ChangeDetectionStrategy, Component, computed, inject } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { PanelModule } from "primeng/panel";
import { Timeline } from "primeng/timeline";

import type {
  AccountEventEvidenceRef,
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

interface AccountTimelineRow {
  readonly event: AccountEventRow;
  readonly evidence: AccountEventEvidenceRef[];
}

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
    Timeline,
  ],
  templateUrl: "./account-desk-operator-events.component.html",
  styleUrl: "./account-desk-operator-events.component.scss",
})
export class AccountDeskOperatorEventsComponent {
  readonly store = inject(AccountDeskEventsStore);
  readonly eventKinds = EVENT_KINDS;
  private readonly timelineRowsByEventId = new Map<string, AccountTimelineRow>();
  readonly timelineAccessibility = {
    host: { role: "list", "aria-label": "Journal timeline events" },
    event: { role: "listitem" },
  };
  readonly timelineRows = computed(() =>
    this.store.operationRows().map((event) => this.timelineRowFor(event)),
  );
  trackKind = (_: number, kind: AccountEventKind): AccountEventKind => kind;
  trackEvidence = (
    _: number,
    evidence: AccountEventEvidenceRef,
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

  private timelineRowFor(event: AccountEventRow): AccountTimelineRow {
    const cached = this.timelineRowsByEventId.get(event.event_id);
    if (cached !== undefined) return cached;

    const row = { event, evidence: event.evidence_refs };
    this.timelineRowsByEventId.set(event.event_id, row);
    return row;
  }
}

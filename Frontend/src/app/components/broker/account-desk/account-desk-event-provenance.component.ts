import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { AccountEventRow } from "../../../api/account-events.types";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";

/** Makes display provenance explicit without turning timeline order into proof. */
@Component({
  selector: "app-account-desk-event-provenance",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: "./account-desk-event-provenance.component.html",
  styleUrl: "./account-desk-event-provenance.component.scss",
})
export class AccountDeskEventProvenanceComponent {
  readonly event = input.required<AccountEventRow>();
}

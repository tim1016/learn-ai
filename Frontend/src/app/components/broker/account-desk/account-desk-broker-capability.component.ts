import { ChangeDetectionStrategy, Component, input, output } from "@angular/core";

import type {
  CapabilityTradeability,
  SessionCapability,
  SessionDataCapability,
  SessionKind,
} from "../../../api/broker-models";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { TimestampDisplayComponent } from "../../../shared/timestamp";

const SESSION_ORDER: readonly SessionKind[] = ["RTH", "PRE", "POST", "OVERNIGHT"];

/** Operator-only view of the selected account's IBKR session capability evidence. */
@Component({
  selector: "app-account-desk-broker-capability",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: "./account-desk-broker-capability.component.html",
  styleUrl: "./account-desk-broker-capability.component.scss",
})
export class AccountDeskBrokerCapabilityComponent {
  readonly snapshots = input<readonly SessionDataCapability[]>([]);
  readonly loading = input(false);
  readonly error = input<unknown>(null);
  readonly connected = input(false);
  readonly probeRequested = output();
  readonly sessions = SESSION_ORDER;

  capability(snapshot: SessionDataCapability, kind: SessionKind): SessionCapability {
    return snapshot.sessions[kind];
  }

  verdictLabel(capability: SessionCapability): string {
    const data = capability.data === "none" ? "no data" : capability.data.replace(/_/g, " ");
    return `${data} + ${this.tradeabilityLabel(capability.tradeable)}`;
  }

  evidenceCodes(capability: SessionCapability): string {
    return capability.evidence_codes.length > 0 ? capability.evidence_codes.join(", ") : "—";
  }

  private tradeabilityLabel(value: CapabilityTradeability): string {
    switch (value) {
      case "yes":
        return "tradeable";
      case "needs_enablement":
        return "enablement needed";
      case "no":
        return "not enabled";
    }
  }
}

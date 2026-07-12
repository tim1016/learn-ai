import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type {
  CapabilityTradeability,
  SessionCapability,
  SessionDataCapability,
  SessionKind,
} from '../../../api/broker-models';

const SESSION_ORDER: readonly SessionKind[] = ['RTH', 'PRE', 'POST', 'OVERNIGHT'];

@Component({
  selector: 'app-broker-capability-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent, ReceiptLabelPipe],
  styleUrl: './broker-capability-panel.component.scss',
  templateUrl: './broker-capability-panel.component.html',
})
export class BrokerCapabilityPanelComponent {
  readonly snapshots = input<readonly SessionDataCapability[]>([]);
  readonly loading = input(false);
  readonly error = input<unknown>(null);
  readonly connected = input(false);
  readonly probe = output();

  readonly sessions = SESSION_ORDER;

  protected session(snapshot: SessionDataCapability, kind: SessionKind): SessionCapability {
    return snapshot.sessions[kind];
  }

  protected verdictLabel(capability: SessionCapability): string {
    const data = capability.data === 'none' ? 'no data' : capability.data.replace(/_/g, ' ');
    const tradeable = this.tradeabilityLabel(capability.tradeable);
    return `${data} + ${tradeable}`;
  }

  protected evidenceCodes(capability: SessionCapability): string {
    return capability.evidence_codes.length > 0 ? capability.evidence_codes.join(', ') : '—';
  }

  private tradeabilityLabel(value: CapabilityTradeability): string {
    switch (value) {
      case 'yes':
        return 'tradeable';
      case 'needs_enablement':
        return 'enablement needed';
      case 'no':
        return 'not enabled';
    }
  }
}

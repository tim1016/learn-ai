import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { TagModule } from 'primeng/tag';

import type {
  BrokerSessionEvent,
  BrokerSessionEventCategory,
  BrokerSessionEventSeverity,
  BrokerSessionRosterRow,
} from '../../../api/broker-session-mirror.types';
import { fmtTimestampNy } from '../format';

type TagSeverity = 'success' | 'info' | 'warn' | 'danger' | 'secondary';

const CATEGORY_ORDER: BrokerSessionEventCategory[] = [
  'client_lifecycle',
  'link_connectivity',
  'recovery_reconnect',
  'data_farm',
  'auth_session',
  'order_execution',
  'pacing_throttling',
  'fault_client_error',
  'unclassified',
];

@Component({
  selector: 'app-broker-session-events-panel',
  imports: [CommonModule, TagModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-session-events-panel.component.html',
  styleUrl: './broker-session-events-panel.component.scss',
})
export class BrokerSessionEventsPanelComponent {
  readonly row = input.required<BrokerSessionRosterRow>();
  readonly events = input.required<readonly BrokerSessionEvent[]>();

  readonly eventCounts = computed(() =>
    CATEGORY_ORDER
      .map((category) => ({
        category,
        count: this.row().event_counts[category] ?? 0,
      }))
      .filter((entry) => entry.count > 0),
  );

  categoryLabel(category: BrokerSessionEventCategory): string {
    switch (category) {
      case 'client_lifecycle':
        return 'Client lifecycle';
      case 'link_connectivity':
        return 'Link connectivity';
      case 'recovery_reconnect':
        return 'Recovery/reconnect';
      case 'data_farm':
        return 'Data farm';
      case 'auth_session':
        return 'Auth/session';
      case 'order_execution':
        return 'Order & execution';
      case 'pacing_throttling':
        return 'Pacing';
      case 'fault_client_error':
        return 'Fault/client-error';
      case 'unclassified':
        return 'Unclassified';
    }
  }

  severityTag(severity: BrokerSessionEventSeverity): TagSeverity {
    switch (severity) {
      case 'info':
        return 'info';
      case 'warning':
        return 'warn';
      case 'critical':
        return 'danger';
    }
  }

  formatTimestamp(value: number): string {
    return fmtTimestampNy(value);
  }

  readonly trackByCategory = (
    _index: number,
    item: { category: BrokerSessionEventCategory },
  ): string => item.category;

  readonly trackByEventSeq = (_index: number, event: BrokerSessionEvent): number =>
    event.seq;
}

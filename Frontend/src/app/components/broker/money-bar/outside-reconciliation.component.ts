import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { AccountMoneyView } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-outside-reconciliation',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @for (row of rows(); track row.symbol) {
      <p role="status">
        <strong>{{ row.state === 'resolved' ? 'Outside fills reconciled.' : 'Outside fills unresolved.' }}</strong>
        {{ row.detail }}
        @if (row.next_action) { {{ row.next_action }} }
      </p>
    }
  `,
})
export class OutsideReconciliationComponent {
  readonly rows = input<AccountMoneyView['outside_reconciliations']>([]);
}

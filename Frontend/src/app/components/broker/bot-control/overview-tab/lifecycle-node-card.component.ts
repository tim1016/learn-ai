import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { LifecycleChartNode } from '../../../../api/live-instances.types';
import { NodeReceiptsListComponent } from '../node-receipts-list.component';

@Component({
  selector: 'app-lifecycle-node-card',
  imports: [NodeReceiptsListComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lifecycle-node-card.component.html',
  styleUrl: './lifecycle-node-card.component.scss',
})
export class LifecycleNodeCardComponent {
  readonly node = input.required<LifecycleChartNode>();
  readonly selected = input<boolean>(false);
  readonly highlighted = input<boolean>(false);
  readonly primary = input<boolean>(false);
  readonly blocking = input<boolean>(false);
  readonly receiptsExpanded = input<boolean>(false);
  readonly headingId = input.required<string>();
  readonly receiptRegionId = input.required<string>();

  readonly selectedRequested = output<LifecycleChartNode>();
  readonly subgraphRequested = output<LifecycleChartNode>();
  readonly receiptsToggled = output<LifecycleChartNode>();

  selectNode(): void {
    this.selectedRequested.emit(this.node());
  }

  openSubgraph(): void {
    this.subgraphRequested.emit(this.node());
  }

  toggleReceipts(): void {
    this.receiptsToggled.emit(this.node());
  }

  receiptToggleLabel(): string {
    const node = this.node();
    const action = this.receiptsExpanded() ? 'Hide' : 'Show';
    const callout = this.blocking() ? ' Blocking step.' : this.primary() ? ' Current step.' : '';
    return `${action} receipts for ${node.label}. Status: ${node.status_label}.${callout}`;
  }

  selectLabel(): string {
    const node = this.node();
    const callout = this.blocking() ? ' Blocking step.' : this.primary() ? ' Current step.' : '';
    return `Select ${node.label}. Status: ${node.status_label}.${callout}`;
  }

  openSubgraphLabel(): string {
    const node = this.node();
    return `Open ${node.label} details. Status: ${node.status_label}.`;
  }
}

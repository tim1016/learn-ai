import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LifecycleChartReceipt } from '../../../api/live-instances.types';
import { NodeReceiptsListComponent } from './node-receipts-list.component';

@Component({
  selector: 'app-node-receipts-pane',
  imports: [NodeReceiptsListComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './node-receipts-pane.component.html',
  styleUrl: './node-receipts-pane.component.scss',
})
export class NodeReceiptsPaneComponent {
  readonly receipts = input<LifecycleChartReceipt[]>([]);
}

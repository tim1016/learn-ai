import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { LifecycleChartNode, LiveInstanceStatus } from '../../../api/live-instances.types';
import { fmtTimestampNy } from '../format';
import { bucketHelp, nodeHelp } from './concept-help.registry';
import { NodeReceiptsPaneComponent } from './node-receipts-pane.component';
import {
  buildChangeForNextRunFields,
  type RedeploySettingField,
} from './node-inspector-presenter';

const ACTIONABILITY_BANNERS: Record<LifecycleChartNode['operator_actionability'], string> = {
  'operator-actionable': 'Operator action is required for this lifecycle step.',
  'system-only': 'Internal gate - no operator action needed; it can still block the bar.',
  'no-action-needed': 'No operator action is needed for this lifecycle step.',
};

@Component({
  selector: 'app-node-inspector',
  imports: [NodeReceiptsPaneComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './node-inspector.component.html',
  styleUrl: './node-inspector.component.scss',
})
export class NodeInspectorComponent {
  readonly node = input.required<LifecycleChartNode>();
  readonly status = input.required<LiveInstanceStatus>();
  readonly hasExplicitSelection = input<boolean>(false);

  readonly redeployRequested = output();

  readonly changeForNextRunFields = computed<RedeploySettingField[]>(
    () => buildChangeForNextRunFields(this.status()),
  );

  readonly bucketHelp = bucketHelp;
  readonly nodeHelp = nodeHelp;

  nodeCheckedAt(node: LifecycleChartNode): string | null {
    return node.ts_ms_resolved ? `Evidence checked ${fmtTimestampNy(node.ts_ms)}` : null;
  }

  trackRedeployField(_: number, field: RedeploySettingField): string {
    return field.id;
  }

  actionabilityBanner(node: LifecycleChartNode): string | null {
    return ACTIONABILITY_BANNERS[node.operator_actionability];
  }
}

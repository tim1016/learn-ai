import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { LifecycleChartNode, LiveInstanceStatus } from '../../../api/live-instances.types';
import { fmtTimestampNy } from '../format';
import { bucketHelp, nodeHelp } from './concept-help.registry';
import { NodeReceiptsPaneComponent } from './node-receipts-pane.component';
import {
  buildChangeForNextRunFields,
  buildDiagnosticEvidenceLines,
  buildProofLines,
  type DiagnosticEvidenceLine,
  type ProofLine,
  type RedeploySettingField,
} from './node-inspector-presenter';

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

  readonly advancedEvidenceLines = computed<DiagnosticEvidenceLine[]>(
    () => buildDiagnosticEvidenceLines(this.status().operator_surface.trader_guidance.advanced_evidence),
  );

  readonly changeForNextRunFields = computed<RedeploySettingField[]>(
    () => buildChangeForNextRunFields(this.status()),
  );

  readonly proofLines = computed<ProofLine[]>(() => buildProofLines(this.status()));

  readonly bucketHelp = bucketHelp;
  readonly nodeHelp = nodeHelp;

  nodeCheckedAt(node: LifecycleChartNode): string | null {
    return node.ts_ms_resolved ? `Evidence checked ${fmtTimestampNy(node.ts_ms)}` : null;
  }

  trackRedeployField(_: number, field: RedeploySettingField): string {
    return field.id;
  }

  trackProofLine(_: number, line: ProofLine): string {
    return line.id;
  }

  trackDiagnosticLine(_: number, line: DiagnosticEvidenceLine): string {
    return line.id;
  }
}

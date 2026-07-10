import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  LifecycleProjectionEventRow,
  OperatorSurfaceProofLine,
  OperatorSurface,
  TraderPrimaryRemediation,
} from '../../../../api/live-instances.types';
import {
  presentTraderRemediation,
  type PresentedAction,
} from '../lib/suggested-action-renderer';
import {
  buildDiagnosticEvidenceLines,
  type DiagnosticEvidenceLine,
} from '../node-inspector-presenter';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TraderGuidanceTimelineComponent } from './trader-guidance-timeline.component';

@Component({
  selector: 'app-trader-guidance-pane',
  imports: [CommonModule, ReceiptLabelPipe, TraderGuidanceTimelineComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trader-guidance-pane.component.html',
  styleUrl: './trader-guidance-pane.component.scss',
})
export class TraderGuidancePaneComponent {
  readonly surface = input.required<OperatorSurface>();
  readonly timelineRows = input<LifecycleProjectionEventRow[]>([]);
  readonly timelineProjectionAvailable = input<boolean>(false);
  readonly timelineCanonicalFallbackRequired = input<boolean>(true);
  readonly timelineNotice = input<string | null>(null);
  readonly primaryRemediationSelected = output<TraderPrimaryRemediation>();

  readonly submitReadiness = computed(() => this.surface().submit_readiness);
  readonly traderGuidance = computed(() => this.surface().trader_guidance);
  readonly accountOwner = computed(() => this.surface().account_owner);
  readonly proofLines = computed(() => this.traderGuidance().proof_lines);
  readonly attentionGroups = computed(() => this.traderGuidance().additional_attention_groups);
  readonly diagnosticEvidence = computed<DiagnosticEvidenceLine[]>(() =>
    buildDiagnosticEvidenceLines(this.traderGuidance().advanced_evidence),
  );
  readonly renderedPrimary = computed<PresentedAction | null>(() =>
    presentTraderRemediation(this.traderGuidance().primary_remediation),
  );

  trackEvidence(index: number, line: DiagnosticEvidenceLine): string {
    return `${line.id}:${index}`;
  }

  trackAttention(index: number, group: { code: string }): string {
    return `${group.code}:${index}`;
  }

  trackProofLine(index: number, line: OperatorSurfaceProofLine): string {
    return `${line.id}:${index}`;
  }

  emitCurrentRemediation(): void {
    const remediation = this.traderGuidance().primary_remediation;
    if (remediation.kind === 'none') return;
    this.primaryRemediationSelected.emit(remediation);
  }
}

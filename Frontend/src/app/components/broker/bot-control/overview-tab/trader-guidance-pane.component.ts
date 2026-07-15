import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type {
  OperatorSurfaceProofLine,
  OperatorSurface,
} from '../../../../api/live-instances.types';
import {
  buildDiagnosticEvidenceLines,
  type DiagnosticEvidenceLine,
} from '../diagnostic-evidence-lines';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

@Component({
  selector: 'app-trader-guidance-pane',
  imports: [CommonModule, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trader-guidance-pane.component.html',
  styleUrl: './trader-guidance-pane.component.scss',
})
export class TraderGuidancePaneComponent {
  readonly surface = input.required<OperatorSurface>();

  readonly submitReadiness = computed(() => this.surface().submit_readiness);
  readonly traderGuidance = computed(() => this.surface().trader_guidance);
  readonly accountClerk = computed(() => this.surface().account_clerk);
  readonly proofLines = computed(() => this.traderGuidance().proof_lines);
  readonly attentionGroups = computed(() => this.traderGuidance().additional_attention_groups);
  readonly diagnosticEvidence = computed<DiagnosticEvidenceLine[]>(() =>
    buildDiagnosticEvidenceLines(this.traderGuidance().advanced_evidence),
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

}

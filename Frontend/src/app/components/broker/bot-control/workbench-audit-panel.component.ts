import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { IbkrApiEvidencePanelComponent } from './reused/ibkr-api-evidence-panel/ibkr-api-evidence-panel.component';
import {
  buildDiagnosticEvidenceLines,
  buildProofLines,
  type DiagnosticEvidenceLine,
  type ProofLine,
} from './node-inspector-presenter';

@Component({
  selector: 'app-workbench-audit-panel',
  imports: [IbkrApiEvidencePanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './workbench-audit-panel.component.html',
  styleUrl: './workbench-audit-panel.component.scss',
})
export class WorkbenchAuditPanelComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly provenance = computed(() => this.status().provenance);
  readonly proofLines = computed<ProofLine[]>(() => buildProofLines(this.status()));
  readonly diagnosticEvidenceLines = computed<DiagnosticEvidenceLine[]>(
    () => buildDiagnosticEvidenceLines(this.status().operator_surface.trader_guidance.advanced_evidence),
  );

  copy(value: string | null | undefined): void {
    if (!value) return;
    void navigator.clipboard?.writeText(value).catch(() => undefined);
  }

  formatTimestamp(ms: number | null | undefined): string {
    if (ms == null) return '-';
    return new Date(ms).toISOString();
  }

  runtimeConfigJson(): string {
    const provenance = this.provenance();
    if (!provenance) return '{}';
    return JSON.stringify(provenance.live_config ?? {}, null, 2);
  }

  trackProofLine(_: number, line: ProofLine): string {
    return line.id;
  }

  trackDiagnosticLine(_: number, line: DiagnosticEvidenceLine): string {
    return line.id;
  }
}

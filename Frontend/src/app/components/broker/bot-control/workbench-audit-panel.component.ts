import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { IbkrApiEvidencePanelComponent } from '../cockpit-v2/reused/ibkr-api-evidence-panel/ibkr-api-evidence-panel.component';

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
}

// PRD #617 — Audit tab.  Provenance fields with copy buttons; the
// ONLY canonical render site for Mark Poisoned (typed-HALT
// confirmation lives on the cockpit shell).

import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import { actionTooltip, disabledReasonCopy } from '../lib/disabled-reason-copy';
import { IbkrApiEvidencePanelComponent } from '../reused/ibkr-api-evidence-panel/ibkr-api-evidence-panel.component';

@Component({
  selector: 'app-audit-tab',
  standalone: true,
  imports: [CommonModule, IbkrApiEvidencePanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './audit-tab.component.html',
  styleUrl: './audit-tab.component.scss',
})
export class AuditTabComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly busyAction = input<string | null>(null);
  readonly markPoisonedRequested = output();

  readonly provenance = computed(() => this.status().provenance);
  readonly markPoisoned = computed(
    () => this.status().operator_surface.actions.mark_poisoned,
  );

  copy(value: string | null | undefined): void {
    if (!value) return;
    void navigator.clipboard?.writeText(value).catch(() => undefined);
  }

  formatTimestamp(ms: number | null | undefined): string {
    if (ms == null) return '—';
    return new Date(ms).toISOString();
  }

  runtimeConfigJson(): string {
    const p = this.status().provenance;
    if (!p) return '{}';
    return JSON.stringify(p.live_config ?? {}, null, 2);
  }

  requestMarkPoisoned(): void {
    if (!this.markPoisoned().enabled || this.busyAction()) return;
    this.markPoisonedRequested.emit();
  }

  /** Operator-language tooltip for the Mark POISONED trigger. The
   *  shared copy map (ADR-0013 §4 — closed-enum presentation copy)
   *  resolves the server-authored reason code; the trigger never
   *  shows the raw enum to the operator. */
  markPoisonedTooltip(): string {
    const cap = this.markPoisoned();
    return actionTooltip({
      enabled: cap.enabled,
      serverReasonCode: cap.disabled_reason_code,
      localTransportStale: false,
      busy: this.busyAction() !== null,
      fallbackLabel: 'Mark this run poisoned',
    });
  }

  /** Operator-language line under the trigger when the action is
   *  disabled with a server-authored reason. Returns ``null`` when
   *  there is no reason to show. */
  markPoisonedDisabledLine(): string | null {
    const cap = this.markPoisoned();
    if (cap.enabled) return null;
    return disabledReasonCopy(cap.disabled_reason_code);
  }
}

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

@Component({
  selector: 'app-audit-tab',
  standalone: true,
  imports: [CommonModule],
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
}

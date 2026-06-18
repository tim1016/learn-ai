import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { ReadinessVector } from '../../../../api/live-instances.types';

interface FailedGateRow {
  key: string;
  label: string;
  severity: 'hard' | 'soft';
  detail: string;
}

/**
 * "Can It Trade?" — the operator-priority verdict surface that answers the
 * single question the trader has every time they open the page.
 *
 * Issue #583 (renamed from ReadinessCardComponent). Layouts:
 *   - READY → a calm one-line "READY · N checks pass" bar.
 *   - BLOCKED / DEGRADED / UNKNOWN → a tall card with the verdict, the
 *     X / N proportional count, and a list of the failing gates.
 *
 * Maps gate.name → operator-language label via the shared map the parent
 * owns; falls back to the raw name on miss.
 */
@Component({
  selector: 'app-can-it-trade-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './can-it-trade-card.component.html',
  styleUrl: './can-it-trade-card.component.scss',
  host: {
    '[attr.data-verdict]': 'verdictAttr()',
  },
})
export class CanItTradeCardComponent {
  readonly readiness = input.required<ReadinessVector | null>();
  /** Operator-language labels for known gates. The parent owns the map
   * (Pre-Trade Checklist uses the same source); this card receives it
   * as an input so the two surfaces stay in lockstep. */
  readonly gateLabels = input.required<Record<string, string>>();

  readonly verdict = computed<'READY' | 'BLOCKED' | 'DEGRADED' | 'UNKNOWN' | 'NO_READINESS'>(
    () => this.readiness()?.verdict ?? 'NO_READINESS',
  );

  readonly gateCount = computed<number>(() => this.readiness()?.gates.length ?? 0);

  readonly passingCount = computed<number>(
    () => this.readiness()?.gates.filter((g) => g.status === 'pass').length ?? 0,
  );

  readonly failingGates = computed<FailedGateRow[]>(() => {
    const r = this.readiness();
    if (!r) return [];
    const labels = this.gateLabels();
    return r.gates
      .filter((g) => g.status !== 'pass')
      .map<FailedGateRow>((g) => ({
        key: g.name,
        label: labels[g.name] ?? g.name,
        severity: g.severity,
        detail: g.detail,
      }));
  });

  readonly verdictAttr = computed<'ready' | 'degraded' | 'blocked' | 'unknown'>(() => {
    switch (this.verdict()) {
      case 'READY':
        return 'ready';
      case 'BLOCKED':
        return 'blocked';
      case 'DEGRADED':
        return 'degraded';
      default:
        return 'unknown';
    }
  });

  readonly verdictTone = computed<'ok' | 'warn' | 'bad' | 'unknown'>(() => {
    switch (this.verdict()) {
      case 'READY':
        return 'ok';
      case 'BLOCKED':
        return 'bad';
      case 'DEGRADED':
      case 'UNKNOWN':
        return 'warn';
      default:
        return 'unknown';
    }
  });

  readonly proportionLabel = computed<string>(() => {
    const total = this.gateCount();
    const pass = this.passingCount();
    if (total === 0) return 'No readiness gates reported by the engine';
    return `${pass} / ${total} checks pass`;
  });
}

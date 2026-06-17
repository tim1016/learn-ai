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
 * Issue #565 PR 11.
 *
 * Layouts:
 *   - READY → a calm one-line "READY · N checks pass" bar (User Story #14,
 *     so the page doesn't shout green-on-green).
 *   - BLOCKED / DEGRADED / UNKNOWN → a tall card with the verdict, the
 *     X / N proportional count (User Story #57), and a list of the failing
 *     gates so the operator can act.
 *
 * Affordances per failing gate (User Stories #11–13: button / nav-link /
 * read-only note) stay rendered by the parent's existing Pre-Trade
 * Checklist for now — the affordance taxonomy is intertwined with parent
 * state (busyAction, expandedGate, runFix). This card is the verdict
 * surface above that checklist; full extraction of the affordance logic
 * is tracked as a follow-up after the sticky control bar lands.
 *
 * Maps gate.name → operator-language label via the same shared map the
 * Pre-Trade Checklist uses (the parent already documents these in
 * GATE_LABELS); when an unknown gate.name slips through, it falls back to
 * the raw name so the surface degrades to "still readable" rather than
 * silently dropping data.
 */
@Component({
  selector: 'app-readiness-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './readiness-card.component.html',
  styleUrl: './readiness-card.component.scss',
})
export class ReadinessCardComponent {
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

import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type {
  DecisionColumnDescriptor,
  LatestSignalTone,
} from '../../../../../api/live-instances.types';

interface SignalCell {
  name: string;
  label: string;
  semantic: string | null;
  value: string;
}

/**
 * "Latest Signal" strip — a slim strip rendered directly below the trade
 * chart's time axis showing the latest signal value and the descriptor-backed
 * decision fields the engine already emits.
 *
 * Issue #565 PR 8 (User Story #23). The signal pill leads (ENTER / EXIT /
 * HOLD / etc.), followed by the per-column descriptor cells in the order the
 * backend declares them. Reasoning + next-evaluation timestamp are deferred
 * to a future contract change (User Story #24).
 *
 * Reads only backend-authored fields from the status contract
 * (decision_columns, latest_decision, latest_signal_tone). No frontend signal
 * classification.
 */
@Component({
  selector: 'app-latest-signal-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './latest-signal-strip.component.html',
  styleUrl: './latest-signal-strip.component.scss',
})
export class LatestSignalStripComponent {
  readonly decisionColumns = input.required<DecisionColumnDescriptor[]>();
  readonly latestDecision = input.required<Record<string, unknown> | null>();
  readonly signalTone = input.required<LatestSignalTone>();

  readonly signal = computed<string | null>(() => {
    const value = this.latestDecision()?.['signal'];
    return typeof value === 'string' ? value : null;
  });

  readonly cells = computed<SignalCell[]>(() => {
    const cols = this.decisionColumns();
    const decision = this.latestDecision();
    return cols
      .filter((c) => c.name !== 'signal')
      .map<SignalCell>((col) => ({
        name: col.name,
        label: col.label,
        semantic: col.semantic ?? null,
        value: formatCell(decision, col),
      }));
  });

  readonly hasContent = computed<boolean>(() => {
    return this.decisionColumns().length > 0 || this.signal() !== null;
  });
}

function formatCell(
  decision: Record<string, unknown> | null,
  col: DecisionColumnDescriptor,
): string {
  const value = decision?.[col.name];
  if (value === null || value === undefined) return '—';
  if (col.format === 'decimal' && typeof value === 'number') return value.toFixed(2);
  return String(value);
}

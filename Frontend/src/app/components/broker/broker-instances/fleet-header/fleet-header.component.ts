import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import type { FleetContamination } from '../../../../api/live-instances.types';
import type { OperationError } from '../../operation-error';
import { BrokerOperationResultComponent } from '../../broker-operation-result/broker-operation-result.component';

/**
 * Account / Fleet status disclosure (PRD #607 cockpit revision
 * 2026-06-21).
 *
 * Collapsed one-line summary:
 *
 *   ACCOUNT · DU1234 · PAPER · ✓ CLEAN · 137 SPY accounted for       [Safety actions ▾]
 *
 * Verdict-driven collapse semantics (Option A):
 *
 *  - ``verdict === 'clean'``     -> collapsed by default; the operator
 *                                    may expand via the toggle.
 *  - ``verdict === 'contaminated'`` -> EXPANDED, no toggle.  Contamination
 *                                       is an attention state that the
 *                                       page-wide collapse rule forbids
 *                                       hiding behind a default-collapsed
 *                                       disclosure.
 *  - ``verdict === 'unknown'``   -> EXPANDED, no toggle.
 *
 * Uses the cockpit's own ``collapsible-card`` + ``card-verdict-border``
 * pattern (grid-template-rows 0fr/1fr).  NOT PrimeNG p-panel; NOT raw
 * ``<details>`` — the toggle is a real button with ``aria-expanded`` and
 * ``aria-controls`` so screen-reader semantics match the other cockpit
 * cards.
 */
@Component({
  selector: 'app-fleet-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './fleet-header.component.html',
  styleUrl: './fleet-header.component.scss',
  imports: [BrokerOperationResultComponent],
  host: {
    '[attr.data-verdict]': 'verdictAttr()',
    '[attr.data-collapsed]': 'collapsedAttr()',
  },
})
export class FleetHeaderComponent {
  readonly account = input<FleetContamination | null>(null);
  readonly selectedInstanceId = input<string | null>(null);
  readonly busyEmergencyFlatten = input<boolean>(false);
  readonly commandError = input<OperationError | null>(null);
  readonly accountId = input<string | null>(null);
  readonly isPaper = input<boolean>(false);

  readonly emergencyFlattenRequested = output();

  protected readonly hasSelection = computed<boolean>(
    () => this.selectedInstanceId() !== null,
  );

  protected readonly verdict = computed<'clean' | 'contaminated' | 'unknown'>(() => {
    const acct = this.account();
    return acct?.verdict ?? 'unknown';
  });

  /** Attention states cannot be manually collapsed. */
  protected readonly isAttentionVerdict = computed<boolean>(
    () => this.verdict() !== 'clean',
  );

  /** Single-boolean operator override that's only honored in the calm
   *  (``clean``) state; verdict-tightening forces the card open. */
  private readonly _manuallyExpanded = signal<boolean>(false);

  protected toggle(): void {
    if (this.isAttentionVerdict()) return;
    this._manuallyExpanded.update((v) => !v);
  }

  protected readonly expanded = computed<boolean>(
    () => this.isAttentionVerdict() || this._manuallyExpanded(),
  );

  protected readonly collapsedAttr = computed<'true' | 'false'>(() =>
    this.expanded() ? 'false' : 'true',
  );

  protected readonly verdictAttr = computed<'ready' | 'degraded' | 'unknown'>(() => {
    switch (this.verdict()) {
      case 'clean':
        return 'ready';
      case 'contaminated':
        return 'degraded';
      default:
        return 'unknown';
    }
  });

  protected readonly accountedSummary = computed<string>(() => {
    const acct = this.account();
    if (!acct || !acct.net_positions) return '0 positions tracked';
    const symbols = Object.entries(acct.explained_total).filter(([, qty]) => qty !== 0);
    if (symbols.length === 0) return '0 positions tracked';
    return symbols
      .map(([sym, qty]) => `${qty} ${sym}`)
      .join(' · ');
  });

  protected readonly verdictLabel = computed<string>(() => {
    switch (this.verdict()) {
      case 'clean':
        return '✓ CLEAN';
      case 'contaminated':
        return '⚠ CONTAMINATED';
      default:
        return '? UNKNOWN';
    }
  });

  protected readonly modeLabel = computed<string>(() =>
    this.isPaper() ? 'PAPER' : 'LIVE',
  );

  protected residualRows(acct: FleetContamination): { symbol: string; qty: number }[] {
    return Object.entries(acct.residual ?? {}).map(([symbol, qty]) => ({ symbol, qty }));
  }

  protected requestEmergencyFlatten(): void {
    this.emergencyFlattenRequested.emit();
  }
}

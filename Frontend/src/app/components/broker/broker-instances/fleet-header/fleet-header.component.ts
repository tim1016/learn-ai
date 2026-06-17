import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';
import type { FleetContamination } from '../../../../api/live-instances.types';
import type { OperationError } from '../../operation-error';
import { BrokerConnectivityService } from '../../../../services/broker-connectivity.service';
import { BrokerConnectivityStripComponent } from '../../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../../broker-operation-result/broker-operation-result.component';

/**
 * Non-sticky fleet header for the trader-first Bot Control Panel (#565 PR 2).
 *
 * Composes the existing ``BrokerConnectivityStripComponent`` and the Account
 * Status card content that previously lived inline in
 * ``BrokerInstancesComponent``. Adds:
 *
 *  - a Platform Update banner that surfaces ``daemonFreshness().state ===
 *    'stale'`` in trader-vocabulary copy (the technical "Engine code: Stale"
 *    detail still renders inside the strip below, per the operator-default-
 *    plus-engineer-detail pattern from the PRD);
 *  - an Account Safety Actions disclosure that hosts ``Emergency Flatten
 *    Account``, moved here from the per-bot Advanced section so the action
 *    is owned by the fleet rather than scattered across per-bot tabs.
 *
 * The typed-confirmation gate (operator must echo their IBKR account id) is
 * preserved by leaving the prompt-and-call flow in the parent — this
 * component only emits ``emergencyFlattenRequested`` and surfaces busy /
 * error state from the parent.
 */
@Component({
  selector: 'app-fleet-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './fleet-header.component.html',
  styleUrl: './fleet-header.component.scss',
  imports: [BrokerConnectivityStripComponent, BrokerOperationResultComponent],
})
export class FleetHeaderComponent {
  private readonly connectivity = inject(BrokerConnectivityService);

  readonly account = input<FleetContamination | null>(null);
  readonly selectedInstanceId = input<string | null>(null);
  readonly busyEmergencyFlatten = input<boolean>(false);
  readonly commandError = input<OperationError | null>(null);

  readonly emergencyFlattenRequested = output();

  /** Surfaces the existing strip-level "stale engine code" check as a
   * trader-vocabulary banner. The strip below continues to render the
   * technical detail (commits-behind, copy-restart-command); the banner
   * just answers "is there an update I should know about?" in plain
   * English. */
  protected readonly platformUpdateAvailable = computed<boolean>(
    () => this.connectivity.daemonFreshness().state === 'stale',
  );

  protected readonly hasSelection = computed<boolean>(() => this.selectedInstanceId() !== null);

  protected accountBadge(acct: FleetContamination): string {
    if (acct.verdict === 'clean') return 'ALL POSITIONS ACCOUNTED FOR';
    if (acct.verdict === 'contaminated') return 'UNRECOGNIZED POSITIONS DETECTED';
    return 'ACCOUNT STATUS UNKNOWN';
  }

  protected residualRows(acct: FleetContamination): { symbol: string; qty: number }[] {
    return Object.entries(acct.residual ?? {}).map(([symbol, qty]) => ({ symbol, qty }));
  }

  protected requestEmergencyFlatten(): void {
    this.emergencyFlattenRequested.emit();
  }
}

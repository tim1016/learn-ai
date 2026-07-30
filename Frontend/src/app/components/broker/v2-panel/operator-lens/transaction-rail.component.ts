import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import type { PanelProfile, StationView, TransactionRail } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Transaction rail (spec §7.1).
 *
 * Renders one selected transaction's six stations. Five states each as
 * icon + text + color — never color alone (AXE / WCAG AA). Station receipts
 * expand inline; evidence links emit `evidenceRequested` so the parent
 * operator lens can open the evidence drawer.
 *
 * Not-applicable stations come from the capability profile (§4), not from
 * local logic. When `profile` is null the component still renders all
 * stations in `unknown_stale` state — the shell should not render the lens
 * until the profile is loaded.
 *
 * <!-- card-help anchor: station-1-signal — wired to S5 drawer in end-phase integration -->
 * <!-- card-help anchor: station-3-submit-gate — wired to S5 drawer in end-phase integration -->
 */
@Component({
  selector: 'app-transaction-rail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './transaction-rail.component.html',
  styleUrl: './transaction-rail.component.scss',
})
export class TransactionRailComponent {
  readonly rail = input.required<TransactionRail>();
  readonly profile = input<PanelProfile | null>(null);

  /** Emits the order_ref whose evidence link was clicked. */
  readonly evidenceRequested = output<string>();

  protected readonly stations = computed(() => this.rail().stations);
  protected readonly transactionRef = computed(() => this.rail().transaction_ref);
  protected readonly hasTransaction = computed(() => this.transactionRef() !== null);

  protected stationIcon(state: string): string {
    switch (state) {
      case 'satisfied': return '✓';
      case 'waiting': return '⏳';
      case 'blocked': return '⚠';
      case 'unknown_stale': return '?';
      case 'not_applicable': return '—';
      default: return '?';
    }
  }

  protected stationAriaLabel(station: StationView): string {
    return `${station.label}: ${station.state_label}`;
  }

  protected onEvidenceClick(_station: StationView): void {
    // Prefer the transaction_ref on the rail; fall back to nothing if absent.
    const ref = this.transactionRef();
    if (ref) {
      this.evidenceRequested.emit(ref);
    }
  }

  /** Tracks by station_id — stable across polls. */
  protected trackStation(_index: number, station: StationView): string {
    return station.station_id;
  }
}

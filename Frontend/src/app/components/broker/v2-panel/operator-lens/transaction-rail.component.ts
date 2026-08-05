import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';
import type {
  PanelProfile,
  StationState,
  StationView,
  TransactionRail,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { TransactionEvidenceTimelineComponent } from './transaction-evidence-timeline.component';

/**
 * Transaction rail (spec §7.1).
 *
 * Renders one selected transaction's six stations. Five states each as
 * icon + text + color — never color alone (AXE / WCAG AA). Station receipts
 * expand inline; raw evidence is fetched only when requested and remains in
 * the station accordion as a formatted timeline.
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
  imports: [TimestampDisplayComponent, TransactionEvidenceTimelineComponent],
  templateUrl: './transaction-rail.component.html',
  styleUrl: './transaction-rail.component.scss',
})
export class TransactionRailComponent {
  readonly rail = input.required<TransactionRail>();
  readonly profile = input<PanelProfile | null>(null);
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();

  protected readonly evidenceStationId = signal<string | null>(null);

  protected readonly stations = computed(() => this.rail().stations);
  protected readonly transactionRef = computed(() => this.rail().transaction_ref);
  protected readonly hasTransaction = computed(() => this.transactionRef() !== null);

  protected readonly stationIcon: Record<StationState, string> = {
    satisfied: '✓',
    waiting: '⏳',
    blocked: '⚠',
    unknown_stale: '?',
    not_applicable: '—',
  };

  protected stationAriaLabel(station: StationView): string {
    return `${station.label}: ${station.state_label}`;
  }

  /** Waiting, blocked, and stale stations start open so exceptions cannot hide. */
  protected needsAttention(station: StationView): boolean {
    return station.state === 'waiting'
      || station.state === 'blocked'
      || station.state === 'unknown_stale';
  }

  protected onEvidenceClick(station: StationView): void {
    this.evidenceStationId.update((current) =>
      current === station.station_id ? null : station.station_id,
    );
  }

  /** Tracks by station_id — stable across polls. */
  protected trackStation(_index: number, station: StationView): string {
    return station.station_id;
  }
}

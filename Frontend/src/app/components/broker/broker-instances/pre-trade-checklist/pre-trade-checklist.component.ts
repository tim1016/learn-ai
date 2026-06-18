import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import type { ReadinessGate } from '../../../../api/live-instances.types';

import type { FleetState } from '../sticky-control-bar/fleet-state';

/**
 * Pre-trade checklist — floating action button (FAB) + dialog. Issue #587.
 *
 * The FAB is hidden in STEADY state (nothing to triage), visible otherwise.
 * Clicking the FAB opens a dialog listing each failing readiness gate as a
 * checklist item the operator can acknowledge. Acknowledgement is local
 * UI state — it doesn't change the underlying engine verdict.
 */
@Component({
  selector: 'app-pre-trade-checklist',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './pre-trade-checklist.component.html',
  styleUrl: './pre-trade-checklist.component.scss',
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
})
export class PreTradeChecklistComponent {
  readonly fleetState = input.required<FleetState>();
  readonly gates = input.required<ReadinessGate[]>();

  readonly fabVisible = computed<boolean>(() => this.fleetState() !== 'STEADY');

  private readonly _open = signal(false);
  readonly open = this._open.asReadonly();

  private readonly _acknowledged = signal<ReadonlySet<string>>(new Set<string>());
  readonly acknowledged = this._acknowledged.asReadonly();

  readonly failingGates = computed<ReadinessGate[]>(() =>
    this.gates().filter((g) => g.status !== 'pass'),
  );

  toggleOpen(): void {
    this._open.update((v) => !v);
  }

  close(): void {
    this._open.set(false);
  }

  acknowledge(gateName: string): void {
    this._acknowledged.update((set) => new Set([...set, gateName]));
  }

  onEscape(): void {
    if (this._open()) this.close();
  }
}

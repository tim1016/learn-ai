import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type {
  AlpacaDeskState,
  BrokerInstallationSelection,
} from '../../../../api/alpaca.types';

/**
 * The visible Stage → Apply → Restart tracker (task 2026-09-12, phase 4).
 *
 * Two truth sources, strictly layered:
 *
 * - The adopted `SelectionResponse` is authoritative for staged/effective and
 *   Apply progress — the page adopts the response of its own write, so a
 *   racing re-read can never hand back a generation that is already stale.
 * - The desk lifecycle (labels, step statuses, restart wording) is used only
 *   while its `selection_generation` equals the adopted response's. On a
 *   mismatch the tracker renders "Refreshing / state unknown" and the page
 *   disables Stage and Apply until a newly adopted response agrees.
 *
 * The tracker displays the restart step; it never initiates one. The restart
 * command stays where ADR 0060 put it — a host ceremony the operator copies
 * from the handoff script and runs themselves.
 */
@Component({
  selector: 'app-configuration-lifecycle-tracker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './configuration-lifecycle-tracker.component.html',
  styleUrl: './configuration-lifecycle-tracker.component.scss',
})
export class ConfigurationLifecycleTrackerComponent {
  readonly selection = input.required<BrokerInstallationSelection>();
  readonly deskState = input<AlpacaDeskState | null>(null);

  /**
   * True when the desk lifecycle describes the adopted selection. `null`
   * desk state (not yet read) is treated as not-yet-matched: the tracker
   * shows the refreshing note rather than guessing.
   */
  protected readonly generationsMatch = computed(() => {
    const state = this.deskState();
    return state !== null && state.selection_generation === this.selection().selection_generation;
  });

  /** Backend-authored lifecycle steps, used only while the generations agree. */
  protected readonly steps = computed(() =>
    this.generationsMatch() ? (this.deskState()?.lifecycle ?? []) : [],
  );

  /** The selection-side Apply progress, which is always safe to show. */
  protected readonly applyRecorded = computed(() => this.selection().apply_requested);
}

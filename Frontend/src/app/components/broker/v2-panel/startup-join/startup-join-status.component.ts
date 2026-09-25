import { ChangeDetectionStrategy, Component, computed, effect, input, signal } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { StartupJoinView } from '../lib/broker-v2-panel.types';

const COUNTDOWN_TICK_MS = 1_000;

/**
 * Where a run is in joining its warmup to its live stream (#2410).
 *
 * The copy is the backend's. This component adds the times: the minute the
 * live stream takes over and, while the run is filling the minutes before it
 * from IBKR history, the time remaining before the run is refused -- a
 * deadline, not an estimate of when it will finish. A refusal names the
 * interval history did not return. Market times render in ET.
 */
@Component({
  selector: 'app-startup-join-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './startup-join-status.component.html',
  styleUrl: './startup-join-status.component.scss',
})
export class StartupJoinStatusComponent {
  readonly view = input.required<StartupJoinView>();

  private readonly nowMs = signal(Date.now());

  /** Whole seconds left before refusal, or `null` when no deadline is running. */
  protected readonly remainingSeconds = computed(() => {
    const view = this.view();
    if (view.state !== 'filling' || view.deadline_ms === null) return null;
    return Math.max(0, Math.ceil((view.deadline_ms - this.nowMs()) / 1_000));
  });

  protected readonly remainingLabel = computed(() => {
    const seconds = this.remainingSeconds();
    if (seconds === null) return null;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
  });

  constructor() {
    // Tick only while a deadline is running; every other state is static.
    effect((onCleanup) => {
      if (this.view().state !== 'filling') return;
      this.nowMs.set(Date.now());
      const timer = setInterval(() => this.nowMs.set(Date.now()), COUNTDOWN_TICK_MS);
      onCleanup(() => clearInterval(timer));
    });
  }
}

import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  BotRunView,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotRunEvidenceCardComponent } from './bot-run-evidence-card.component';

@Component({
  selector: 'app-bot-run-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotRunEvidenceCardComponent, TimestampDisplayComponent],
  templateUrl: './bot-run-history.component.html',
  styleUrl: './bot-run-history.component.scss',
})
export class BotRunHistoryComponent {
  readonly state = input.required<RunHistoryState>();
  readonly botRunning = input(false);
  readonly navigationRequested = output<RunHistoryNavigation>();

  protected readonly displayedRun = computed<BotRunView | null>(() => {
    const state = this.state();
    return state.mode === 'current'
      ? state.current
      : (state.history?.runs[0] ?? null);
  });

  protected readonly evidenceUpdatedAtMs = computed<number | null>(() => {
    const run = this.displayedRun();
    if (!run) return null;
    return run.terminal_outcome?.recorded_at_ms
      ?? run.process?.observed_at_ms
      ?? run.started_at_ms;
  });

  protected readonly evidenceStatus = computed(() => {
    if (this.state().mode === 'history') return 'Recorded evidence';
    return this.botRunning() ? 'Evidence is updating' : 'No changes since';
  });

  protected navigate(destination: RunHistoryNavigation): void {
    this.navigationRequested.emit(destination);
  }
}

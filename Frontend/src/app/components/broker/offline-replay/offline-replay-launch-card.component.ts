import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  OfflineReplayCatalogSession,
  OfflineReplaySpeed,
} from '../../../api/offline-replay.types';

export interface OfflineReplayDateOption {
  session: OfflineReplayCatalogSession;
  label: string;
}

@Component({
  selector: 'app-offline-replay-launch-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './offline-replay-launch-card.component.html',
  styleUrl: './offline-replay-launch-card.component.scss',
})
export class OfflineReplayLaunchCardComponent {
  readonly dateOptions = input.required<readonly OfflineReplayDateOption[]>();
  readonly selectedSessionDateMs = input.required<number | null>();
  readonly playbackMinutes = input.required<30 | 60>();
  readonly speed = input.required<OfflineReplaySpeed>();
  readonly loading = input.required<boolean>();
  readonly active = input.required<boolean>();
  readonly acting = input.required<boolean>();

  readonly sessionDateChange = output<number | null>();
  readonly durationChange = output<30 | 60>();
  readonly speedChange = output<OfflineReplaySpeed>();
  readonly launch = output();

  readonly launchDisabled = computed(
    () =>
      this.selectedSessionDateMs() === null ||
      this.loading() ||
      this.acting() ||
      this.active(),
  );

  onDate(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    this.sessionDateChange.emit(Number.isFinite(value) ? value : null);
  }

  onDuration(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    if (value === 30 || value === 60) this.durationChange.emit(value);
  }

  onSpeed(event: Event): void {
    const value = Number((event.target as HTMLSelectElement).value);
    if (value === 1 || value === 10 || value === 60) this.speedChange.emit(value);
  }

  cachedLabel(session: OfflineReplayCatalogSession): string {
    if (session.cached_symbols.length === 2) return 'SPY + TSLA cached';
    if (session.cached_symbols.length === 1) {
      return `${session.cached_symbols[0]} cached · missing symbol downloads on launch`;
    }
    return 'Downloads SPY + TSLA on launch';
  }
}

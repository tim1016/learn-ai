import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type {
  OfflineReplayCommandAction,
  OfflineReplaySpeed,
  OfflineReplayStatus,
} from '../../../api/offline-replay.types';

export interface OfflineReplayCommand {
  action: OfflineReplayCommandAction;
  speed?: OfflineReplaySpeed;
}

@Component({
  selector: 'app-offline-replay-transport',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './offline-replay-transport.component.html',
  styleUrl: './offline-replay-transport.component.scss',
})
export class OfflineReplayTransportComponent {
  readonly status = input.required<OfflineReplayStatus>();
  readonly speed = input.required<OfflineReplaySpeed>();
  readonly acting = input.required<boolean>();
  readonly replayCommand = output<OfflineReplayCommand>();

  readonly playbackSpeeds: readonly OfflineReplaySpeed[] = [1, 10, 60];

  emit(action: OfflineReplayCommandAction, speed?: OfflineReplaySpeed): void {
    this.replayCommand.emit(speed === undefined ? { action } : { action, speed });
  }
}

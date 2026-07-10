import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { BotEventStreamComponent } from './reused/bot-event-stream/bot-event-stream.component';
import type { BotEventStreamCommand } from './reused/bot-event-stream/bot-event-stream-action';
import { boundRunIdForStatus } from './lib/bound-run-id';

@Component({
  selector: 'app-bot-control-side-panel',
  imports: [BotEventStreamComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-control-side-panel.component.html',
  styleUrl: './bot-control-side-panel.component.scss',
})
export class BotControlSidePanelComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly commandsDisabled = input(false);

  readonly freshRunRequested = output();
  readonly streamActionInvoked = output<BotEventStreamCommand>();

  readonly runId = computed<string | null>(() => boundRunIdForStatus(this.status()));
}

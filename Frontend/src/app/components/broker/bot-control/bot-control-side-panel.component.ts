import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { LifecycleChartNode, LiveInstanceStatus } from '../../../api/live-instances.types';
import { BotEventStreamComponent } from './reused/bot-event-stream/bot-event-stream.component';
import { NodeInspectorComponent } from './node-inspector.component';
import { boundRunIdForStatus } from './lib/bound-run-id';

@Component({
  selector: 'app-bot-control-side-panel',
  imports: [BotEventStreamComponent, NodeInspectorComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-control-side-panel.component.html',
  styleUrl: './bot-control-side-panel.component.scss',
})
export class BotControlSidePanelComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly node = input<LifecycleChartNode | null>(null);
  readonly hasExplicitSelection = input<boolean>(false);

  readonly freshRunRequested = output();

  readonly runId = computed<string | null>(() => boundRunIdForStatus(this.status()));
}

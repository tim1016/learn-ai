import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  Injector,
  input,
  output,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type { LiveInstanceStatus } from '../../../../../api/live-instances.types';
import type { BotEventSeverity } from '../../../../../api/live-runs.types';
import {
  type DisplayRow,
  toDisplayRow,
} from './bot-event-display-row';
import { BotEventDrawerComponent } from './bot-event-drawer.component';
import { botEventRowStream, type BotEventRowStream } from './bot-event-row-stream';
import {
  actionForRow,
  type BotEventStreamAction,
  type BotEventStreamCommand,
} from './bot-event-stream-action';

interface ActionDisplayRow {
  readonly display: DisplayRow;
  readonly action: BotEventStreamAction | null;
}

@Component({
  selector: 'app-bot-event-stream',
  imports: [BotEventDrawerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-event-stream.component.html',
  styleUrl: './bot-event-stream.component.scss',
})
export class BotEventStreamComponent {
  readonly runId = input.required<string>();
  readonly status = input.required<LiveInstanceStatus>();
  readonly commandsDisabled = input(false);
  readonly actionInvoked = output<BotEventStreamCommand>();

  private readonly injector = inject(Injector);
  private readonly expanded = signal<Set<number>>(new Set());
  private readonly stream = signal<BotEventRowStream | null>(null);

  constructor() {
    effect((onCleanup) => {
      const runId = this.runId();
      const next = runInInjectionContext(this.injector, () => botEventRowStream(runId));
      this.stream.set(next);
      onCleanup(() => next.close());
    });
  }

  readonly rows = computed<DisplayRow[]>(() =>
    (this.stream()?.rows() ?? []).map((row) => toDisplayRow(row)),
  );
  readonly actionRows = computed<ActionDisplayRow[]>(() =>
    this.rows().map((display) => ({
      display,
      action: actionForRow(display.row, this.status(), this.commandsDisabled()),
    })),
  );
  readonly isLoading = computed<boolean>(() =>
    this.stream()?.isLoading() ?? true,
  );
  readonly errorMessage = computed<string | null>(() => this.stream()?.errorMessage() ?? null);
  readonly rowCountLabel = computed<string>(() => {
    const count = this.rows().length;
    return `${count} ${count === 1 ? 'event' : 'events'}`;
  });

  isExpanded(seq: number): boolean {
    return this.expanded().has(seq);
  }

  toggle(seq: number): void {
    this.expanded.update((current) => {
      const next = new Set(current);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  }

  severityClass(severity: BotEventSeverity): string {
    return `severity-${severity}`;
  }

  invokeAction(action: BotEventStreamAction): void {
    if (!action.enabled) return;
    this.actionInvoked.emit(action.command);
  }
}

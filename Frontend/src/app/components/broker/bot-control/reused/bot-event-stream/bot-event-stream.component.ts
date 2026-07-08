import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  Injector,
  input,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type { BotEventSeverity } from '../../../../../api/live-runs.types';
import {
  type DisplayRow,
  toDisplayRow,
} from './bot-event-display-row';
import { BotEventDrawerComponent } from './bot-event-drawer.component';
import { botEventRowStream, type BotEventRowStream } from './bot-event-row-stream';

@Component({
  selector: 'app-bot-event-stream',
  imports: [BotEventDrawerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-event-stream.component.html',
  styleUrl: './bot-event-stream.component.scss',
})
export class BotEventStreamComponent {
  readonly runId = input.required<string>();

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
  readonly isLoading = computed<boolean>(() =>
    this.stream()?.isLoading() ?? true,
  );
  readonly errorMessage = computed<string | null>(() => this.stream()?.errorMessage() ?? null);
  readonly rowCountLabel = computed<string>(() => `${this.rows().length} row(s)`);

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

  trackRow(_index: number, display: DisplayRow): number {
    return display.row.seq;
  }
}

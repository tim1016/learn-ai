import { HttpClient } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type { FailureRow } from '../bot-trade-chart-card/bot-trade-chart-card.types';

const POLL_INTERVAL_MS = 5_000;

@Component({
  selector: 'app-bot-failures-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-failures-table.component.html',
  styleUrl: './bot-failures-table.component.scss',
})
export class BotFailuresTableComponent {
  readonly runId = input<string | null>(null);

  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly expanded = signal<Set<number>>(new Set());

  protected readonly failuresResource = resource<FailureRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadFailures(params),
  });

  protected readonly rows = computed<FailureRow[]>(
    () => this.failuresResource.value() ?? [],
  );

  protected readonly hasData = computed<boolean>(() => this.rows().length > 0);

  constructor() {
    const timer = setInterval(() => this.failuresResource.reload(), POLL_INTERVAL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  private async loadFailures(runId: string | null): Promise<FailureRow[]> {
    if (!runId) return [];
    return firstValueFrom(
      this.http.get<FailureRow[]>(
        `/api/live-runs/${encodeURIComponent(runId)}/failures`,
      ),
    );
  }

  protected toggle(i: number): void {
    this.expanded.update((s) => {
      const next = new Set(s);
      if (next.has(i)) {
        next.delete(i);
      } else {
        next.add(i);
      }
      return next;
    });
  }

  protected isExpanded(i: number): boolean {
    return this.expanded().has(i);
  }

  protected trackFailure(_i: number, r: FailureRow): string {
    return `${r.ts_ms}:${r.logger}:${r.message.slice(0, 40)}`;
  }
}

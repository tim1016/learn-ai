import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import type {
  RunHistoryMode,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { OperatorDisclosureCardComponent } from '../operator-lens/operator-disclosure-card.component';
import { BotRunHistoryComponent } from './bot-run-history.component';

interface RunHistoryLocation {
  readonly mode: RunHistoryMode;
  readonly cursor: string | null;
  readonly newerCursors: readonly (string | null)[];
}

/** Operator-only owner for run-evidence loading, polling, and navigation. */
@Component({
  selector: 'app-operator-run-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotRunHistoryComponent, OperatorDisclosureCardComponent],
  templateUrl: './operator-run-history.component.html',
})
export class OperatorRunHistoryComponent {
  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly destroyRef = inject(DestroyRef);

  readonly broker = input.required<string>();
  readonly sid = input.required<string>();
  readonly botRunning = input.required<boolean>();
  protected readonly expanded = signal(false);
  private readonly activated = signal(false);

  private readonly location = linkedSignal<string, RunHistoryLocation>({
    source: this.sid,
    computation: () => ({ mode: 'current', cursor: null, newerCursors: [] }),
  });

  private readonly currentRun = resource({
    params: () => this.activated()
      ? { broker: this.broker(), sid: this.sid(), running: this.botRunning() }
      : undefined,
    loader: ({ params }) => this.panelSvc.getCurrentRun(params.broker, params.sid),
  });

  private readonly previousRun = resource({
    params: () => {
      const location = this.location();
      return this.activated() && location.mode === 'history'
        ? { broker: this.broker(), sid: this.sid(), cursor: location.cursor }
        : undefined;
    },
    loader: ({ params }) =>
      this.panelSvc.getRunHistory(params.broker, params.sid, params.cursor ?? undefined),
  });

  protected readonly state = computed<RunHistoryState>(() => {
    const location = this.location();
    const current = this.currentRun.hasValue() ? this.currentRun.value() : null;
    const history = this.previousRun.hasValue() ? this.previousRun.value() : null;
    return {
      mode: location.mode,
      current,
      history,
      currentLoading: current === null && this.currentRun.isLoading(),
      historyLoading: history === null && this.previousRun.isLoading(),
      currentFailed: current === null && this.currentRun.error() !== undefined,
      historyFailed: history === null && this.previousRun.error() !== undefined,
      canViewNewer: location.newerCursors.length > 0,
    };
  });

  constructor() {
    const pollTimer = setInterval(() => {
      if (this.expanded() && this.botRunning() && !this.currentRun.isLoading()) {
        this.currentRun.reload();
      }
    }, 5_000);
    this.destroyRef.onDestroy(() => clearInterval(pollTimer));
  }

  protected onExpandedChange(expanded: boolean): void {
    if (expanded) this.activated.set(true);
    this.expanded.set(expanded);
  }

  protected navigate(destination: RunHistoryNavigation): void {
    const location = this.location();
    if (destination === 'current') {
      if (location.mode === 'current') {
        if (this.currentRun.error() !== undefined) this.currentRun.reload();
        return;
      }
      this.location.update((current) => ({ ...current, mode: 'current' }));
      return;
    }
    if (destination === 'history') {
      if (location.mode === 'history') {
        if (this.previousRun.error() !== undefined) this.previousRun.reload();
        return;
      }
      this.location.update((current) => ({ ...current, mode: 'history' }));
      return;
    }
    if (destination === 'older') {
      const nextCursor = this.previousRun.hasValue()
        ? this.previousRun.value().next_cursor
        : null;
      if (!nextCursor) return;
      this.location.set({
        mode: 'history',
        cursor: nextCursor,
        newerCursors: [...location.newerCursors, location.cursor],
      });
      return;
    }
    const newerCursor = location.newerCursors.at(-1);
    if (newerCursor === undefined) return;
    this.location.set({
      mode: 'history',
      cursor: newerCursor,
      newerCursors: location.newerCursors.slice(0, -1),
    });
  }
}

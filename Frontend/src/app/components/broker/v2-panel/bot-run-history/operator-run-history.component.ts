import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import type {
  CurrentRunState,
  FeedContinuityView,
  RunHistoryMode,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { OperatorDisclosureCardComponent } from '../operator-lens/operator-disclosure-card.component';
import { BotRunHistoryComponent } from './bot-run-history.component';

interface RunHistoryLocation {
  readonly mode: RunHistoryMode;
  readonly cursor: string | null;
  readonly newerCursors: readonly (string | null)[];
}

/** Previous-run loading and navigation; latest-run evidence is shared by both lenses. */
@Component({
  selector: 'app-operator-run-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotRunHistoryComponent, OperatorDisclosureCardComponent],
  templateUrl: './operator-run-history.component.html',
})
export class OperatorRunHistoryComponent {
  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);

  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();
  readonly botRunning = input.required<boolean>();
  readonly currentRunState = input<CurrentRunState>(EMPTY_CURRENT_RUN_STATE);
  readonly runRefreshRequested = output();
  readonly feedContinuity = input.required<FeedContinuityView>();
  protected readonly expanded = signal(false);
  private readonly activated = signal(false);

  private readonly target = computed(() => {
    const lane = this.fleetDirectory.lane(this.broker(), this.clerkId());
    return resourceTarget(this.broker(), this.clerkId(), {
      accountId: this.accountId(),
      entityId: this.sid(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  });

  /** Cursors are opaque and lane-local; discard them on any provenance change. */
  private readonly location = linkedSignal<ResourceTarget, RunHistoryLocation>({
    source: this.target,
    computation: () => ({ mode: 'current', cursor: null, newerCursors: [] }),
  });

  private readonly previousRun = resource({
    params: () => {
      const location = this.location();
      return this.activated() && location.mode === 'history'
        ? { target: this.target(), sid: this.sid(), cursor: location.cursor }
        : undefined;
    },
    loader: ({ params }) =>
      this.panelSvc.getRunHistory(params.target, params.sid, params.cursor ?? undefined),
  });

  protected readonly state = computed<RunHistoryState>(() => {
    const location = this.location();
    const { run: current, loading, failed } = this.currentRunState();
    const history = this.previousRun.hasValue() ? this.previousRun.value() : null;
    return {
      mode: location.mode,
      current,
      history,
      currentLoading: current === null && loading,
      historyLoading: history === null && this.previousRun.isLoading(),
      currentFailed: current === null && failed,
      historyFailed: history === null && this.previousRun.error() !== undefined,
      canViewNewer: location.newerCursors.length > 0,
    };
  });

  protected onExpandedChange(expanded: boolean): void {
    if (expanded) this.activated.set(true);
    this.expanded.set(expanded);
  }

  protected navigate(destination: RunHistoryNavigation): void {
    const location = this.location();
    if (destination === 'current') {
      if (location.mode === 'current') {
        if (this.currentRunState().failed) this.runRefreshRequested.emit();
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

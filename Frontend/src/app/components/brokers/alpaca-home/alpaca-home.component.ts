import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  resource,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MessageService } from 'primeng/api';

import {
  HOME_VIEW_QUERY_PARAM,
  HOME_WALL_VIEW,
  accountWorkspaceTabRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  LANE_FENCE_REFRESH_FAILED_MESSAGE,
  fencedTarget,
  freezeLaneFence,
  laneFenceVerdict,
} from '../../../fleet/lane-fence';
import { openLaneFence } from '../../../fleet/open-lane-fence';
import { resourceTarget, withCommand, withEntity, type ResourceTarget } from '../../../fleet/resource-target';
import { LaneAttentionService } from '../../../services/lane-attention.service';
import { TypedHaltConfirmComponent } from '../../broker/shared/typed-halt-confirm/typed-halt-confirm.component';
import { CohortFlattenDrawerComponent } from '../../broker/v2-panel/cohort-flatten/cohort-flatten-drawer.component';
import { GalleryLiveStore } from '../../broker/v2-panel/gallery/lib/gallery-live-store.service';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import type { ActionId, BotCatalogView, PanelAction } from '../../broker/v2-panel/lib/broker-v2-panel.types';
import { actionOutcomeToast, deriveActionRejection } from '../../broker/v2-panel/lib/panel-action-outcome';
import { AlpacaAccountCardComponent } from '../alpaca-desk/alpaca-account-card.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { AlpacaManualOrderHostComponent } from '../alpaca-desk/alpaca-manual-order-host.component';
import type { StopPhase } from './home-bot-action.component';
import { HomeAttentionComponent } from './home-attention.component';
import { HomeBotGroupComponent } from './home-bot-group.component';
import { homeBots } from './home-bots';
import { HOME_CLEAR_COPY, type ClearBatch, type ClearOutcome } from './home-clear';
import { HomeFinishedComponent } from './home-finished.component';
import { HomeMoneyComponent } from './home-money.component';

/** How often Home re-reads its bots; the Wall's candles stream on their own. */
const CATALOG_POLL_MS = 5_000;

/** A Stop's outcome, said where the keyboard lands after it. */
interface Outcome {
  readonly tone: 'success' | 'info' | 'danger';
  readonly message: string;
}

/** The stop a bot's Clerk offers: the recovery catalog's, the same action
 * the bot page presents (`recovery_policy`, #2605). */
const STOP_ACTION_ID: ActionId = 'stop_bot_decisions';

/** One Stop as the owner is asked it: the action the bot's Clerk offered when
 * Stop was pressed, and the command target it was read against. */
interface StopAsk {
  readonly sid: string;
  readonly target: ResourceTarget;
  readonly action: PanelAction;
}

/** One roster read, stamped with the account it was read for. */
interface KeyedRoster {
  readonly key: string;
  readonly rows: readonly BotCatalogView[];
}

/** This account's bots as far as Home knows them: not read yet, a first read
 * that failed, or the rows of the last read for THIS account. Counts and
 * empty states are said only for `ready` — an unknown never reads as "no
 * bots" (review B1). */
type Roster =
  | { readonly kind: 'unread' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'ready'; readonly rows: readonly BotCatalogView[] };

/** The account a read belongs to; a switch to another account is a new key. */
function rosterKey(target: ResourceTarget): string {
  return `${target.broker}::${target.clerkId}::${target.accountId ?? ''}`;
}

/**
 * An account's Home (PRD #2560 D1): the bare account URL, replacing Overview,
 * Bots and Gallery. Top to bottom: what needs the owner, each with its fix;
 * where the money is; the bots — running, then stopped still holding — as a
 * List or a Wall; Dry Run; the folded Finished list; the folded Account
 * details from Alpaca.
 *
 * Home owns the action paths for its bots — Stop, and clearing finished bots
 * — fenced to the lane the owner was shown (#2068), and moves the keyboard to
 * the outcome. The bot
 * grouping, every dollar and every attention line are the backend's; Home
 * only joins each bot to its own slice of the money read.
 */
@Component({
  selector: 'app-alpaca-home',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaAccountCardComponent,
    AlpacaManualOrderHostComponent,
    CohortFlattenDrawerComponent,
    HomeAttentionComponent,
    HomeBotGroupComponent,
    HomeFinishedComponent,
    HomeMoneyComponent,
    RouterLink,
    TypedHaltConfirmComponent,
  ],
  templateUrl: './alpaca-home.component.html',
  styleUrl: './alpaca-home.component.scss',
  providers: [GalleryLiveStore],
})
export class AlpacaHomeComponent {
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  /** `?view=wall` shows the bots as the Wall; anything else as the List. */
  readonly view = input<string | undefined>(undefined);

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly attention = inject(LaneAttentionService);
  private readonly messageService = inject(MessageService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly document = inject(DOCUMENT);
  private readonly injector = inject(Injector);
  protected readonly wallStore = inject(GalleryLiveStore);

  protected readonly account = computed<BoundAccountWorkspaceAddress>(() => ({
    broker: 'alpaca',
    clerkId: this.clerkId(),
    accountId: this.accountId(),
  }));
  protected readonly wall = computed(() => this.view() === HOME_WALL_VIEW);
  protected readonly deployRoute = computed(() => accountWorkspaceTabRoute(this.account(), 'deploy'));

  // ── Reads ────────────────────────────────────────────────────────────────

  private readonly target = computed(() => {
    const lane = this.fleetDirectory.lane('alpaca', this.clerkId());
    return resourceTarget('alpaca', this.clerkId(), {
      accountId: this.accountId(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  });

  /** The fence the owner was shown: captured when Home renders the lane and
   * again only when the route identity changes, never at click time (#2068). */
  private readonly openFence = openLaneFence(
    () => freezeLaneFence(this.fleetDirectory.lane('alpaca', this.clerkId())),
    () => `alpaca::${this.clerkId()}`,
  );

  protected readonly catalog = resource({
    params: () => this.target(),
    loader: async ({ params }): Promise<KeyedRoster> => ({
      key: rosterKey(params),
      rows: await this.panelService.getCatalog(params),
    }),
  });
  /** The last roster read, kept while a poll is in flight or has failed —
   * but only ever shown for the account it was read for. */
  private readonly lastCatalog = signal<KeyedRoster | null>(null);
  private readonly key = computed(() => rosterKey(this.target()));
  protected readonly roster = computed<Roster>(() => {
    const last = this.lastCatalog();
    if (last !== null && last.key === this.key()) return { kind: 'ready', rows: last.rows };
    return this.catalog.error() === undefined ? { kind: 'unread' } : { kind: 'failed' };
  });

  /** Where the account's money is, in the one money state every money surface
   * renders (`accountMoneyState`). */
  protected readonly money = this.accountData.moneyState;
  protected readonly settingsRoute = computed(() => accountWorkspaceTabRoute(this.account(), 'settings'));

  protected readonly bots = computed(() => {
    const roster = this.roster();
    const money = this.money();
    return homeBots(
      roster.kind === 'ready' ? roster.rows : [],
      money.kind === 'ready' ? money.view.segments ?? [] : [],
    );
  });
  /** Running bots, then stopped bots still holding money: the Bots list. */
  protected readonly botList = computed(() => [...this.bots().running, ...this.bots().holding]);
  protected readonly catalogFailure = computed(() => {
    const error = this.catalog.error();
    return error === undefined ? null : deriveActionRejection(error, 'The bots could not be read.');
  });
  protected readonly attentionState = computed(() => this.attention.stateFor(this.clerkId()));

  // ── Actions ──────────────────────────────────────────────────────────────

  /** Each bot whose Stop is in flight, and where it stands. One Stop is read
   * at a time, since only one can be asked. */
  protected readonly stopPhases = signal<ReadonlyMap<string, StopPhase>>(new Map());
  /** The Stop the owner is being asked to confirm, in its action's own words. */
  protected readonly stopAsk = signal<StopAsk | null>(null);
  protected readonly stopConfirmation = computed(() => this.stopAsk()?.action.confirmation ?? null);
  /** Counts the owner's arrivals on an account. A Stop belongs to the visit
   * it began on: one that answers on a later visit, even back on the same
   * account, neither asks nor touches the phase a newer Stop holds. */
  private visit = 0;
  protected readonly outcome = signal<Outcome | null>(null);
  protected readonly flattenOpen = signal(false);
  private readonly outcomeNotice = viewChild<ElementRef<HTMLElement>>('outcomeNotice');
  private readonly listRadio = viewChild<ElementRef<HTMLButtonElement>>('listRadio');
  private readonly wallRadio = viewChild<ElementRef<HTMLButtonElement>>('wallRadio');
  private flattenOpener: HTMLElement | null = null;

  /** The last clear sent: a retry re-sends it verbatim, under its own key. */
  private readonly clearBatch = signal<ClearBatch | null>(null);
  /** How many bots the clear in flight carries. */
  protected readonly clearing = signal<number | null>(null);
  protected readonly clearOutcome = signal<ClearOutcome | null>(null);
  private readonly finished = viewChild(HomeFinishedComponent);

  constructor() {
    // Home opens on the account's money as it is now. The read belongs to the
    // workspace and is polled only every 15 s, so a Deploy or a sale made on
    // another page would otherwise leave its bot's row joined to a read from
    // before it — "Money not shown" beside a bar that still claims the money.
    if (!this.accountData.money.isLoading()) this.accountData.money.reload();
    effect(() => {
      if (this.catalog.hasValue()) this.lastCatalog.set(this.catalog.value());
    });
    // A Stop's question and outcome belong to the account they were said on:
    // a switch to another account clears them.
    effect(() => {
      this.key();
      untracked(() => {
        this.visit++;
        this.stopAsk.set(null);
        this.stopPhases.set(new Map());
        this.outcome.set(null);
        this.dismissClear();
      });
    });
    // The Wall's candles and fills stream from the gallery feed only while the
    // Wall is shown; the List needs none.
    effect(() => {
      if (!this.wall()) {
        this.wallStore.stop();
        return;
      }
      const lane = this.fleetDirectory.lane('alpaca', this.clerkId());
      void this.wallStore.start(
        'alpaca',
        this.clerkId(),
        this.accountId(),
        lane?.effective_binding_generation ?? null,
        lane?.routing_epoch ?? null,
      );
    });
    const timer = setInterval(() => {
      if (this.document.visibilityState === 'visible' && !this.catalog.isLoading()) this.catalog.reload();
    }, CATALOG_POLL_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  protected showView(wall: boolean): void {
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [HOME_VIEW_QUERY_PARAM]: wall ? HOME_WALL_VIEW : null },
      queryParamsHandling: 'merge',
    });
  }

  /** Arrow keys, Home and End move the List/Wall choice and the keyboard with
   * it (the ARIA radio group pattern). With two choices every move flips it. */
  protected onViewKeydown(event: KeyboardEvent): void {
    const wall =
      ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(event.key) ? !this.wall()
      : event.key === 'Home' ? false
      : event.key === 'End' ? true
      : null;
    if (wall === null) return;
    event.preventDefault();
    this.showView(wall);
    (wall ? this.wallRadio() : this.listRadio())?.nativeElement.focus();
  }

  protected refresh(): void {
    this.catalog.reload();
    this.accountData.money.reload();
  }

  protected openCohortFlatten(): void {
    const opener = this.document.activeElement;
    this.flattenOpener = opener instanceof HTMLElement ? opener : null;
    this.flattenOpen.set(true);
  }

  protected closeCohortFlatten(): void {
    this.flattenOpen.set(false);
    const opener = this.flattenOpener;
    this.flattenOpener = null;
    if (opener === null) return;
    afterNextRender({ write: () => { if (opener.isConnected) opener.focus(); } }, { injector: this.injector });
  }

  /**
   * Stop one bot through the stop its Clerk offers now, against the lane the
   * owner was shown, asked with that action's own confirmation exactly as the
   * bot page asks it (#2605). The outcome is announced and the keyboard moves
   * to it.
   */
  protected async stop(sid: string): Promise<void> {
    // The bot's own button already shows its Stop in flight.
    if (this.stopPhases().has(sid) || this.stopAsk() !== null) return;
    const reading = [...this.stopPhases()].find(([, phase]) => phase === 'reading')?.[0];
    if (reading !== undefined) {
      this.announce({
        tone: 'info',
        message: `Still checking whether ${reading} can be stopped. Press Stop on ${sid} again after that.`,
      });
      return;
    }
    const visit = this.visit;
    this.setStopPhase(sid, 'reading');
    let ask: StopAsk | null;
    try {
      ask = await this.readStop(sid, visit);
    } finally {
      if (visit === this.visit) {
        this.setStopPhase(sid, null);
        // A "still checking" note is over once the check is.
        if (this.outcome()?.tone === 'info') this.outcome.set(null);
      }
    }
    if (ask === null) return;
    if (ask.action.confirmation === null) await this.sendStop(ask);
    else this.stopAsk.set(ask);
  }

  protected confirmStop(): void {
    const ask = this.stopAsk();
    this.stopAsk.set(null);
    if (ask !== null) void this.sendStop(ask);
  }

  protected cancelStop(): void {
    this.stopAsk.set(null);
  }

  /** The stop this bot's Clerk offers now, or `null` once why not is said. */
  private async readStop(sid: string, visit: number): Promise<StopAsk | null> {
    const lane = this.shownLane();
    if (!lane.ok) {
      this.announce({ tone: 'danger', message: lane.message });
      return null;
    }
    const target = withCommand(withEntity(lane.target, sid), 'bot_action', crypto.randomUUID());
    const read = await this.panelService.getPanel(target, sid).then(
      (panel) => ({ panel, error: null }),
      (error: unknown) => ({ panel: null, error }),
    );
    // The owner moved to another account while this was read: its answer
    // belongs to the visit they left, so it is neither asked nor said here.
    if (this.visit !== visit) return null;
    if (read.panel === null) {
      this.announceStopRefusal(sid, read.error);
    } else {
      const action = read.panel.actions.find((candidate) => candidate.action_id === STOP_ACTION_ID);
      if (action !== undefined && action.enabled) return { sid, target, action };
      this.announce({ tone: 'danger', message: `${sid} can no longer be stopped from here. Its current state is shown below.` });
    }
    this.refresh();
    return null;
  }

  private async sendStop({ sid, target, action }: StopAsk): Promise<void> {
    const visit = this.visit;
    this.setStopPhase(sid, 'sending');
    try {
      const result = await this.panelService.runBotAction(target, sid, action);
      this.announce({ tone: 'success', message: result.message });
    } catch (error) {
      this.announceStopRefusal(sid, error);
    } finally {
      if (visit === this.visit) this.setStopPhase(sid, null);
      this.refresh();
    }
  }

  private setStopPhase(sid: string, phase: StopPhase | null): void {
    this.stopPhases.update((current) => {
      const next = new Map(current);
      if (phase === null) next.delete(sid);
      else next.set(sid, phase);
      return next;
    });
  }

  private announceStopRefusal(sid: string, error: unknown): void {
    const rejection = deriveActionRejection(error, `${sid} could not be stopped.`);
    this.announce({ tone: 'danger', message: rejection.why ? `${rejection.message} ${rejection.why}` : rejection.message });
    this.refreshDirectoryAfterFenceRefusal(rejection.reasonCode);
  }

  /**
   * Clear the confirmed finished bots (owner decision 2026-09-28) under a key
   * minted for this confirmation, against the lane the owner was shown.
   */
  protected async clear(sids: readonly string[]): Promise<void> {
    if (this.clearing() !== null) return;
    const lane = this.shownLane();
    if (!lane.ok) {
      this.clearBatch.set(null);
      this.clearOutcome.set({ kind: 'refused', message: lane.message, why: null, reasonCode: null });
      this.focusClearOutcome();
      return;
    }
    const key = crypto.randomUUID();
    const batch: ClearBatch = {
      target: withCommand(lane.target, 'bot_action', key),
      request: { idempotency_key: key, strategy_instance_ids: [...sids] },
    };
    // A new batch replaces the last outcome; only a retry keeps it on screen (#2767).
    this.clearOutcome.set(null);
    this.clearBatch.set(batch);
    await this.sendClear(batch);
  }

  /** Re-send the last clear unchanged: a bot it already cleared replays. */
  protected async retryClear(): Promise<void> {
    const batch = this.clearBatch();
    if (batch !== null && this.clearing() === null) await this.sendClear(batch);
  }

  protected dismissClear(): void {
    this.clearOutcome.set(null);
    this.clearBatch.set(null);
  }

  private async sendClear(batch: ClearBatch): Promise<void> {
    this.clearing.set(batch.request.strategy_instance_ids.length);
    try {
      const result = await this.panelService.clearBots(batch.target, batch.request);
      this.clearOutcome.set({ kind: 'result', result, requested: batch.request.strategy_instance_ids });
    } catch (error) {
      const rejection = deriveActionRejection(error, HOME_CLEAR_COPY.requestFallback);
      this.clearOutcome.set(rejection.outcome === 'unknown'
        ? { kind: 'unknown' }
        : { kind: 'refused', message: rejection.message, why: rejection.why, reasonCode: rejection.reasonCode });
      this.refreshDirectoryAfterFenceRefusal(rejection.reasonCode);
    } finally {
      this.clearing.set(null);
      // Cleared bots leave the Finished list on this read.
      this.refresh();
      this.focusClearOutcome();
    }
  }

  private focusClearOutcome(): void {
    afterNextRender({ write: () => this.finished()?.focusOutcome() }, { injector: this.injector });
  }

  /** The lane the owner was shown, as a command target — or why it no longer
   * is the lane this account routes to (#2068). */
  private shownLane(): { readonly ok: true; readonly target: ResourceTarget } | { readonly ok: false; readonly message: string } {
    const fence = this.openFence();
    const verdict = laneFenceVerdict(fence, this.fleetDirectory.lane('alpaca', this.clerkId()));
    return verdict.ok ? { ok: true, target: fencedTarget(this.target(), fence) } : verdict;
  }

  /** A refusal that proves the shown lane stale re-reads the directory, so the
   * next action is fenced to a lane the owner can see. */
  private refreshDirectoryAfterFenceRefusal(reasonCode: string | null): void {
    if (reasonCode !== 'clerk_binding_generation_conflict') return;
    void this.fleetDirectory.refresh().catch(() => {
      this.messageService.add(actionOutcomeToast('failure', LANE_FENCE_REFRESH_FAILED_MESSAGE));
    });
  }

  /** Say what happened and move the keyboard there (PRD #2560 story 48). */
  private announce(outcome: Outcome): void {
    this.outcome.set(outcome);
    afterNextRender({ write: () => this.outcomeNotice()?.nativeElement.focus() }, { injector: this.injector });
  }
}

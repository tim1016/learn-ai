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
import { CohortFlattenDrawerComponent } from '../../broker/v2-panel/cohort-flatten/cohort-flatten-drawer.component';
import { GalleryLiveStore } from '../../broker/v2-panel/gallery/lib/gallery-live-store.service';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';
import { actionOutcomeToast, deriveActionRejection } from '../../broker/v2-panel/lib/panel-action-outcome';
import { AlpacaAccountCardComponent } from '../alpaca-desk/alpaca-account-card.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { AlpacaManualOrderHostComponent } from '../alpaca-desk/alpaca-manual-order-host.component';
import { HomeAttentionComponent } from './home-attention.component';
import { HomeBotGroupComponent } from './home-bot-group.component';
import { homeBots } from './home-bots';
import { HomeFinishedComponent } from './home-finished.component';
import { HomeMoneyComponent } from './home-money.component';

/** How often Home re-reads its bots; the Wall's candles stream on their own. */
const CATALOG_POLL_MS = 5_000;

/** A Stop's outcome, said where the keyboard lands after it. */
interface Outcome {
  readonly tone: 'success' | 'danger';
  readonly message: string;
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
 * Home owns the one action path for its bots (Stop), fenced to the lane the
 * owner was shown (#2068), and moves the keyboard to the outcome. The bot
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

  protected readonly pendingSids = signal<ReadonlySet<string>>(new Set());
  protected readonly outcome = signal<Outcome | null>(null);
  protected readonly flattenOpen = signal(false);
  private readonly outcomeNotice = viewChild<ElementRef<HTMLElement>>('outcomeNotice');
  private flattenOpener: HTMLElement | null = null;

  constructor() {
    effect(() => {
      if (this.catalog.hasValue()) this.lastCatalog.set(this.catalog.value());
    });
    // A Stop's outcome belongs to the account it was said on: a switch to
    // another account clears it.
    effect(() => {
      this.key();
      untracked(() => this.outcome.set(null));
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
   * Stop one bot through the action its panel presents now, against the lane
   * the owner was shown. The outcome is announced and the keyboard moves to it.
   */
  protected async stop(sid: string): Promise<void> {
    if (this.pendingSids().has(sid)) return;
    const fence = this.openFence();
    const verdict = laneFenceVerdict(fence, this.fleetDirectory.lane('alpaca', this.clerkId()));
    if (!verdict.ok) {
      this.announce({ tone: 'danger', message: verdict.message });
      return;
    }
    const target = withCommand(withEntity(fencedTarget(this.target(), fence), sid), 'bot_action', crypto.randomUUID());
    this.pendingSids.update((current) => new Set(current).add(sid));
    try {
      const panel = await this.panelService.getPanel(target, sid);
      const action = panel.actions.find((candidate) => candidate.action_id === 'stop');
      if (action === undefined || !action.enabled) {
        this.announce({ tone: 'danger', message: `${sid} can no longer be stopped from here. Its current state is shown below.` });
        return;
      }
      const result = await this.panelService.runBotAction(target, sid, action);
      this.announce({ tone: 'success', message: result.message });
    } catch (error) {
      const rejection = deriveActionRejection(error, `${sid} could not be stopped.`);
      this.announce({ tone: 'danger', message: rejection.why ? `${rejection.message} ${rejection.why}` : rejection.message });
      if (rejection.reasonCode === 'clerk_binding_generation_conflict') {
        void this.fleetDirectory.refresh().catch(() => {
          this.messageService.add(actionOutcomeToast('failure', LANE_FENCE_REFRESH_FAILED_MESSAGE));
        });
      }
    } finally {
      this.pendingSids.update((current) => {
        const next = new Set(current);
        next.delete(sid);
        return next;
      });
      this.refresh();
    }
  }

  /** Say what happened and move the keyboard there (PRD #2560 story 48). */
  private announce(outcome: Outcome): void {
    this.outcome.set(outcome);
    afterNextRender({ write: () => this.outcomeNotice()?.nativeElement.focus() }, { injector: this.injector });
  }
}

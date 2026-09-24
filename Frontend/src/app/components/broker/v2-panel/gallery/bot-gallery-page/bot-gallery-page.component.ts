import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { MessageService } from 'primeng/api';

import { BrokerV2PanelService } from '../../lib/broker-v2-panel.service';
import { resourceTarget, withCommand } from '../../../../../fleet/resource-target';
import { FleetDirectoryService } from '../../../../../fleet/fleet-directory.service';
import {
  freezeLaneFence,
  laneFenceVerdict,
  LANE_FENCE_REFRESH_FAILED_MESSAGE,
} from '../../../../../fleet/lane-fence';
import { openLaneFence } from '../../../../../fleet/open-lane-fence';
import { actionOutcomeToast, deriveActionRejection } from '../../lib/panel-action-outcome';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { BotGalleryDockComponent } from '../bot-gallery-dock/bot-gallery-dock.component';
import { GalleryLiveStore } from '../lib/gallery-live-store.service';

type GalleryViewState = 'loading' | 'error' | 'empty' | 'ready';

/**
 * Route host for the aggregated bot gallery wall (`…/gallery`). Owns the one
 * `GalleryLiveStore` for this account — component-provided, started from the
 * routed `broker`/`accountId` in a constructor `effect`, stopped on destroy
 * — and renders the loading/error/empty/ready states around
 * `BotGalleryDockComponent` (which owns pagination, "Reset layout", the
 * status filter, and — via the `status` input below — the footer's
 * `●Live` indicator; this host does not duplicate any of that). The page
 * itself no longer has its own toolbar/title: the dock is the page, and the
 * account it serves is named once by the account workspace's header above
 * it (ADR 0064), so the wall keeps the full width under that header.
 *
 * `GalleryLiveStore.status()` can only be `'error'` while no snapshot has
 * ever been adopted (see the store's `applyTransportStatus`), which means
 * `bots()` is necessarily empty in that state too — so `error` is checked
 * ahead of `empty` in `viewState` and the dock is never rendered alongside
 * the error banner; there is no dead "error with tiles" branch to maintain.
 *
 * Quick actions: `GalleryBotView.primary_action` is a deliberately lean
 * projection (id/label/enabled/reason — see `gallery.types.ts`), not the
 * full `PanelAction` the existing `POST …/bots/{sid}/actions` pipeline
 * requires (it needs `revision`/`concurrency_token` for the
 * optimistic-concurrency guard). `onAction` fetches the authoritative panel
 * once per confirmed click to get that object, then calls the same
 * `BrokerV2PanelService.runBotAction` every other action surface in this
 * panel uses — no new action endpoint (design spec §7).
 */
@Component({
  selector: 'app-bot-gallery-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotGalleryDockComponent, ReceiptLabelPipe],
  providers: [GalleryLiveStore],
  templateUrl: './bot-gallery-page.component.html',
  styleUrl: './bot-gallery-page.component.scss',
  host: { class: 'block h-full' },
})
export class BotGalleryPageComponent {
  /** Frozen lane context for the gallery's reads and actions (FR-094). Reads
   * the fence captured at open, not the live directory (#2068). */
  private readonly galleryTarget = (sid: string) => {
    const fence = this.openFence();
    return resourceTarget(this.broker(), this.clerkId(), {
      accountId: this.accountId(),
      entityId: sid,
      bindingGeneration: fence.bindingGeneration,
      routingEpoch: fence.routingEpoch,
    });
  };

  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();

  protected readonly store = inject(GalleryLiveStore);
  private readonly panelService = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly messageService = inject(MessageService);
  private readonly destroyRef = inject(DestroyRef);

  /** The fence the operator was shown. Captured when the gallery renders the
   * lane and again only when the route identity changes; never at click time
   * (#2068). Wiring is the shared `openLaneFence` helper — see its doc for
   * why both the `untracked` directory read and the eager materialization
   * it performs are load-bearing. */
  private readonly openFence = openLaneFence(
    () => freezeLaneFence(this.fleetDirectory.lane(this.broker(), this.clerkId())),
    () => `${this.broker()}::${this.clerkId()}`,
  );

  /**
   * Sids with a confirmed quick action in flight. Drives two things off the
   * one set: the reentrancy guard in `onAction` (below) and the visual
   * pending affordance on the tile — passed straight through to the dock's
   * `pendingSids` input, which forwards `has(bot.sid)` to each
   * `BotTileComponent`'s `pending` input (disables the button, sets
   * `aria-busy`). Mirrors `bots-roster`'s `pendingBotIds` pattern.
   */
  protected readonly pendingSids = signal<ReadonlySet<string>>(new Set());

  /** Drives the non-blocking stale banner above the dock; the dock's own footer renders the compact `●Delayed` indicator off the same `store.status()`, forwarded via the `status` input. */
  protected readonly stale = computed(() => this.store.status() === 'stale');

  protected readonly viewState = computed<GalleryViewState>(() => {
    const bots = this.store.bots();
    if (this.store.status() === 'connecting' && bots.length === 0) return 'loading';
    if (this.store.status() === 'error') return 'error';
    if (bots.length === 0) return 'empty';
    return 'ready';
  });

  constructor() {
    effect(() => {
      // The stream address is a read, not a command: it must keep following
      // the live lane, unlike the fence above.
      const lane = this.fleetDirectory.lane(this.broker(), this.clerkId());
      void this.store.start(
        this.broker(),
        this.clerkId(),
        this.accountId(),
        lane?.effective_binding_generation ?? null,
        lane?.routing_epoch ?? null,
      );
    });
    this.destroyRef.onDestroy(() => this.store.stop());
  }

  protected async onAction(event: { sid: string; actionId: string }): Promise<void> {
    if (this.pendingSids().has(event.sid)) return;
    const verdict = laneFenceVerdict(this.openFence(), this.fleetDirectory.lane(this.broker(), this.clerkId()));
    if (!verdict.ok) {
      this.messageService.add(actionOutcomeToast('conflict', verdict.message));
      return;
    }
    // Capture once at presentation/submission time. In particular, do not
    // rebuild from route signals after the authoritative panel read returns.
    const target = withCommand(this.galleryTarget(event.sid), 'bot_action', crypto.randomUUID());
    this.pendingSids.update((current) => new Set(current).add(event.sid));
    try {
      const panel = await this.panelService.getPanel(target, event.sid);
      const action = panel.actions.find((candidate) => candidate.action_id === event.actionId);
      if (action === undefined || !action.enabled) {
        const message = `${event.actionId} is no longer available for ${event.sid}.`;
        this.messageService.add(actionOutcomeToast('conflict', message));
        return;
      }
      const result = await this.panelService.runBotAction(
        target,
        event.sid,
        action,
      );
      this.messageService.add(actionOutcomeToast('success', result.message));
    } catch (error) {
      const rejection = deriveActionRejection(
        error,
        `Could not run ${event.actionId} on ${event.sid}.`,
      );
      this.messageService.add(actionOutcomeToast(rejection.outcome, rejection.message, rejection.why));
      // A stale-generation refusal means the fence the operator was shown is
      // provably wrong; refresh so the next action is minted against a lane
      // they have actually seen (#2068).
      if (rejection.reasonCode === 'clerk_binding_generation_conflict') {
        void this.fleetDirectory.refresh().catch(() => {
          this.messageService.add(actionOutcomeToast('failure', LANE_FENCE_REFRESH_FAILED_MESSAGE));
        });
      }
    } finally {
      this.pendingSids.update((current) => {
        const next = new Set(current);
        next.delete(event.sid);
        return next;
      });
    }
  }
}

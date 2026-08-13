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
import { RouterLink } from '@angular/router';
import { MessageService } from 'primeng/api';

import { BrokerV2PanelService } from '../../lib/broker-v2-panel.service';
import { actionOutcomeToast, deriveActionRejection } from '../../lib/panel-action-outcome';
import { BotGalleryDockComponent } from '../bot-gallery-dock/bot-gallery-dock.component';
import { GalleryLiveStore } from '../lib/gallery-live-store.service';
import type { GalleryLiveStatus } from '../lib/gallery.types';

const STATUS_LABEL: Record<GalleryLiveStatus, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  stale: 'Delayed',
  error: 'Feed error',
};

type GalleryViewState = 'loading' | 'error' | 'empty' | 'ready';

/**
 * Route host for the aggregated bot gallery wall (`…/gallery`). Owns the one
 * `GalleryLiveStore` for this account — component-provided, started from the
 * routed `broker`/`accountId` in a constructor `effect`, stopped on destroy
 * — and renders the loading/error/empty/ready states around
 * `BotGalleryDockComponent` (which already owns pagination and "Reset
 * layout"; this host does not duplicate either).
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
  imports: [RouterLink, BotGalleryDockComponent],
  providers: [GalleryLiveStore],
  templateUrl: './bot-gallery-page.component.html',
  styleUrl: './bot-gallery-page.component.scss',
  host: { class: 'block h-full page-inset' },
})
export class BotGalleryPageComponent {
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();

  protected readonly store = inject(GalleryLiveStore);
  private readonly panelService = inject(BrokerV2PanelService);
  private readonly messageService = inject(MessageService);
  private readonly destroyRef = inject(DestroyRef);

  /**
   * Sids with a confirmed quick action in flight. Drives two things off the
   * one set: the reentrancy guard in `onAction` (below) and the visual
   * pending affordance on the tile — passed straight through to the dock's
   * `pendingSids` input, which forwards `has(bot.sid)` to each
   * `BotTileComponent`'s `pending` input (disables the button, sets
   * `aria-busy`). Mirrors `bots-roster`'s `pendingBotIds` pattern.
   */
  protected readonly pendingSids = signal<ReadonlySet<string>>(new Set());

  protected readonly statusLabel = computed(() => STATUS_LABEL[this.store.status()]);
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
      void this.store.start(this.broker(), this.accountId());
    });
    this.destroyRef.onDestroy(() => this.store.stop());
  }

  protected async onAction(event: { sid: string; actionId: string }): Promise<void> {
    if (this.pendingSids().has(event.sid)) return;
    this.pendingSids.update((current) => new Set(current).add(event.sid));
    try {
      const panel = await this.panelService.getPanel(this.broker(), this.accountId(), event.sid);
      const action = panel.actions.find((candidate) => candidate.action_id === event.actionId);
      if (action === undefined || !action.enabled) {
        const message = `${event.actionId} is no longer available for ${event.sid}.`;
        this.messageService.add(actionOutcomeToast('conflict', message));
        return;
      }
      const result = await this.panelService.runBotAction(
        this.broker(),
        this.accountId(),
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
    } finally {
      this.pendingSids.update((current) => {
        const next = new Set(current);
        next.delete(event.sid);
        return next;
      });
    }
  }
}

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  untracked,
} from '@angular/core';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget } from '../../../../fleet/resource-target';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotEndRefusalComponent } from '../../bot-end/bot-end-refusal.component';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import {
  BrokerV2PanelService,
  type OptionBuyingPowerChecks,
  type OptionBuyingPowerHoldReview,
} from '../lib/broker-v2-panel.service';
import { deriveActionRejection, type ActionRejection } from '../lib/panel-action-outcome';

/** Said only when the review came back with no words of the backend's own. */
const REVIEW_UNANSWERED = 'The review was not recorded, so the result is not known. Read this card again.';

/**
 * A bot's option entries beside Alpaca's option buying power (ADR 0076 decision 26).
 *
 * Above the entries: how many were compared, how many were not, and why. An
 * entry nobody compared is counted as that and never shown as agreeing. Under
 * them: the notice that holds the bot's new entries, when one does, with the
 * owner's review behind a confirmation. Every figure, count and sentence is
 * the backend's; this component adds the names of its controls.
 *
 * The account's own requirement across an entry is evidence beside the
 * comparison. It changes no result and holds nothing, so the card words it
 * apart from what the bot set aside.
 *
 * The page owns the review, as it owns every command on this bot: `review` is
 * its request, fenced to the lane the owner was shown and sent one at a time.
 * The card reads the comparisons and sends nothing of its own.
 *
 * A comparison is a custody record, so a new journal tail re-reads the page,
 * keeping what is shown while it does. A review answers with the comparisons
 * as they then stand, which the card shows until the next read.
 */
@Component({
  selector: 'app-bot-option-buying-power',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndRefusalComponent, TimestampDisplayComponent, TypedHaltConfirmComponent],
  templateUrl: './bot-option-buying-power.component.html',
  styleUrl: './bot-option-buying-power.component.scss',
})
export class BotOptionBuyingPowerComponent {
  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();
  /** The panel's newest custody record; a new one re-reads the page. */
  readonly journalTailSeq = input<number | null>(null);
  /** Records the owner's review of the bot's notice; rejects with the refusal this card is to show. */
  readonly review = input.required<() => Promise<OptionBuyingPowerHoldReview>>();
  /** Another command on this bot is on its way. */
  readonly locked = input(false);

  private readonly panelSvc = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);

  private readonly target = computed(() => {
    const lane = this.fleetDirectory.lane(this.broker(), this.clerkId());
    return resourceTarget(this.broker(), this.clerkId(), {
      accountId: this.accountId(),
      entityId: this.sid(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  });

  // Identity only in the params: a refresh is a reload, which keeps what is shown.
  protected readonly checks = resource({
    params: () => [
      this.broker(),
      this.clerkId(),
      this.accountId(),
      this.sid(),
      this.target().bindingGeneration ?? '',
      this.target().routingEpoch ?? '',
    ].join('|'),
    loader: () => this.panelSvc.getOptionBuyingPowerChecks(this.target(), this.sid()),
  });

  /** What the card shows: the newest read, or what a review answered with until a read replaces it. */
  protected readonly page = linkedSignal<OptionBuyingPowerChecks | null>(
    () => (this.checks.hasValue() ? this.checks.value() : null),
  );

  protected readonly confirming = signal(false);
  protected readonly reviewing = signal(false);
  protected readonly busy = computed(() => this.reviewing() || this.locked());
  protected readonly refusal = signal<ActionRejection | null>(null);

  constructor() {
    effect(() => {
      this.journalTailSeq();
      untracked(() => {
        if (this.checks.hasValue()) this.checks.reload();
      });
    });
  }

  /** An amount as a page says it: the backend's dollars, with the sign before the dollar mark. */
  protected money(dollars: string): string {
    return dollars.startsWith('-') ? `-$${dollars.slice(1)}` : `$${dollars}`;
  }

  protected async confirmReview(): Promise<void> {
    this.confirming.set(false);
    if (this.busy()) return;
    this.reviewing.set(true);
    this.refusal.set(null);
    try {
      this.page.set((await this.review()()).state);
    } catch (error) {
      this.refusal.set(deriveActionRejection(error, REVIEW_UNANSWERED));
    } finally {
      this.reviewing.set(false);
    }
  }
}

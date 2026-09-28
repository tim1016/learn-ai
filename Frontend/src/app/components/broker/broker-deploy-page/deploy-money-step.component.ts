import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  model,
  output,
  resource,
  signal,
  untracked,
} from '@angular/core';
import { FormField, form, maxLength, pattern, readonly as readOnly, required } from '@angular/forms/signals';

import type { ResourceTarget } from '../../../fleet/resource-target';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { MoneyBarComponent } from '../money-bar/money-bar.component';
import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeploymentBudgetPreview,
} from '../v2-panel/lib/broker-v2-panel.service';
import { canonicalJson } from './deploy-draft.store';

/** One previewed dollar amount the backend accepted for these exact settings:
 * the `review_token` and `confirmation_text` consent binds to. */
export interface MoneyReview {
  readonly context: string;
  readonly amount: string;
  readonly preview: DeploymentBudgetPreview;
}

/** What a review is valid for: the lane and every setting the preview judged. */
export function budgetReviewContext(target: ResourceTarget, body: DeployBotBody): string {
  return canonicalJson({ target, body });
}

/** The amount stops being typed before it is previewed: one request per
 * settled amount, not one per keystroke. */
const AMOUNT_SETTLE_MS = 400;

/**
 * A price wait is transient: each preview asks IBKR for the instrument, and
 * its quote lands with a later market-data snapshot. Re-check a bounded
 * number of times; "Refresh money" stays the manual retry after that.
 */
const PRICE_RECHECK_DELAY_MS = 2_000;
const PRICE_RECHECK_LIMIT = 15;

/** A positive dollar amount with at most two decimal places. The string is
 * sent as typed; the backend parses it, never the browser. */
const AMOUNT_PATTERN = /^(?=.*[1-9])\d+(\.\d{1,2})?$/;

/**
 * Deploy step 3, Money (PRD #2560 D8/D12).
 *
 * The owner types a dollar budget or takes a server shortcut; each settled
 * amount is previewed, and the account bar grows a NEW slice only from the
 * previewed `money_after` the backend returns. Every dollar string and every
 * slice width on this step is Python-authored — nothing here parses, adds or
 * compares money. A refused amount shows the backend's own sentence, with
 * its exact amounts. Dry Run has no account bar: its simulated starting cash
 * is its own, never the account's.
 */
@Component({
  selector: 'app-deploy-money-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, FormField, MoneyBarComponent, TimestampDisplayComponent],
  templateUrl: './deploy-money-step.component.html',
  styleUrl: './deploy-money-step.component.scss',
})
export class DeployMoneyStepComponent {
  readonly target = input.required<ResourceTarget>();
  /** The settings this budget is for; `null` until What and How are complete. */
  readonly body = input<DeployBotBody | null>(null);
  readonly disabled = input(false);
  /** The owner's typed dollar choice. The host keeps it in the session draft. */
  readonly amount = model('');
  /** The accepted review for the amount on screen, or `null`. */
  readonly reviewed = output<MoneyReview | null>();

  private readonly service = inject(BrokerV2PanelService);

  protected readonly fields = form(this.amount, (amount) => {
    readOnly(amount, () => this.disabled());
    required(amount, { message: 'Enter a dollar budget.' });
    maxLength(amount, 40);
    pattern(amount, AMOUNT_PATTERN, { message: 'Enter a positive dollar amount with at most two decimal places.' });
  });

  protected readonly dryRun = computed(() => this.body()?.execution_mode === 'dry_run');
  private readonly context = computed(() => {
    const body = this.body();
    return body === null ? null : budgetReviewContext(this.target(), body);
  });

  /** The account's money for these settings, with no amount: the facts,
   * the shortcuts and the bar as it stands. */
  protected readonly facts = resource({
    params: () => {
      const body = this.body();
      return body === null ? undefined : { body, target: this.target() };
    },
    loader: ({ params }) => this.service.previewBudget(params.target, params.body),
  });
  protected readonly factsView = computed(() => (this.facts.hasValue() ? this.facts.value() : null));

  /** The amount once typing has paused. A shortcut settles at once. */
  private readonly settledAmount = signal('');

  protected readonly amountPreview = resource({
    params: () => {
      const body = this.body();
      const facts = this.factsView();
      const amount = this.settledAmount();
      if (body === null || facts?.state !== 'ready' || !AMOUNT_PATTERN.test(amount)) return undefined;
      return {
        target: this.target(),
        body: { ...body, budget: { amount_usd: amount, risk_revision: facts.risk_revision ?? 0 } },
      };
    },
    loader: ({ params }) => this.service.previewBudget(params.target, params.body),
  });

  /** The preview of exactly the amount on screen — never an earlier one. */
  protected readonly amountView = computed<DeploymentBudgetPreview | null>(() =>
    this.settledAmount() === this.amount() && this.amountPreview.hasValue() ? this.amountPreview.value() : null,
  );
  protected readonly amountPending = computed(() =>
    AMOUNT_PATTERN.test(this.amount()) && this.factsView()?.state === 'ready' && this.amountView() === null,
  );

  /** The bar the backend drew: with this amount's NEW slice once its preview
   * has answered, otherwise the account as it stands. A refused amount's
   * preview also carries a bar — today's, with no NEW slice — beside the
   * backend's refusal sentence. */
  protected readonly bar = computed(() => this.amountView()?.money_after ?? this.factsView()?.money_after ?? null);

  private readonly ready = computed<MoneyReview | null>(() => {
    const preview = this.amountView();
    const context = this.context();
    if (preview === null || context === null || preview.state !== 'ready' || !preview.review_token) return null;
    return { context, amount: this.amount(), preview };
  });
  /** Only an amount the backend accepted is drawn as "after this Deploy". */
  protected readonly barShowsNew = computed(() => this.ready() !== null);

  private readonly priceRechecks = linkedSignal({ source: this.context, computation: () => 0 });

  constructor() {
    effect(() => this.reviewed.emit(this.ready()));
    effect((onCleanup) => {
      const amount = this.amount();
      const timer = setTimeout(() => this.settledAmount.set(amount), AMOUNT_SETTLE_MS);
      onCleanup(() => clearTimeout(timer));
    });
    effect((onCleanup) => {
      if (this.factsView()?.state !== 'awaiting_price' || this.facts.isLoading()) return;
      if (untracked(this.priceRechecks) >= PRICE_RECHECK_LIMIT) return;
      const timer = setTimeout(() => {
        this.priceRechecks.update((count) => count + 1);
        this.facts.reload();
      }, PRICE_RECHECK_DELAY_MS);
      onCleanup(() => clearTimeout(timer));
    });
  }

  protected choose(amount: string): void {
    this.amount.set(amount);
    this.settledAmount.set(amount);
  }

  protected refresh(): void {
    this.facts.reload();
    if (this.amountPreview.error() !== undefined) this.amountPreview.reload();
  }

  protected shortcutHelpId(key: string): string {
    return `deploy-money-shortcut-${key}`;
  }
}

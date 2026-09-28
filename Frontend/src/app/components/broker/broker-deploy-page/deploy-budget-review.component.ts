import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, resource, signal, untracked } from '@angular/core';
import { FormField, form, maxLength, pattern, readonly as readOnly, required } from '@angular/forms/signals';

import { extractServerMessage } from '../operation-error';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { DEPLOYMENT_WORLD_LABELS, BrokerV2PanelService, type DeployBotBody, type DeploymentBudgetInput, type DeploymentBudgetPreview } from '../v2-panel/lib/broker-v2-panel.service';

export interface ReviewedDeploymentBudget {
  readonly context: string;
  readonly budget: DeploymentBudgetInput;
}

/** The review context includes both the frozen lane and all material terms. */
export function budgetReviewContext(target: ResourceTarget, body: DeployBotBody): string {
  return JSON.stringify({ target, body });
}

@Component({
  selector: 'app-deploy-budget-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, FormField, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './deploy-budget-review.component.html',
  styleUrl: './deploy-budget-review.component.scss',
})
export class DeployBudgetReviewComponent {
  protected readonly worldLabels = DEPLOYMENT_WORLD_LABELS;
  readonly target = input.required<ResourceTarget>();
  readonly body = input<DeployBotBody | null>(null);
  readonly disabled = input(false);
  readonly reviewed = output<ReviewedDeploymentBudget | null>();
  private readonly service = inject(BrokerV2PanelService);
  protected readonly draft = signal({ amount: '', confirmation: '' });
  protected readonly reviewing = signal(false);
  protected readonly fields = form(this.draft, path => {
    readOnly(path.amount, () => this.disabled() || this.reviewing());
    readOnly(path.confirmation, () => this.disabled());
    required(path.amount, { message: 'Enter a dollar budget.' });
    maxLength(path.amount, 40);
    pattern(path.amount, /^(?=.*[1-9])\d+(\.\d{1,2})?$/, { message: 'Enter a positive dollar amount with at most two decimal places.' });
  });
  protected readonly context = computed(() => {
    const body = this.body();
    return body === null ? null : budgetReviewContext(this.target(), body);
  });
  protected readonly evidence = resource({
    params: () => {
      const body = this.body();
      return body === null ? undefined : { body, target: this.target() };
    },
    loader: ({ params }) => this.service.previewBudget(params.target, params.body),
  });
  protected readonly initialView = computed(() => this.evidence.hasValue() ? this.evidence.value() : null);
  protected readonly failure = signal<string | null>(null);
  private readonly amount = computed(() => this.draft().amount);
  private readonly completed = signal<{ context: string; amount: string; view: DeploymentBudgetPreview } | null>(null);
  protected readonly reviewView = computed(() => {
    const completed = this.completed();
    return completed?.context === this.context() && completed?.amount === this.draft().amount ? completed.view : null;
  });
  protected readonly view = computed(() => this.reviewView() ?? this.initialView());
  protected readonly ready = computed<ReviewedDeploymentBudget | null>(() => {
    const view = this.reviewView();
    const context = this.context();
    if (context === null || view?.state !== 'ready' || !view.review_token || typeof view.risk_revision !== 'number' || this.fields.amount().invalid()) return null;
    if (view.confirmation_text && this.draft().confirmation !== view.confirmation_text) return null;
    return { context, budget: { amount_usd: this.draft().amount, risk_revision: view.risk_revision,
      review_token: view.review_token, live_confirmation: view.confirmation_text ? this.draft().confirmation : null } };
  });

  constructor() {
    effect(() => this.reviewed.emit(this.ready()));
    // The dollar choice survives repricing; typed consent never survives a
    // material change or a new review, even if its expected text is unchanged.
    effect(() => {
      const context = this.context();
      const amount = this.amount();
      untracked(() => {
        const prior = this.completed();
        if (prior?.context !== context || prior?.amount !== amount) this.completed.set(null);
        this.draft.update(value => value.confirmation ? { ...value, confirmation: '' } : value);
      });
    });
  }

  protected choose(amount: string): void {
    this.draft.set({ amount, confirmation: '' });
    this.failure.set(null);
  }

  protected refresh(): void {
    this.completed.set(null);
    this.evidence.reload();
  }

  protected async review(): Promise<void> {
    this.fields.amount().markAsTouched();
    const body = this.body();
    const context = this.context();
    if (body === null || context === null || this.fields.amount().invalid() || this.reviewing()) return;
    const amount = this.draft().amount;
    this.completed.set(null);
    this.draft.update(value => ({ ...value, confirmation: '' }));
    this.reviewing.set(true);
    this.failure.set(null);
    try {
      const view = await this.service.previewBudget(this.target(), { ...body,
        budget: { amount_usd: amount, risk_revision: this.initialView()?.risk_revision ?? 0 } });
      if (context === this.context() && amount === this.draft().amount) this.completed.set({ context, amount, view });
    } catch (error) {
      if (context === this.context() && amount === this.draft().amount) {
        this.failure.set(extractServerMessage(error, 'The budget could not be reviewed. Refresh the account evidence and try again.'));
      }
    } finally {
      this.reviewing.set(false);
    }
  }
}

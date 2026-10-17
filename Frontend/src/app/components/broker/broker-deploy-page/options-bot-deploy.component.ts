import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, resource, signal, untracked } from '@angular/core';
import { disabled, FormField, form, pattern, required } from '@angular/forms/signals';
import { ActivatedRoute, Router } from '@angular/router';

import { accountWorkspaceBotRoute } from '../../../fleet/account-workspace';
import { fencedTarget, laneFenceIsEnforceable, type LaneFence } from '../../../fleet/lane-fence';
import { sameResourceTarget, withAccount, withCommand, type ResourceTarget } from '../../../fleet/resource-target';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { refusalBody } from '../../../shared/errors/refusal-body';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { OptionsQualificationEvidenceComponent } from '../../options-backtest/options-qualification-evidence.component';
import { GoldenOptionSetupsService } from '../../options-backtest/golden-option-setups.service';
import { OptionsBacktestService } from '../../options-backtest/options-backtest.service';
import { SetupSentencesComponent } from '../../options-backtest/cards/setup-sentences.component';
import { FundingAnswerComponent } from '../funding-answer/funding-answer.component';
import { MoneyBarComponent } from '../money-bar/money-bar.component';
import { extractServerMessage } from '../operation-error';
import { BrokerV2PanelService, committedReceipt, uncommittedClaim, type BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';
import { canonicalJson } from './deploy-draft.store';
import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import { DeployOptionOrderTermsComponent } from './deploy-option-order-terms.component';
import { OptionsBotDeployService, type OptionsDeploymentReview, type OptionsDeployRequest } from './options-bot-deploy.service';
import { orderTermsSet, startingOrderTerms, type OptionOrderTerms } from './option-structure/option-structure';

interface ReviewedDeployment {
  readonly context: string;
  readonly target: ResourceTarget;
  readonly request: OptionsDeployRequest;
  readonly review: OptionsDeploymentReview;
}

@Component({
  selector: 'app-options-bot-deploy',
  imports: [
    AssetIdentityComponent, AuthoredUsdPipe, OptionsQualificationEvidenceComponent, DeployLaunchReceiptComponent, DeployOptionOrderTermsComponent,
    FormField, FundingAnswerComponent, MoneyBarComponent, SetupSentencesComponent, TimestampDisplayComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-bot-deploy.component.html',
  styleUrl: './options-bot-deploy.component.scss',
})
export class OptionsBotDeployComponent {
  readonly target = input.required<ResourceTarget>();
  readonly accountId = input.required<string>();
  readonly fence = input.required<LaneFence>();
  readonly laneReviewRequired = input(false);
  readonly outstandingChange = output<boolean>();

  private readonly service = inject(OptionsBotDeployService);
  private readonly goldenService = inject(GoldenOptionSetupsService);
  private readonly panels = inject(BrokerV2PanelService);
  private readonly backtests = inject(OptionsBacktestService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  protected readonly busy = signal(false);
  protected readonly unknown = signal(false);
  protected readonly problem = signal<string | null>(null);
  protected readonly receipt = signal<BudgetDeployReceipt | null>(null);
  private readonly fields = signal({ goldenId: this.route.snapshot.queryParamMap.get('options_setup') ?? '', amount: '', mode: '', confirmation: '' });
  protected readonly ticket = form(this.fields, (ticket) => {
    disabled(ticket, () => this.busy() || this.unknown() || this.receipt() !== null);
    required(ticket.goldenId);
    required(ticket.mode);
    required(ticket.amount);
    pattern(ticket.amount, /^(?=.*[1-9])\d+(\.\d{1,2})?$/, { message: 'Enter a positive dollar amount with at most two decimal places.' });
  });
  protected readonly terms = signal<OptionOrderTerms | null>(null);
  private readonly reviewed = signal<ReviewedDeployment | null>(null);
  private submissionKey = this.route.snapshot.queryParamMap.get('options_submission');
  private submittedTarget: ResourceTarget | null = null;
  private reviewGeneration = 0;

  protected readonly address = computed(() => withAccount(fencedTarget(this.target(), this.fence()), this.accountId()), { equal: sameResourceTarget });
  protected readonly context = resource({
    params: () => this.address(),
    loader: ({ params }) => this.panels.getDeployView(params),
  });
  protected readonly catalogue = resource({
    params: () => this.address(),
    loader: ({ params }) => this.panels.getOptionStructures(params),
  });
  protected readonly versions = resource({ loader: () => this.goldenService.list() });
  private readonly selectedVersion = resource({
    params: () => {
      const id = this.fields().goldenId;
      return id && (!this.versions.hasValue() || !this.versions.value().some((item) => item.id === id)) ? id : undefined;
    },
    loader: ({ params }) => this.goldenService.get(params),
  });
  protected readonly versionChoices = computed(() => {
    const recent = this.versions.hasValue() ? this.versions.value() : [];
    const selected = this.selectedVersion.hasValue() ? this.selectedVersion.value() : null;
    return selected !== null && !recent.some((item) => item.id === selected.id) ? [selected, ...recent] : recent;
  });
  protected readonly golden = computed(() => this.versionChoices().find((item) => item.id === this.fields().goldenId) ?? null);
  protected readonly setupWords = resource({
    params: () => this.golden()?.setup,
    loader: ({ params }) => this.backtests.describe(params),
  });
  protected readonly loadProblem = computed(() => {
    const error = this.selectedVersion.error() ?? this.versions.error() ?? this.context.error() ?? this.catalogue.error();
    return error === undefined ? null : extractServerMessage(error, 'The setup or account deployment settings could not be read.');
  });
  protected readonly modes = computed(() => this.context.hasValue() ? this.context.value().execution_modes : []);
  protected readonly orderDefaults = computed(() => this.catalogue.hasValue() ? this.catalogue.value().order_terms : null);
  protected readonly orderNotes = computed(() => this.catalogue.hasValue() ? this.catalogue.value().order_term_notes : null);
  private readonly request = computed<OptionsDeployRequest | null>(() => {
    const fields = this.fields();
    const golden = this.golden();
    const terms = this.terms();
    const exitTerms = this.context.hasValue() ? this.context.value().default_exit_terms : null;
    const mode = this.modes().find((item) => item.mode === fields.mode && item.availability === 'available');
    if (golden?.status !== 'ready' || !mode || !exitTerms || !orderTermsSet(terms, this.orderDefaults())
      || this.ticket.amount().invalid()) return null;
    return {
      golden_setup_id: golden.id, execution_mode: mode.mode, order_terms: terms, exit_terms: exitTerms,
      budget: { amount_usd: fields.amount, risk_revision: 0 }, end: null,
    };
  });
  private readonly reviewContext = computed(() => canonicalJson({ target: this.address(), request: this.request() }));
  protected readonly currentReview = computed(() => {
    const reviewed = this.reviewed();
    return reviewed?.context === this.reviewContext() ? reviewed : null;
  });
  protected readonly mayReview = computed(() => this.request() !== null && !this.busy() && !this.unknown()
    && this.receipt() === null && !this.laneReviewRequired() && laneFenceIsEnforceable(this.fence()));
  protected readonly mayDeploy = computed(() => {
    const reviewed = this.currentReview();
    if (!this.mayReview() || !reviewed?.review.allowed || !reviewed.review.budget.review_token) return false;
    const phrase = reviewed.review.budget.confirmation_text;
    return !phrase || this.fields().confirmation === phrase;
  });
  protected readonly botLink = computed(() => {
    const receipt = this.receipt();
    const target = this.submittedTarget ?? this.address();
    return receipt === null || target.accountId === null ? null : accountWorkspaceBotRoute({ broker: target.broker, clerkId: target.clerkId, accountId: target.accountId }, receipt.strategy_instance_id);
  });

  constructor() {
    effect(() => {
      const defaults = this.orderDefaults();
      if (defaults !== null && untracked(this.terms) === null) this.terms.set(startingOrderTerms(defaults));
    });
    effect(() => {
      const golden = this.golden();
      if (golden !== null) untracked(() => this.fields.update((fields) => ({ ...fields, amount: golden.evidence.starting_cash_usd })));
    });
    effect(() => {
      this.currentReview();
      untracked(() => this.fields.update((fields) => ({ ...fields, confirmation: '' })));
    });
    effect(() => {
      if (!this.context.hasValue()) return;
      const view = this.context.value();
      untracked(() => {
        if (this.fields().mode === '') this.fields.update((fields) => ({ ...fields, mode: view.account_mode === 'paper' ? 'paper' : 'shadow' }));
      });
    });
    effect(() => this.outstandingChange.emit(this.busy() || this.unknown()));
    if (this.submissionKey !== null) {
      this.unknown.set(true);
      this.problem.set('An earlier Options deployment has no confirmed result here. Check its deployment status before sending another.');
    }
  }

  protected reload(): void { this.versions.reload(); this.selectedVersion.reload(); this.context.reload(); this.catalogue.reload(); }

  protected async review(): Promise<void> {
    const request = this.request();
    if (!this.mayReview() || request === null) return;
    const context = this.reviewContext();
    const target = this.address();
    const generation = ++this.reviewGeneration;
    this.busy.set(true);
    this.problem.set(null);
    this.reviewed.set(null);
    try {
      const facts = await this.service.review(target, { ...request, budget: null });
      if (generation !== this.reviewGeneration || context !== this.reviewContext()) return;
      if (facts.budget.risk_revision === undefined) throw new Error('The account did not provide its current risk revision. Review again.');
      const withBudget = { ...request, budget: { amount_usd: this.fields().amount, risk_revision: facts.budget.risk_revision } };
      const review = await this.service.review(target, withBudget);
      if (generation !== this.reviewGeneration || context !== this.reviewContext()) return;
      this.reviewed.set({ context, target, request: withBudget, review });
    } catch (error) {
      if (context === this.reviewContext()) this.problem.set(extractServerMessage(error, 'This setup and account could not be reviewed.'));
    } finally {
      if (generation === this.reviewGeneration) this.busy.set(false);
    }
  }

  protected async deploy(): Promise<void> {
    const reviewed = this.currentReview();
    if (!this.mayDeploy() || reviewed === null || reviewed.request.budget == null) return;
    const key = this.submissionKey ?? crypto.randomUUID();
    this.submissionKey = key;
    this.submittedTarget = withCommand(reviewed.target, 'bot_action', key);
    this.busy.set(true);
    this.unknown.set(true);
    this.problem.set(null);
    try {
      await this.router.navigate([], { relativeTo: this.route, queryParams: { options_submission: key }, queryParamsHandling: 'merge', replaceUrl: true });
      const result = await this.service.deploy(this.submittedTarget, {
        ...reviewed.request, submission_key: key,
        budget: { ...reviewed.request.budget, review_token: reviewed.review.budget.review_token, live_confirmation: this.fields().confirmation || null },
      });
      this.receipt.set(result);
      this.unknown.set(false);
      await this.clearRecoveryLink();
    } catch (error) {
      this.problem.set(extractServerMessage(error, 'The deployment result is unknown. Check deployment status to recover the same request.'));
    } finally {
      this.busy.set(false);
    }
  }

  protected async recover(): Promise<void> {
    if (this.submissionKey === null || this.busy()) return;
    this.busy.set(true);
    try {
      const answer = await this.service.submission(this.submittedTarget ?? this.address(), this.submissionKey);
      const receipt = committedReceipt(answer);
      if (receipt !== null) {
        this.receipt.set(receipt); this.unknown.set(false); this.problem.set(null); await this.clearRecoveryLink();
      } else {
        const claim = uncommittedClaim(answer);
        this.problem.set(claim?.message ?? 'The deployment is still being checked.');
        if (claim?.status === 'not_committed') {
          this.unknown.set(false); this.submissionKey = null; this.reviewed.set(null); await this.clearRecoveryLink();
        }
      }
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 404 && refusalBody(error)?.['code'] === 'deploy_submission_never_claimed') {
        this.unknown.set(false); this.submissionKey = null; this.problem.set('This deployment was never claimed. Review the account before trying again.');
        this.reviewed.set(null);
        await this.clearRecoveryLink();
      } else this.problem.set(extractServerMessage(error, 'The deployment result is still unknown. Check again.'));
    } finally { this.busy.set(false); }
  }

  private clearRecoveryLink(): Promise<boolean> {
    return this.router.navigate([], { relativeTo: this.route, queryParams: { options_submission: null }, queryParamsHandling: 'merge', replaceUrl: true });
  }
}

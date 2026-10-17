import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, resource, signal, untracked } from '@angular/core';
import { disabled, FormField, form } from '@angular/forms/signals';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { laneFenceVerdict } from '../../../../fleet/lane-fence';
import { withCommand, type ResourceTarget } from '../../../../fleet/resource-target';
import { refusalBody } from '../../../../shared/errors/refusal-body';
import { optionContractFromSymbol } from '../../../../shared/option-contract/option-contract-symbol';
import { OptionContractComponent } from '../../../../shared/option-contract/option-contract.component';
import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { SetupSentencesComponent } from '../../../options-backtest/cards/setup-sentences.component';
import { OptionsBacktestService } from '../../../options-backtest/options-backtest.service';
import { canonicalJson } from '../../broker-deploy-page/deploy-draft.store';
import { OptionsBotDeployService, type OptionsBotConfiguration, type OptionsResumeRequest } from '../../broker-deploy-page/options-bot-deploy.service';
import { extractServerMessage } from '../../operation-error';
import type { DeploymentBudgetPreview } from '../lib/broker-v2-panel.service';

interface ResumeAttempt {
  readonly context: string;
  readonly target: ResourceTarget;
  readonly sid: string;
  readonly request: OptionsResumeRequest;
}

@Component({
  selector: 'app-options-bot-session',
  imports: [AuthoredUsdPipe, FormField, OptionContractComponent, ReceiptLabelPipe, SetupSentencesComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-bot-session.component.html',
  styleUrl: './options-bot-session.component.scss',
})
export class OptionsBotSessionComponent {
  readonly target = input.required<ResourceTarget>();
  readonly sid = input.required<string>();
  readonly configuration = input.required<OptionsBotConfiguration>();
  readonly revision = input.required<number>();
  readonly locked = input(false);
  readonly resumed = output();
  readonly pendingChange = output<boolean>();

  private readonly service = inject(OptionsBotDeployService);
  private readonly setups = inject(OptionsBacktestService);
  private readonly directory = inject(FleetDirectoryService);
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);
  protected readonly outcome = signal<string | null>(null);
  private readonly attempt = signal<ResumeAttempt | null>(null);
  protected readonly reviewed = signal<DeploymentBudgetPreview | null>(null);
  private readonly confirmation = signal('');
  protected readonly confirmationForm = form(this.confirmation, (field) => disabled(field, () => this.busy() || this.attempt() !== null));
  private generation = 0;
  private readonly context = computed(() => canonicalJson({ target: this.target(), sid: this.sid(), golden: this.configuration().golden_setup_id }));
  protected readonly session = resource({
    params: () => ({ target: this.target(), sid: this.sid(), revision: this.revision() }),
    loader: ({ params }) => this.service.session(params.target, params.sid),
  });
  protected readonly words = resource({
    params: () => this.configuration().setup,
    loader: ({ params }) => this.setups.describe(params),
  });
  protected readonly legs = computed(() => this.session.hasValue() ? this.session.value().legs.map((leg) => ({
    ...leg, contract: leg.held_contract ? optionContractFromSymbol(leg.held_contract, this.configuration().setup.underlying) : null,
  })) : []);
  protected readonly readProblem = computed(() => this.session.error() === undefined
    ? null : extractServerMessage(this.session.error(), 'This Options session could not be read.'));
  protected readonly fenceVerdict = computed(() => laneFenceVerdict({
    bindingGeneration: this.target().bindingGeneration, routingEpoch: this.target().routingEpoch,
  }, this.directory.lane(this.target().broker, this.target().clerkId)));
  protected readonly mayResume = computed(() => this.session.hasValue() && this.session.value().resume_allowed
    && this.fenceVerdict().ok && !this.locked() && !this.busy() && this.attempt() === null);
  protected readonly mayResumeEntries = computed(() => {
    const review = this.reviewed();
    return this.mayResume() && review?.state === 'ready' && review.review_token != null && review.budget_usd != null
      && (!review.confirmation_text || this.confirmation() === review.confirmation_text);
  });
  protected readonly retryNeeded = computed(() => this.attempt() !== null && !this.busy());

  constructor() {
    effect(() => {
      this.context();
      untracked(() => {
        this.generation += 1; this.reviewed.set(null); this.attempt.set(null);
        this.confirmation.set(''); this.problem.set(null); this.outcome.set(null); this.busy.set(false);
      });
    });
    effect(() => this.pendingChange.emit(this.busy() || this.attempt() !== null));
    const timer = setInterval(() => { if (!this.session.isLoading()) this.session.reload(); }, 5000);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  protected async reviewEntries(): Promise<void> {
    if (!this.mayResume()) return;
    const context = this.context();
    const target = this.target();
    const sid = this.sid();
    const generation = ++this.generation;
    this.busy.set(true); this.problem.set(null); this.reviewed.set(null);
    try {
      const facts = await this.service.reviewResume(target, sid, null);
      if (facts.budget_usd == null || facts.risk_revision === undefined) {
        this.problem.set(facts.detail); return;
      }
      if (context !== this.context()) return;
      const review = await this.service.reviewResume(target, sid, { amount_usd: facts.budget_usd, risk_revision: facts.risk_revision });
      if (context === this.context()) this.reviewed.set(review);
    } catch (error) {
      if (context === this.context()) this.problem.set(extractServerMessage(error, 'The entry budget could not be reviewed.'));
    } finally { if (generation === this.generation) this.busy.set(false); }
  }

  protected async resume(withEntries: boolean): Promise<void> {
    if (!this.mayResume() || (withEntries && !this.mayResumeEntries())) return;
    const key = crypto.randomUUID();
    const review = this.reviewed();
    const budget = withEntries && review?.budget_usd != null && review.risk_revision !== undefined
      ? { amount_usd: review.budget_usd, risk_revision: review.risk_revision,
        review_token: review.review_token, live_confirmation: this.confirmation() || null } : null;
    const attempt: ResumeAttempt = {
      context: this.context(), target: withCommand(this.target(), 'bot_action', key), sid: this.sid(),
      request: { idempotency_key: key, budget },
    };
    this.attempt.set(attempt);
    await this.send(attempt);
  }

  protected async retry(): Promise<void> {
    const attempt = this.attempt();
    if (attempt === null || this.busy() || !this.fenceVerdict().ok || this.locked()) return;
    await this.send(attempt);
  }

  private async send(attempt: ResumeAttempt): Promise<void> {
    const generation = ++this.generation;
    this.busy.set(true); this.problem.set(null);
    try {
      const result = await this.service.resume(attempt.target, attempt.sid, attempt.request);
      if (attempt.context !== this.context()) return;
      this.attempt.set(null); this.reviewed.set(null); this.confirmation.set('');
      this.outcome.set(result.running
        ? (attempt.request.budget ? 'The Options bot resumed with reviewed entry funding.' : 'Position management resumed. New entries and re-entries remain disabled.')
        : 'This recovery request is recorded. The bot is currently stopped; its status shows how it ended.');
      this.session.reload(); this.resumed.emit();
    } catch (error) {
      if (attempt.context === this.context()) {
        this.problem.set(extractServerMessage(error, 'The Resume result is unknown. Retry this same request to recover its result.'));
        if (refusalBody(error)?.['code'] === 'OPTIONS_RESUME_NOT_STARTED') {
          this.attempt.set(null); this.reviewed.set(null); this.confirmation.set('');
        }
      }
    } finally { if (generation === this.generation) this.busy.set(false); }
  }
}

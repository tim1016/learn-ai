import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, signal, untracked } from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { optionsSetupRoute, optionsStudyRoute } from '../../options-setups/options-setups.address';
import { AssetIdentityComponent } from '../../shared/asset-identity';
import type { DateRange } from '../../shared/date-picker';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { HealthNoticeComponent } from '../../shared/scoped-health/health-notice.component';
import { HEALTH_NEEDS } from '../../shared/scoped-health/scoped-health';
import { rangeInstants } from '../options-backtest/duration';
import { OptionsGoldenSearchService, OptionsSearchDispatchError, optionsSearchErrorMessage } from './options-golden-search.service';
import type { PlanRequest, Preflight, SearchDeclarations } from './options-golden-search.types';
import { OptionsSearchKnobsComponent, type KnobSelection } from './options-search-knobs.component';
import { OptionsSearchRulesComponent, type RuleSelection } from './options-search-rules.component';
import { OptionsSearchTimingComponent } from './options-search-timing.component';

interface CaptureState { readonly stamp: string; readonly kind: 'starting' | 'running' | 'checking' | 'done' | 'failed'; readonly jobId?: string; readonly current?: number; readonly total?: number; readonly message?: string }

/**
 * Plans and starts a golden search from one saved setup, as last saved.
 *
 * The seed is the setup the server holds under the saved name, read back as its
 * declarations with the hash of its content. Every check and the start carry that
 * name and hash as `expected_setup_hash`, so a setup saved differently since it was
 * read is refused by the server and never searched. The setup is edited in Define,
 * not here, and a search never starts from a draft.
 *
 * Starting freezes the plan under a key tied to its checked preflight, then asks for
 * the research with the study's own revision; a repeated click or a retry is the same
 * request. Nothing here places an order.
 */
@Component({
  selector: 'app-options-search-plan',
  imports: [HealthNoticeComponent, AssetIdentityComponent, ReceiptLabelPipe, RouterLink, OptionsSearchTimingComponent, OptionsSearchRulesComponent, OptionsSearchKnobsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-plan.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchPlanComponent {
  private readonly service = inject(OptionsGoldenSearchService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  /** A search replays archived option history; its backups decide whether that can be recovered. */
  protected readonly healthNeeds = HEALTH_NEEDS.archivedOptions;

  /** The saved name whose content is the seed. */
  readonly setupName = input.required<string>();
  /** The study this plan forks, when the plan was opened from one. */
  readonly parentStudyId = input<string | undefined>(undefined);

  protected readonly studyRoute = optionsStudyRoute;
  protected readonly defineRoute = computed(() => optionsSetupRoute(this.setupName(), 'define'));
  protected readonly declarations = signal<SearchDeclarations | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly loading = signal(false);
  protected readonly range = signal<DateRange>({ from: '', to: '' });
  protected readonly knobs = signal<readonly KnobSelection[] | null>([]);
  protected readonly timing = signal<readonly KnobSelection[] | null>([]);
  protected readonly dimensions = computed(() => [...(this.timing() ?? []), ...(this.knobs() ?? [])].map((dimension) => ({ path: dimension.path, label: dimension.path.replace('all_legs.', 'All legs · ').replaceAll('_', ' ').replaceAll('.', ' · ') })));
  protected readonly objectiveLabel = computed(() => this.rules()?.policy.rank_by === 'sharpe' ? 'Risk-adjusted return' : this.rules()?.policy.rank_by === 'drawdown' ? 'Lower drawdown' : 'Net profit');
  protected readonly rules = signal<RuleSelection | null>(null);
  protected readonly checked = signal<{ readonly stamp: string; readonly value: Preflight } | null>(null);
  protected readonly captureState = signal<CaptureState | null>(null);
  protected readonly request = computed<PlanRequest | null>(() => {
    const declarations = this.declarations();
    const rules = this.rules();
    const range = rangeInstants(this.range());
    if (declarations === null || rules === null || range === null || this.timing() === null || this.knobs() === null || ((this.knobs()?.length ?? 0) === 0 && this.timing()?.length === 0)) return null;
    return { setup_name: declarations.setup_name, expected_setup_hash: declarations.setup_hash, ...(this.parentStudyId() ? { parent_study_id: this.parentStudyId() } : {}), knobs: [...(this.timing() ?? []), ...(this.knobs() ?? [])], method: rules.method, policy: rules.policy, history: { ...range, train_months: rules.train_months, test_months: rules.test_months, final_months: rules.final_months }, budget: rules.budget, finalist_count: rules.finalist_count, stress: rules.stress, zoom: rules.zoom, pair_audits: rules.pair_audits };
  });
  protected readonly preflight = computed(() => this.checked()?.stamp === JSON.stringify(this.request()) ? this.checked()?.value ?? null : null);
  protected readonly captureForPlan = computed(() => {
    const capture = this.captureState();
    return capture !== null && (['starting', 'running', 'checking'].includes(capture.kind) || capture.stamp === JSON.stringify(this.request())) ? capture : null;
  });
  protected readonly capturing = computed(() => ['starting', 'running', 'checking'].includes(this.captureState()?.kind ?? ''));
  protected readonly canLock = computed(() => this.request() !== null && this.preflight()?.can_lock === true && !this.busy() && !this.capturing());
  private generation = 0;
  private readonly keys = new Map<string, string>();
  private captureUnsubscribe: (() => void) | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => this.captureUnsubscribe?.());
    effect(() => {
      const request = this.request();
      if (request === null || this.capturing()) return;
      const active = this.service.activeHistoryCapture(request);
      if (active !== null && this.captureState()?.jobId !== active.id) {
        untracked(() => this.watchHistoryCapture(active.id, JSON.stringify(request), active.current, active.total));
      }
    });
    effect(() => {
      const name = this.setupName();
      untracked(() => void this.loadSetup(name));
    });
    this.destroyRef.onDestroy(() => { this.generation += 1; });
  }

  private async loadSetup(name: string): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set(null);
    this.declarations.set(null);
    this.range.set({ from: '', to: '' });
    this.checked.set(null);
    this.knobs.set([]);
    this.timing.set([]);
    this.rules.set(null);
    try {
      const declarations = await this.service.declarations(name);
      if (generation === this.generation) this.declarations.set(declarations);
    } catch (error) {
      if (generation === this.generation) this.error.set(optionsSearchErrorMessage(error, 'This saved setup could not be prepared for search.'));
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  protected retry(): void {
    void this.loadSetup(this.setupName());
  }

  protected async checkPlan(): Promise<void> {
    const request = this.request();
    if (request === null || this.busy()) return;
    const stamp = JSON.stringify(request);
    this.busy.set(true);
    this.error.set(null);
    try {
      const value = await this.service.preflight(request);
      if (JSON.stringify(this.request()) === stamp) this.checked.set({ stamp, value });
    } catch (error) {
      this.error.set(optionsSearchErrorMessage(error, 'Preflight could not verify the plan.'));
    } finally { this.busy.set(false); }
  }

  protected async captureHistory(): Promise<void> {
    const request = this.request();
    if (request === null || !this.preflight()?.missing_history || this.capturing()) return;
    const stamp = JSON.stringify(request);
    this.captureState.set({ stamp, kind: 'starting' });
    try {
      await this.service.refreshHistoryCaptures();
      if (this.destroyRef.destroyed) return;
      const active = this.service.activeHistoryCapture(request);
      if (active !== null) {
        this.watchHistoryCapture(active.id, stamp, active.current, active.total);
        return;
      }
      const jobId = await this.service.captureHistory(request);
      if (this.destroyRef.destroyed) return;
      this.watchHistoryCapture(jobId, stamp);
    } catch (error) {
      if (!this.destroyRef.destroyed) this.captureState.set({ stamp, kind: 'failed', message: optionsSearchErrorMessage(error, 'History capture could not start.') });
    }
  }

  private watchHistoryCapture(jobId: string, stamp: string, current?: number, total?: number): void {
    this.captureUnsubscribe?.();
    this.captureState.set({ stamp, kind: 'running', jobId, current, total });
    const unsubscribe = this.service.onHistoryCapture(jobId, (event) => {
      if (event.type === 'job.progress') {
        const current = typeof event['current'] === 'number' ? event['current'] : undefined;
        const total = typeof event['total'] === 'number' ? event['total'] : undefined;
        this.captureState.set({ stamp, kind: 'running', jobId, current, total });
      } else if (event.type === 'job.completed') {
        this.captureUnsubscribe?.();
        this.captureUnsubscribe = null;
        this.captureState.set({ stamp, kind: 'checking' });
        if (JSON.stringify(this.request()) === stamp) {
          void this.checkPlan().finally(() => this.captureState.set({ stamp, kind: 'done' }));
        } else this.captureState.set({ stamp, kind: 'done' });
      } else if (event.type === 'job.failed' || event.type === 'job.cancelled') {
        this.captureUnsubscribe?.();
        this.captureUnsubscribe = null;
        this.captureState.set({ stamp, kind: 'failed', message: event.type === 'job.cancelled' ? 'History capture was cancelled.' : typeof event['message'] === 'string' ? event['message'] : 'History could not be captured.' });
      }
    });
    this.captureUnsubscribe = unsubscribe;
    if (this.captureState()?.kind !== 'running') {
      unsubscribe();
      this.captureUnsubscribe = null;
    }
  }

  protected async cancelHistoryCapture(): Promise<void> {
    const state = this.captureState();
    if (state?.kind !== 'running' || !state.jobId) return;
    try {
      await this.service.cancelHistoryCapture(state.jobId);
    } catch (error) {
      this.captureState.set({ ...state, message: optionsSearchErrorMessage(error, 'The capture could not be cancelled. Check its job status.') });
    }
  }

  /**
   * Freezes the checked plan, asks for its research, and opens the study. The freeze is keyed by the
   * checked preflight and the research request by the study's revision, so starting again after a
   * failure reuses both and cannot make a second study. The research is asked before the study opens,
   * so the page that opens it already reads the study as running.
   */
  protected async lock(): Promise<void> {
    const request = this.request();
    const checked = this.preflight();
    if (!this.canLock() || request === null || checked === null) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      const study = await this.service.create(request, checked.preflight_hash, this.key(`lock:${checked.preflight_hash}`));
      if (study.allowed_commands.includes('run_research')) {
        try {
          await this.service.command(study.id, { command: 'run_research', expected_revision: study.revision, idempotency_key: this.key(`${study.id}:${study.revision}:run_research:{}`) });
        } catch (error) {
          // The research was committed but its job did not start: the study is there to resume. Any other failure leaves
          // the plan here to start again, which reuses the same study and the same request.
          if (!(error instanceof OptionsSearchDispatchError)) {
            this.error.set(optionsSearchErrorMessage(error, 'The plan was frozen, but its research could not be requested. Start the search again to reuse the same study.'));
            return;
          }
        }
      }
      await this.router.navigate(this.studyRoute(study.id));
    } catch (error) {
      this.error.set(optionsSearchErrorMessage(error, 'The plan could not be locked.'));
    } finally { this.busy.set(false); }
  }

  private key(intent: string): string {
    const existing = this.keys.get(intent);
    if (existing !== undefined) return existing;
    const key = crypto.randomUUID();
    this.keys.set(intent, key);
    return key;
  }
}

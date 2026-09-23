import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  type ElementRef,
  inject,
  Injector,
  input,
  output,
  signal,
  type Signal,
  untracked,
  viewChild,
} from '@angular/core';

import { Drawer } from 'primeng/drawer';

import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { CohortDrawerPresentation } from '../lib/cohort-drawer-presentation';
import { deriveActionRejection } from '../lib/panel-action-outcome';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { LANE_FENCE_REFRESH_FAILED_MESSAGE } from '../../../../fleet/lane-fence';
import { type ResourceTarget, withCommand } from '../../../../fleet/resource-target';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import type {
  CohortActionResult,
  CohortFlattenCohort,
  CohortFlattenLeg,
  CohortFlattenRequest,
  CohortFlattenView,
} from '../lib/broker-v2-panel.types';
import { COHORT_FLATTEN_COPY, MAX_COHORT_FLATTEN_LEGS } from './cohort-flatten-confirmation';
import { CohortFlattenGroupComponent } from './cohort-flatten-group.component';
import { CohortFlattenOutcomeComponent } from './cohort-flatten-outcome.component';
import { sameKeyRetryCanChange } from './cohort-flatten-retry';

/** An armed leg, proven to carry the identity its POST is checked against. */
type ArmedFlattenLeg = CohortFlattenLeg & {
  action_id: NonNullable<CohortFlattenLeg['action_id']>;
  revision: number;
  concurrency_token: string;
};

function isArmed(leg: CohortFlattenLeg): leg is ArmedFlattenLeg {
  return (
    leg.enabled &&
    leg.action_id !== null &&
    leg.revision !== null &&
    leg.concurrency_token !== null
  );
}

function cohortKey(cohort: CohortFlattenCohort): string {
  return `${cohort.strategy_key}::${cohort.symbol}`;
}

/** A cohort's armed legs, up to what one POST may carry. */
function defaultSelection(armed: readonly ArmedFlattenLeg[]): ReadonlySet<string> {
  return new Set(
    armed.slice(0, MAX_COHORT_FLATTEN_LEGS).map((leg) => leg.strategy_instance_id),
  );
}

/**
 * Everything the confirmation shows, frozen when the operator asked to
 * review — its legs and symbol — so nothing a later
 * read or directory refresh does can change or silently drop what they
 * confirm.
 */
interface PendingWave {
  readonly legs: readonly ArmedFlattenLeg[];
  readonly symbol: string;
  readonly heading: string;
  readonly message: string;
  readonly confirmLabel: string;
}

/**
 * One confirmed wave: the exact request sent and the target it was sent to.
 * A retry re-sends this object unchanged — same durable key, same legs, same
 * tokens — so applied legs replay as no-ops and only released legs retry.
 */
interface SentWave {
  readonly target: ResourceTarget;
  readonly request: CohortFlattenRequest;
}

/**
 * Why a POST returned no typed batch result. `unknown` is a transport
 * failure — the batch may or may not have run, so a same-key retry is the
 * cure. `refused` is a typed refusal of the whole request (a lane-fence 409,
 * a scope refusal): nothing ran, and the same request would be refused again.
 */
type DispatchFailure =
  | { readonly kind: 'unknown' }
  | {
      readonly kind: 'refused';
      readonly message: string;
      readonly why: string | null;
      readonly reasonCode: string | null;
    };

function dispatchFailureOf(error: unknown): DispatchFailure {
  const rejection = deriveActionRejection(error, COHORT_FLATTEN_COPY.requestFallback);
  if (rejection.outcome === 'unknown') return { kind: 'unknown' };
  return {
    kind: 'refused',
    message: rejection.message,
    why: rejection.why,
    reasonCode: rejection.reasonCode,
  };
}

/**
 * Flatten a stranded cohort, N attributed per-bot legs behind one affordance
 * (ADR 0051, #1909).
 *
 * Presentation is backend-authored: the drawer renders the cohorts and legs
 * it is sent and never decides which bot can flatten — a leg is selectable
 * iff the backend armed it, a cohort's button arms iff one of its legs is
 * armed (ADR 0047).
 *
 * A wave is one cohort. Its selection defaults to that cohort's armed legs,
 * the operator may deselect, and the POST names exactly the confirmed legs
 * with the facts they were presented with (Decision 2). The durable key is
 * minted when a wave is confirmed and belongs to that wave: Retry re-sends it
 * verbatim, and a new wave — only possible from a presentation read STARTED
 * after the last dispatch settled — mints its own.
 */
@Component({
  selector: 'app-cohort-flatten-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Drawer,
    ReceiptLabelPipe,
    TypedHaltConfirmComponent,
    CohortFlattenGroupComponent,
    CohortFlattenOutcomeComponent,
  ],
  templateUrl: './cohort-flatten-drawer.component.html',
  styleUrl: './cohort-flatten-drawer.component.scss',
})
export class CohortFlattenDrawerComponent {
  readonly visible = input.required<boolean>();
  readonly broker = input.required<string>();
  readonly clerkId = input.required<string>();
  readonly accountId = input.required<string>();

  readonly closed = output();
  /** Emitted after any leg applied or replayed, so the roster re-reads. */
  readonly flattened = output();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly injector = inject(Injector);

  protected readonly copy = COHORT_FLATTEN_COPY;
  protected readonly refreshFailedMessage = LANE_FENCE_REFRESH_FAILED_MESSAGE;
  protected readonly maxLegs = MAX_COHORT_FLATTEN_LEGS;
  protected readonly keyOf = cohortKey;

  /** The presentation read and the command lane frozen at open (#2068). */
  private readonly presentation = new CohortDrawerPresentation({
    visible: this.visible,
    broker: this.broker,
    clerkId: this.clerkId,
    accountId: this.accountId,
    load: (target) => this.panelService.getCohortFlattenView(target),
  });
  protected readonly view: Signal<CohortFlattenView | null> = this.presentation.view;
  protected readonly loading = this.presentation.loading;
  protected readonly loadFailed = this.presentation.loadFailed;
  protected readonly laneConflict = this.presentation.conflict;
  protected readonly laneConflictMessage = this.presentation.conflictMessage;

  /** The operator's explicit choice; `null` means the default wave. */
  private readonly choice = signal<{ key: string; selected: ReadonlySet<string> } | null>(null);
  protected readonly pending = signal<PendingWave | null>(null);
  private readonly sent = signal<SentWave | null>(null);
  protected readonly outcome = signal<CohortActionResult | null>(null);
  /** Why the last POST returned no typed batch result, when it did not. */
  protected readonly dispatchFailure = signal<DispatchFailure | null>(null);
  /** The mitigating directory refresh after a fence refusal itself failed. */
  protected readonly directoryRefreshFailed = signal(false);
  protected readonly submitting = signal(false);
  /**
   * The read watermark when the last POST settled. A new wave needs a
   * presentation from a read STARTED after it: a read already in flight when
   * the POST resolved may carry pre-flatten facts.
   */
  private readonly lastDispatchRead = signal<number | null>(null);

  private readonly reviewButton = viewChild<ElementRef<HTMLButtonElement>>('reviewButton');
  private readonly progress = viewChild<ElementRef<HTMLElement>>('progress');
  private readonly failureAlert = viewChild<ElementRef<HTMLElement>>('failureAlert');
  private readonly outcomePanel = viewChild(CohortFlattenOutcomeComponent);

  protected readonly emptyMessage = computed(() => {
    const cohorts = this.view()?.cohorts;
    if (this.loading() || this.loadFailed() || cohorts === undefined) return null;
    return cohorts.length === 0 ? COHORT_FLATTEN_COPY.empty : null;
  });

  /** The wave's cohort: the operator's choice, else the first one with an armed leg. */
  protected readonly activeCohort = computed<CohortFlattenCohort | null>(() => {
    const cohorts = this.view()?.cohorts ?? [];
    const chosen = this.choice();
    const picked = chosen && cohorts.find((cohort) => cohortKey(cohort) === chosen.key);
    return picked || (cohorts.find((cohort) => cohort.legs.some(isArmed)) ?? null);
  });

  protected readonly activeKey = computed(() => {
    const cohort = this.activeCohort();
    return cohort === null ? null : cohortKey(cohort);
  });

  private readonly armedLegs = computed<readonly ArmedFlattenLeg[]>(
    () => this.activeCohort()?.legs.filter(isArmed) ?? [],
  );

  /** The active cohort has more armed legs than one POST may carry. */
  protected readonly overCap = computed(() => this.armedLegs().length > MAX_COHORT_FLATTEN_LEGS);

  /** Explicit when the operator has touched the wave, else its armed legs up to the cap. */
  protected readonly selected = computed<ReadonlySet<string>>(() => {
    const chosen = this.choice();
    if (chosen !== null && chosen.key === this.activeKey()) return chosen.selected;
    return defaultSelection(this.armedLegs());
  });

  /** Derived, so a selection can never outlive the armed leg it named. */
  protected readonly selectedLegs = computed<readonly ArmedFlattenLeg[]>(() => {
    const chosen = this.selected();
    return this.armedLegs().filter((leg) => chosen.has(leg.strategy_instance_id));
  });

  protected readonly capReached = computed(
    () => this.selectedLegs().length >= MAX_COHORT_FLATTEN_LEGS,
  );

  protected readonly canReview = computed(() => {
    const watermark = this.lastDispatchRead();
    const read = this.presentation.read();
    return (
      this.selectedLegs().length > 0 &&
      this.selectedLegs().length <= MAX_COHORT_FLATTEN_LEGS &&
      this.pending() === null &&
      !this.submitting() &&
      !this.laneConflict() &&
      read !== null &&
      // The one post-dispatch gate. A reload keeps showing the prior read
      // while it runs, and that read is at or below the watermark, so this
      // alone holds Review shut until a read started after the POST lands.
      (watermark === null || read > watermark)
    );
  });

  protected readonly canRetry = computed(() => {
    const sent = this.sent();
    if (sent === null || this.submitting() || this.laneConflict()) return false;
    const failure = this.dispatchFailure();
    // A typed batch refusal ran nothing and will refuse the same request
    // again; only a transport failure leaves a same-key retry useful.
    if (failure !== null) return failure.kind === 'unknown';
    const outcome = this.outcome();
    return outcome !== null && sameKeyRetryCanChange(outcome, sent.request.legs.length);
  });

  protected readonly sentSids = computed(
    () => this.sent()?.request.legs.map((leg) => leg.strategy_instance_id) ?? [],
  );

  constructor() {
    // A lane conflict arising under the open confirmation closes it: the
    // operator must close and reopen the drawer to act (#2068, decision 10).
    effect(() => {
      if (this.laneConflict() && untracked(this.pending) !== null) this.pending.set(null);
    });
  }

  protected chooseCohort(cohort: CohortFlattenCohort): void {
    this.choice.set({
      key: cohortKey(cohort),
      selected: defaultSelection(cohort.legs.filter(isArmed)),
    });
  }

  protected toggle(leg: CohortFlattenLeg): void {
    const key = this.activeKey();
    if (key === null || !isArmed(leg)) return;
    const next = new Set(this.selected());
    if (!next.delete(leg.strategy_instance_id)) {
      if (next.size >= MAX_COHORT_FLATTEN_LEGS) return;
      next.add(leg.strategy_instance_id);
    }
    this.choice.set({ key, selected: next });
  }

  protected review(): void {
    const cohort = this.activeCohort();
    if (!this.canReview() || cohort === null) return;
    const legs = this.selectedLegs();
    this.pending.set({
      legs,
      symbol: cohort.symbol,
      heading: COHORT_FLATTEN_COPY.confirmHeading(legs.length),
      message: COHORT_FLATTEN_COPY.confirmMessage(this.accountId(), cohort.strategy_label, legs),
      confirmLabel: COHORT_FLATTEN_COPY.confirmLabel(legs.length),
    });
  }

  protected cancelReview(): void {
    this.pending.set(null);
    this.focusAfterRender(() => this.reviewButton()?.nativeElement.focus());
  }

  protected async confirm(): Promise<void> {
    const pending = this.pending();
    this.pending.set(null);
    // A conflict that raced the click states itself in the drawer's alert;
    // nothing is sent against a lane the operator did not see (#2068).
    if (pending === null || this.laneConflict()) return;
    const frozen = this.presentation.frozenTarget();
    if (frozen === null) {
      // Unreachable while the lane is unconflicted: an unenforceable fence
      // raises the conflict above. Refuse loudly rather than drop the click.
      throw new Error('The confirmed cohort flatten has no frozen lane to dispatch against.');
    }
    const idempotencyKey = crypto.randomUUID();
    const wave: SentWave = {
      target: withCommand(frozen, 'bot_action', idempotencyKey),
      request: {
        idempotency_key: idempotencyKey,
        reason: 'Cohort flatten from the bots roster',
        legs: pending.legs.map((leg) => ({
          strategy_instance_id: leg.strategy_instance_id,
          action_id: leg.action_id,
          revision: leg.revision,
          concurrency_token: leg.concurrency_token,
        })),
      },
    };
    this.sent.set(wave);
    this.choice.set(null);
    this.outcome.set(null);
    await this.send(wave);
  }

  protected async retry(): Promise<void> {
    const wave = this.sent();
    if (!this.canRetry() || wave === null) return;
    await this.send(wave);
  }

  protected onClose(): void {
    // Every route out of the drawer comes through here, so the next open
    // starts a new wave: nothing that could act on a bot outlives the drawer.
    this.choice.set(null);
    this.pending.set(null);
    this.sent.set(null);
    this.outcome.set(null);
    this.dispatchFailure.set(null);
    this.directoryRefreshFailed.set(false);
    this.lastDispatchRead.set(null);
    this.closed.emit();
  }

  private async send(wave: SentWave): Promise<void> {
    this.submitting.set(true);
    this.dispatchFailure.set(null);
    this.focusAfterRender(() => this.progress()?.nativeElement.focus());
    try {
      const result = await this.panelService.runCohortFlatten(wave.target, wave.request);
      this.outcome.set(result);
      if (result.applied_count + result.replayed_count > 0) this.flattened.emit();
      this.focusAfterRender(() => this.outcomePanel()?.focus());
    } catch (error) {
      // Keep the last typed outcome: its receipts are proof of what already
      // ran, and a failed retry proves nothing about them.
      const failure = dispatchFailureOf(error);
      this.dispatchFailure.set(failure);
      if (failure.kind === 'refused' && failure.reasonCode === 'clerk_binding_generation_conflict') {
        // The fence the operator acted on is provably stale: re-read the
        // directory so the next wave is minted against a lane they can see.
        this.refreshDirectoryAfterFenceRefusal();
      }
      this.focusAfterRender(() => this.failureAlert()?.nativeElement.focus());
    } finally {
      this.submitting.set(false);
      // Watermark first: only a read started from here on may arm a new wave.
      this.lastDispatchRead.set(this.presentation.readsStarted());
      this.presentation.reload();
    }
  }

  private refreshDirectoryAfterFenceRefusal(): void {
    void this.fleetDirectory.refresh().catch(() => this.directoryRefreshFailed.set(true));
  }

  private focusAfterRender(focus: () => void): void {
    afterNextRender({ write: focus }, { injector: this.injector });
  }
}

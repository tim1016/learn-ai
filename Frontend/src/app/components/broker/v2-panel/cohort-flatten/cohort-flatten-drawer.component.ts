import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  type ElementRef,
  inject,
  Injector,
  input,
  output,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { Drawer } from 'primeng/drawer';

import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { cohortDrawerLane } from '../lib/cohort-drawer-lane';
import { type ResourceTarget, withCommand } from '../../../../fleet/resource-target';
import type {
  CohortActionResult,
  CohortFlattenCohort,
  CohortFlattenLeg,
  CohortFlattenRequest,
  CohortFlattenView,
} from '../lib/broker-v2-panel.types';
import {
  cohortFlattenConfirmation,
  FLATTEN_CONFIRM_TOKEN,
  type CohortFlattenConfirmation,
} from './cohort-flatten-confirmation';
import { CohortFlattenGroupComponent } from './cohort-flatten-group.component';
import { CohortFlattenOutcomeComponent } from './cohort-flatten-outcome.component';

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

/** The legs frozen when the operator asked to review them. */
interface PendingWave {
  readonly legs: readonly ArmedFlattenLeg[];
  readonly copy: CohortFlattenConfirmation;
}

/**
 * One confirmed wave: the exact request sent and the target it was sent to.
 * A retry re-sends this object unchanged — same durable key, same legs, same
 * tokens — so applied legs replay as no-ops and only released legs retry.
 */
interface SentWave {
  readonly target: ResourceTarget;
  readonly request: CohortFlattenRequest;
  /** The presentation read the wave was confirmed from; a new wave needs a later one. */
  readonly presentationRead: number;
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
 * verbatim, and a new wave — only possible from a presentation read after the
 * last one was sent — mints its own.
 */
@Component({
  selector: 'app-cohort-flatten-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Drawer,
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
  private readonly injector = inject(Injector);

  protected readonly confirmToken = FLATTEN_CONFIRM_TOKEN;

  /** The command lane frozen at open; see `cohortDrawerLane` (#2068). */
  private readonly lane = cohortDrawerLane({
    visible: this.visible,
    broker: this.broker,
    clerkId: this.clerkId,
    accountId: this.accountId,
  });
  protected readonly laneConflict = this.lane.conflict;
  protected readonly laneConflictMessage = this.lane.conflictMessage;

  /** The operator's explicit choice; `null` means the default wave. */
  private readonly choice = signal<{ key: string; selected: ReadonlySet<string> } | null>(null);
  protected readonly pending = signal<PendingWave | null>(null);
  private readonly sent = signal<SentWave | null>(null);
  protected readonly outcome = signal<CohortActionResult | null>(null);
  /** A POST that never returned a typed batch result — network, auth, 5xx. */
  protected readonly submitError = signal(false);
  protected readonly submitting = signal(false);

  private readonly reviewButton = viewChild<ElementRef<HTMLButtonElement>>('reviewButton');
  private readonly outcomePanel = viewChild(CohortFlattenOutcomeComponent);

  /** Numbers each presentation read, so "read after the wave" is a fact, not an identity guess. */
  private reads = 0;
  private readonly flattenable = resource({
    params: () => (this.visible() ? { target: this.lane.readTarget() } : undefined),
    loader: async ({ params }) => ({
      view: await this.panelService.getCohortFlattenView(params.target),
      read: ++this.reads,
    }),
  });

  private readonly presentation = computed(() => {
    const value = this.flattenable.hasValue() ? this.flattenable.value() : null;
    // Never hand back another account's legs: the resource keeps its previous
    // value across a params change, and these legs carry act-on-me tokens.
    return value?.view.account_id === this.accountId() ? value : null;
  });
  protected readonly view = computed<CohortFlattenView | null>(
    () => this.presentation()?.view ?? null,
  );
  protected readonly loading = computed(() => this.flattenable.isLoading());
  protected readonly loadFailed = computed(() => this.flattenable.error() !== undefined);

  protected readonly emptyMessage = computed(() => {
    const cohorts = this.view()?.cohorts;
    if (this.loading() || this.loadFailed() || cohorts === undefined) return null;
    return cohorts.length === 0
      ? 'No cohort on this account has two or more bots on the same strategy and symbol.'
      : null;
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

  /** Explicit when the operator has touched the wave, else its armed legs. */
  protected readonly selected = computed<ReadonlySet<string>>(() => {
    const chosen = this.choice();
    if (chosen !== null && chosen.key === this.activeKey()) return chosen.selected;
    return new Set(this.armedLegs().map((leg) => leg.strategy_instance_id));
  });

  /** Derived, so a selection can never outlive the armed leg it named. */
  protected readonly selectedLegs = computed<readonly ArmedFlattenLeg[]>(() => {
    const chosen = this.selected();
    return this.armedLegs().filter((leg) => chosen.has(leg.strategy_instance_id));
  });

  protected readonly canReview = computed(() => {
    const sent = this.sent();
    return (
      this.selectedLegs().length > 0 &&
      !this.submitting() &&
      !this.loading() &&
      !this.laneConflict() &&
      // A new wave needs a presentation read after the last one was sent.
      (sent === null || sent.presentationRead !== this.presentation()?.read)
    );
  });

  protected readonly canRetry = computed(() => {
    const outcome = this.outcome();
    if (this.sent() === null || this.submitting() || this.laneConflict()) return false;
    if (this.submitError()) return true;
    if (outcome === null) return false;
    return (
      outcome.refused_count + outcome.failed_count > 0 ||
      outcome.legs.length < (this.sent()?.request.legs.length ?? 0)
    );
  });

  protected readonly sentSids = computed(
    () => this.sent()?.request.legs.map((leg) => leg.strategy_instance_id) ?? [],
  );

  protected chooseCohort(cohort: CohortFlattenCohort): void {
    this.choice.set({
      key: cohortKey(cohort),
      selected: new Set(cohort.legs.filter(isArmed).map((leg) => leg.strategy_instance_id)),
    });
  }

  protected toggle(leg: CohortFlattenLeg): void {
    const key = this.activeKey();
    if (key === null || !isArmed(leg)) return;
    const next = new Set(this.selected());
    if (!next.delete(leg.strategy_instance_id)) next.add(leg.strategy_instance_id);
    this.choice.set({ key, selected: next });
  }

  protected review(): void {
    const cohort = this.activeCohort();
    if (!this.canReview() || cohort === null) return;
    const legs = this.selectedLegs();
    this.pending.set({
      legs,
      copy: cohortFlattenConfirmation(this.accountId(), cohort.strategy_label, legs),
    });
  }

  protected cancelReview(): void {
    this.pending.set(null);
    this.focusAfterRender(() => this.reviewButton()?.nativeElement.focus());
  }

  protected async confirm(): Promise<void> {
    const pending = this.pending();
    const presented = this.lane.presentedTarget();
    const presentation = this.presentation();
    this.pending.set(null);
    if (pending === null || presented === null || presentation === null) return;
    const idempotencyKey = crypto.randomUUID();
    const wave: SentWave = {
      target: withCommand(presented, 'bot_action', idempotencyKey),
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
      presentationRead: presentation.read,
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
    this.submitError.set(false);
    this.closed.emit();
  }

  private async send(wave: SentWave): Promise<void> {
    this.submitting.set(true);
    this.submitError.set(false);
    try {
      const result = await this.panelService.runCohortFlatten(wave.target, wave.request);
      this.outcome.set(result);
      if (result.applied_count + result.replayed_count > 0) this.flattened.emit();
      this.focusAfterRender(() => this.outcomePanel()?.focus());
    } catch {
      // No typed batch result came back, so no leg outcome is known. Say so,
      // and keep the wave so a retry re-sends it under the same key rather
      // than minting a second identity for a command that may have run.
      this.outcome.set(null);
      this.submitError.set(true);
    } finally {
      this.submitting.set(false);
      // Re-read so each leg shows its current facts. `reload` keeps the prior
      // value on screen under the outcome the operator is reading.
      this.flattenable.reload();
    }
  }

  private focusAfterRender(focus: () => void): void {
    afterNextRender({ write: focus }, { injector: this.injector });
  }
}

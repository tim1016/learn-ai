import { CurrencyPipe, PercentPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  resource,
  untracked,
  viewChild,
} from '@angular/core';
import { FormField, form } from '@angular/forms/signals';

import type { components } from '../../../../api/broker.types';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';
import { AlpacaLiveVerdictService } from '../../../../services/alpaca-live-verdict.service';
import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BrokerConfigurationService } from './broker-configuration.service';
import { ConfigurationRefusalComponent } from './configuration-refusal.component';
import { type ConfigurationRefusal, toConfigurationRefusal } from './broker-configuration-refusal';

/** The hold status a loss-limit read reports, in words: status is never carried
 * by colour alone. The backend's own `detail` follows each. */
const ENTRY_STATE_LABEL = {
  held: 'On hold.',
  ready: 'No hold.',
  unknown: 'New entries wait.',
} as const;

/** What a finished action changed, stated where focus lands afterwards. */
const OUTCOME_COPY = {
  applied: 'Applied. New entries use this limit now; no restart or redeploy is needed.',
  cleared: 'Hold cleared. The status above is the account’s current state.',
} as const;
type Outcome = keyof typeof OUTCOME_COPY;

type AccountRiskState = components['schemas']['AccountRiskStateResponse'];
type ReviewedRisk = components['schemas']['AccountRiskClearRequest'];
type LossHold = components['schemas']['AlpacaLiveVerdict']['loss_hold'];

let nextFieldId = 0;

/**
 * The Daily loss limit section of Settings (PRD #2560): the limit in force,
 * the loss hold beside it, Apply (effective immediately) and Clear hold.
 *
 * The section re-reads itself when the lane's loss-hold state changes — the
 * shell's live verdict, polled for every lane, carries it — so a hold raised
 * while Settings is open appears without a reload (#2552). A change that lands
 * while an action is in flight is kept and re-read once the action settles.
 */
@Component({
  selector: 'app-configuration-risk-limits',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField, CurrencyPipe, PercentPipe, AuthoredUsdPipe, ConfigurationRefusalComponent, TimestampDisplayComponent],
  templateUrl: './configuration-risk-limits.component.html',
  styleUrl: './configuration-risk-limits.component.scss',
})
export class ConfigurationRiskLimitsComponent {
  readonly clerkId = input.required<string>();
  private readonly service = inject(BrokerConfigurationService);
  private readonly directory = inject(FleetDirectoryService);
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);
  private readonly injector = inject(Injector);
  private readonly outcomeRegion = viewChild<ElementRef<HTMLElement>>('outcomeRegion');
  private readonly refusalRegion = viewChild<ElementRef<HTMLElement>>('refusalRegion');

  protected readonly ids = (() => {
    const id = nextFieldId++;
    return { fraction: `loss-limit-fraction-${id}`, cap: `loss-limit-cap-${id}` };
  })();
  protected readonly ENTRY_STATE_LABEL = ENTRY_STATE_LABEL;
  protected readonly OUTCOME_COPY = OUTCOME_COPY;

  protected readonly state = resource({
    params: () => this.clerkId(),
    loader: ({ params }) => this.service.readRiskLimits(params),
  });
  // Seeded from a `computed` with an explicit `equal`, like every other draft
  // editor on this surface: a `linkedSignal` source *function* re-seeds
  // whenever its dependencies change even if its value has not, and a refusal
  // banner's "Reload configuration" reloads this same `state` resource —
  // which would silently discard a typed-but-unapplied edit.
  private readonly storedLimits = computed(
    () => ({
      loss_fraction: this.state.hasValue() ? this.state.value().loss_fraction : null,
      loss_usd: this.state.hasValue() ? this.state.value().loss_usd : null,
    }),
    { equal: (a, b) => a.loss_fraction === b.loss_fraction && a.loss_usd === b.loss_usd },
  );
  protected readonly draft = linkedSignal({
    source: this.storedLimits,
    computation: (limits) => limits,
  });
  protected readonly fields = form(this.draft);
  protected readonly busy = linkedSignal(() => { this.clerkId(); return false; });
  protected readonly refusal = linkedSignal<string, ConfigurationRefusal | null>({ source: this.clerkId, computation: () => null });
  /** The hold status this section shows, per lane. */
  private readonly shownEntryState = computed(
    () => ({ clerkId: this.clerkId(), entryState: this.state.hasValue() ? this.state.value().entry_state : null }),
    { equal: (a, b) => a.clerkId === b.clerkId && a.entryState === b.entryState },
  );
  /** What the last action changed. A later read that finds another hold status
   * retires it, so "Applied…" never sits beside a hold raised since; a re-read
   * that confirms the same status (the verdict catching up with Clear hold)
   * keeps it. */
  protected readonly outcome = linkedSignal({ source: this.shownEntryState, computation: (): Outcome | null => null });
  protected readonly valid = computed(() => {
    const draft = this.draft();
    return draft.loss_fraction !== null && draft.loss_fraction > 0 && draft.loss_fraction < 1
      && draft.loss_usd !== null && draft.loss_usd > 0;
  });

  /** This lane's loss-hold state from the shell's live verdict, or `null`
   * while it is unread — not read yet, or its last read failed. */
  private readonly lossHold = computed(
    () => this.liveVerdicts.stateFor(this.clerkId()).verdict?.loss_hold ?? null,
  );
  /** A loss-hold change this section's read has not caught up with yet. */
  private readonly holdChanged = linkedSignal({ source: this.clerkId, computation: () => false });

  constructor() {
    let seen: { readonly clerkId: string; readonly hold: LossHold } | null = null;
    effect(() => {
      const clerkId = this.clerkId();
      const hold = this.lossHold();
      // An unread verdict says nothing about the hold, so the last known state
      // stands: clear, then a failed read, then held is still a change.
      if (hold === null) return;
      const previous = seen;
      seen = { clerkId, hold };
      // Only a change on the same lane is news: a new lane re-reads through
      // `state` itself, and the verdict's first arrival says nothing this
      // section's own read did not.
      if (previous === null || previous.clerkId !== clerkId || previous.hold === hold) return;
      untracked(() => this.holdChanged.set(true));
    });
    // Re-read once no action is in flight: a change that lands mid-action waits
    // for it instead of being dropped.
    effect(() => {
      if (!this.holdChanged() || this.busy()) return;
      untracked(() => {
        this.holdChanged.set(false);
        this.state.reload();
      });
    });
  }

  protected checkAgain(): void {
    this.outcome.set(null);
    this.state.reload();
  }

  protected applyLimit(): void {
    const { loss_fraction, loss_usd } = this.draft();
    if (loss_fraction === null || loss_usd === null || !this.valid()) return;
    void this.run('applied', (target, reviewed) =>
      this.service.applyRiskLimits(target, { ...reviewed, loss_fraction, loss_usd }));
  }

  protected clearHold(): void {
    void this.run('cleared', (target, reviewed) => this.service.clearRiskHold(target, reviewed));
  }

  /** One reviewed write at a time, fenced on the revision and selection
   * generation this section read. Never retried: a stale fence is a refusal. */
  private async run(
    outcome: Outcome,
    write: (target: ResourceTarget, reviewed: ReviewedRisk) => Promise<AccountRiskState>,
  ): Promise<void> {
    const state = this.state.hasValue() ? this.state.value() : null;
    if (this.busy() || state === null) return;
    const clerkId = this.clerkId();
    const lane = this.directory.lane('alpaca', clerkId);
    const target = resourceTarget('alpaca', clerkId, {
      capability: 'configuration_manage', idempotencyKey: globalThis.crypto.randomUUID(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
    this.busy.set(true);
    this.refusal.set(null);
    this.outcome.set(null);
    try {
      const updated = await write(target, {
        expected_risk_revision: state.risk_revision,
        expected_selection_generation: state.selection_generation,
      });
      if (this.clerkId() !== clerkId) return;
      this.state.set(updated);
      this.outcome.set(outcome);
      this.focusAfterRender(this.outcomeRegion);
    } catch (error) {
      if (this.clerkId() !== clerkId) return;
      this.refusal.set(toConfigurationRefusal(error));
      this.focusAfterRender(this.refusalRegion);
    } finally {
      if (this.clerkId() === clerkId) this.busy.set(false);
    }
  }

  /** Move the keyboard to what the action produced once it has rendered. */
  private focusAfterRender(region: () => ElementRef<HTMLElement> | undefined): void {
    afterNextRender({ write: () => region()?.nativeElement.focus() }, { injector: this.injector });
  }
}

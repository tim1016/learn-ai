import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';

import type {
  BotLifecycleAction,
  BotLifecycleActionId,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';
import type { OperatorMove } from '../../../../api/operator-blocker.types';
import type { RenderedAction } from '../lib/suggested-action-renderer';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { ActivityTabComponent } from '../tabs/activity-tab.component';
import { WhyDrawerComponent } from './why-drawer.component';
import {
  CRASH_RECOVERY_VERB_LABEL,
  EVIDENCE_VERB_LABEL,
  resolveVerdictCardModel,
} from '../lib/verdict-card-model';
import { lifecycleConditionCureTarget } from '../../lib/condition-cure-actions';

/** The Verdict Card — the trader-first face of `/broker/bots/:id`
 *  (docs/superpowers/specs/2026-07-08-bot-control-verdict-card-design.md).
 *
 *  Pure presentation over the backend-authored status: it renders one state
 *  word, one reason line, one big verb, and (on duty) the price chart. Deep
 *  evidence is reached only through the scoped `why?` drawer or the `⋯` overflow.
 *  Verb dispatch is the container's job — this component only signals intent. */
@Component({
  selector: 'app-verdict-card',
  imports: [AssetIdentityComponent, ActivityTabComponent, WhyDrawerComponent],
  templateUrl: './verdict-card.component.html',
  styleUrl: './verdict-card.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class VerdictCardComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly busy = input<boolean>(false);
  /** The container-computed remediation verb (label + variant); rendered as the
   *  primary verb only when the model has no lifecycle primary action. */
  readonly renderedRemediation = input<RenderedAction | null>(null);

  readonly lifecycleAction = output<BotLifecycleActionId>();
  readonly remediationInvoked = output();
  readonly crashRecoveryRequested = output();
  readonly settingsRequested = output();
  readonly accountMonitorRequested = output();
  readonly removeRequested = output();
  readonly terminalRetireReplaceRequested = output();
  readonly blockerMoveRequested = output<OperatorMove>();

  readonly whyOpen = signal(false);
  readonly historyOpen = signal(false);
  readonly overflowOpen = signal(false);

  readonly model = computed(() => resolveVerdictCardModel(this.status()));
  readonly guidance = computed(() => this.status().operator_surface.trader_guidance);
  readonly gates = computed(() => this.status().operator_surface.readiness_gates);

  readonly lifecycleVerb = computed<BotLifecycleAction | null>(() => {
    const verb = this.model().verb;
    return verb.kind === 'lifecycle' ? verb.action : null;
  });

  readonly verbLabel = computed<string | null>(() => {
    const verb = this.model().verb;
    if (verb.kind === 'lifecycle') return verb.action.label;
    if (verb.kind === 'blocker_move') return verb.move.label;
    if (verb.kind === 'remediation') return this.renderedRemediation()?.label ?? null;
    if (verb.kind === 'crash_recovery') return CRASH_RECOVERY_VERB_LABEL;
    if (verb.kind === 'evidence') return EVIDENCE_VERB_LABEL;
    return null;
  });

  readonly verbEnabled = computed<boolean>(() => {
    if (this.busy()) return false;
    const verb = this.model().verb;
    if (verb.kind === 'lifecycle') return verb.action.enabled;
    if (verb.kind === 'blocker_move') return true;
    if (verb.kind === 'remediation') return this.renderedRemediation() !== null;
    if (verb.kind === 'crash_recovery') return true;
    if (verb.kind === 'evidence') return true;
    return false;
  });

  readonly verbDisabledReason = computed<string | null>(() => {
    const verb = this.model().verb;
    return verb.kind === 'lifecycle' ? verb.action.reason : null;
  });

  readonly sizingPreset = computed<string | null>(() => this.status().sizing?.preset ?? null);
  readonly orderCapLimit = computed<number | null>(
    () => this.status().operator_surface.daily_order_cap.limit,
  );
  readonly sickBayCondition = computed(() => {
    if (!this.model().showConditionCure) return null;
    const lifecycle = this.status().daily_lifecycle;
    return lifecycle.display_status === 'Sick bay'
      ? lifecycle.conditions?.[0] ?? null
      : null;
  });

  invokeVerb(): void {
    if (!this.verbEnabled()) return;
    const verb = this.model().verb;
    if (verb.kind === 'lifecycle') this.lifecycleAction.emit(verb.action.id);
    else if (verb.kind === 'blocker_move') this.blockerMoveRequested.emit(verb.move);
    else if (verb.kind === 'remediation') this.remediationInvoked.emit();
    else if (verb.kind === 'crash_recovery') this.crashRecoveryRequested.emit();
    else if (verb.kind === 'evidence') this.openWhy();
  }

  invokeTerminalMove(move: OperatorMove): void {
    if (this.busy()) return;
    if (move.action.kind === 'retire_replace') {
      this.terminalRetireReplaceRequested.emit();
    } else if (move.action.kind === 'remove') {
      this.removeRequested.emit();
    }
  }

  invokeAmbient(action: BotLifecycleAction): void {
    this.overflowOpen.set(false);
    if (!action.enabled || this.busy()) return;
    this.lifecycleAction.emit(action.id);
  }

  toggleOverflow(): void {
    this.overflowOpen.update((open) => !open);
  }

  openHistory(): void {
    this.overflowOpen.set(false);
    this.historyOpen.set(true);
  }

  closeHistory(): void {
    this.historyOpen.set(false);
  }

  requestSettings(): void {
    this.overflowOpen.set(false);
    this.settingsRequested.emit();
  }

  invokeConditionCure(): void {
    const condition = this.sickBayCondition();
    if (!condition) {
      this.accountMonitorRequested.emit();
      return;
    }
    if (lifecycleConditionCureTarget(condition) === 'retireReplace') {
      this.lifecycleAction.emit('retire_replace');
      return;
    }
    this.accountMonitorRequested.emit();
  }

  openWhy(): void {
    this.whyOpen.set(true);
  }

  closeWhy(): void {
    this.whyOpen.set(false);
  }
}

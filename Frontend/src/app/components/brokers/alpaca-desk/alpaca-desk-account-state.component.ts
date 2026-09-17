import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';

import type {
  AlpacaDeskSelectionSummary,
  AlpacaDeskState,
  BrokerAccountSnapshot,
} from '../../../api/alpaca.types';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneDisplayNameText } from '../../../fleet/fleet-directory.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { AlpacaAccountActivationComponent } from './alpaca-account-activation.component';
import { AlpacaAccountCardComponent } from './alpaca-account-card.component';

/** What the identity strip shows, and where each fact came from. */
interface EffectiveIdentity {
  readonly profileLabel: string;
  readonly revision: number | null;
  /** How this account is *named*, never its number (ADR 0064; #2188) —
   * the backend-composed friendly label when a choice is effective, and the
   * lane's own display name when none is. `null` only when no lane can be
   * named because the directory has not resolved this account; the strip
   * then omits the name rather than falling back to the number. */
  readonly accountLabel: string | null;
  /** Rendered through the shared receipt-label path; `paper`/`live` are code-like values. */
  readonly endpointMode: string;
  readonly source: 'effective_choice' | 'snapshot-fallback';
}

/**
 * Configuration/connection state above the operating desk. This keeps the
 * shell's trading controls independent from the four activation states while
 * preserving the account card's shared account read.
 *
 * ## Identity truth layers (task 2026-09-12, phase 3)
 *
 * The strip shows the *effective* Paper/Live identity, never one inferred from
 * staged state:
 *
 * - When `effective_choice` exists, profile, revision, account label, and
 *   endpoint mode come only from it.
 * - Only when it is absent may the generation-zero `BrokerAccountSnapshot`
 *   supply an *observed* account and mode — and never a revision, because
 *   the snapshot has none to give. That observed account is *named*, through
 *   its lane's display name, never printed as a number: the effective-choice
 *   branch's own `account_label` is a friendly backend-composed name, and the
 *   fallback must not silently degrade the same line into an identifier
 *   (ADR 0064; #2188).
 * - If an independently reported account id disagrees with the effective
 *   choice's, the strip says so and gates the identity-dependent review action
 *   rather than silently merging the two records.
 */
@Component({
  selector: 'app-alpaca-desk-account-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaAccountActivationComponent, AlpacaAccountCardComponent, ReceiptLabelPipe],
  templateUrl: './alpaca-desk-account-state.component.html',
  styleUrl: './alpaca-desk-account-state.component.scss',
})
export class AlpacaDeskAccountStateComponent {
  private readonly fleetDirectory = inject(FleetDirectoryService);

  readonly state = input<AlpacaDeskState | null>(null);
  readonly accountAvailable = input.required<boolean>();
  readonly accountFailed = input.required<boolean>();
  /** The generation-zero account read; used for identity only when no effective choice exists. */
  readonly snapshot = input<BrokerAccountSnapshot | null>(null);

  readonly reviewRequested = output<AlpacaDeskSelectionSummary | null>();

  protected readonly identity = computed<EffectiveIdentity | null>(() => {
    const choice = this.state()?.effective_choice ?? null;
    if (choice !== null) {
      return {
        profileLabel: choice.profile_label,
        revision: choice.revision,
        accountLabel: choice.account_label,
        endpointMode: choice.endpoint_mode,
        source: 'effective_choice',
      };
    }
    const observed = this.snapshot();
    if (observed === null || !this.accountAvailable()) return null;
    return {
      profileLabel: 'Unconfigured worker account',
      revision: null,
      accountLabel: this.laneName(observed.account_id),
      endpointMode: observed.account_mode,
      source: 'snapshot-fallback',
    };
  });

  /** The display name of the lane whose confirmed binding serves this
   * account, resolved through the directory's own canonical helpers so this
   * strip names an account exactly as the workspace header, the account
   * switcher, and the shell badge do — including the "(Label)" disambiguator
   * a name shared with a sibling lane carries (ADR 0064 Decision 5).
   *
   * `null` when the directory cannot name it. The number is not a fallback:
   * showing it here is the leak this replaced. */
  private laneName(accountId: string): string | null {
    const lane = this.fleetDirectory.laneForAccount('alpaca', accountId);
    if (lane === undefined) return null;
    const display = this.fleetDirectory.displayNameOf('alpaca', lane.clerk_id);
    return display === null ? null : laneDisplayNameText(display);
  }

  /**
   * The effective choice and the live account read name different accounts.
   * That is never merged away: the strip warns, and the review action that
   * speaks for the effective identity is gated until the operator resolves it.
   */
  protected readonly accountIdMismatch = computed<string | null>(() => {
    const choice = this.state()?.effective_choice ?? null;
    const observed = this.snapshot();
    if (choice === null || choice.account_id === null) return null;
    if (!this.accountAvailable() || observed === null) return null;
    return choice.account_id !== observed.account_id
      ? `The effective configuration names account ${choice.account_id}, but Alpaca reports ${observed.account_id}. Resolve the mismatch before acting on this identity.`
      : null;
  });

  protected readonly showActivation = computed(
    () => this.accountFailed() && this.state()?.effective_choice === null,
  );
  protected readonly showConnectivityContext = computed(() => {
    const state = this.state();
    return this.accountFailed() && state !== null && state.effective_choice !== null;
  });
  protected readonly showSelectionChange = computed(() => {
    const state = this.state();
    return state !== null
      && (this.accountAvailable()
        || (this.accountFailed() && state.effective_choice !== null))
      && state.staged_choice !== null
      && state.activation_state !== 'effective_selection';
  });

  protected actionTarget(state: AlpacaDeskState): AlpacaDeskSelectionSummary | null {
    return state.action.kind === 'review_configuration'
      ? state.effective_choice
      : state.staged_choice;
  }

  /** Identity-dependent actions stay available only while the identity is unambiguous. */
  protected actionEnabled(state: AlpacaDeskState): boolean {
    return state.action.enabled && this.accountIdMismatch() === null;
  }

  protected requestReview(
    state: AlpacaDeskState,
    target: AlpacaDeskSelectionSummary | null,
  ): void {
    if (!this.actionEnabled(state)) return;
    this.reviewRequested.emit(target);
  }
}

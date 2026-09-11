import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  BrokerEndpointMode,
  BrokerInstallationSelection,
  BrokerProfile,
} from '../../../../api/alpaca.types';
import { ReceiptLabelPipe, formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/** How far apart staged and effective are, and therefore what is owed next. */
export type SelectionDrift = 'nothing-staged' | 'apply-pending' | 'staged-not-applied' | 'in-step';

/**
 * What each drift owes the operator. This is copy about *this page's* own
 * mechanics — the controlled restart the plan asks E to surface — not a
 * rendering of any backend reason code, which arrives with its own prose.
 */
const DRIFT_MESSAGE: Readonly<Record<SelectionDrift, string>> = {
  'nothing-staged': 'Stage a profile revision to apply one.',
  'apply-pending':
    'Apply is recorded and nothing has changed yet. The staged revision becomes effective '
    + 'at the next controlled restart of the worker, which an operator performs on the host.',
  'staged-not-applied':
    'The staged revision is not the effective one. Apply records the intent; a controlled '
    + 'restart is what makes it effective.',
  'in-step': 'The staged revision is the one already effective.',
};

/**
 * What one side's endpoint reads as. A mode the page has not read yet says so
 * rather than falling back to `paper`: on this surface, guessing the quieter of
 * the two is the one guess that could mislead an operator about real money.
 */
function modeLabel(mode: BrokerEndpointMode | null): string {
  return mode === null ? 'endpoint unread' : formatReceiptLabel(mode);
}

/**
 * Staged and effective, side by side, and the Apply button between them.
 *
 * The three states ADR 0060 Decision 4 separates are separated here too:
 * **staged** governs nothing, **effective** is what the running worker
 * resolved at its last boot, and **sealed** — the envelope a live arming
 * record binds — is not on this surface at all, because nothing here can
 * change it.
 *
 * Pressing Apply records a one-shot request and changes no runtime. The panel
 * says so rather than implying the change is live, and it never implies the
 * browser can arm a live limit: that is a host CLI ceremony.
 */
@Component({
  selector: 'app-configuration-status-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './configuration-status-panel.component.html',
  styleUrl: './configuration-status-panel.component.scss',
  host: { class: 'block' },
})
export class ConfigurationStatusPanelComponent {
  readonly selection = input.required<BrokerInstallationSelection>();
  readonly profiles = input.required<readonly BrokerProfile[]>();
  /** The label for the account the worker bound, resolved by the page that owns the nicknames. */
  readonly effectiveNickname = input<string | null>(null);
  /**
   * Which endpoint each side talks to. `null` means the page has not read that
   * revision yet — rendered as unread rather than as paper, because guessing
   * the quieter of the two is the one guess this surface must never make.
   */
  readonly stagedEndpointMode = input<BrokerEndpointMode | null>(null);
  readonly effectiveEndpointMode = input<BrokerEndpointMode | null>(null);
  readonly busy = input(false);

  readonly applyRequested = output();

  protected readonly stagedLabel = computed(() =>
    this.describe(this.selection().staged_profile_id, this.selection().staged_revision),
  );

  protected readonly effectiveLabel = computed(() =>
    this.describe(this.selection().effective_profile_id, this.selection().effective_revision),
  );

  protected readonly drift = computed<SelectionDrift>(() => {
    const current = this.selection();
    if (current.apply_requested) return 'apply-pending';
    if (current.staged_profile_id === null || current.staged_revision === null) {
      return 'nothing-staged';
    }
    const sameAsEffective =
      current.staged_profile_id === current.effective_profile_id
      && current.staged_revision === current.effective_revision;
    return sameAsEffective ? 'in-step' : 'staged-not-applied';
  });

  protected readonly effectiveModeLabel = computed(() => modeLabel(this.effectiveEndpointMode()));
  protected readonly stagedModeLabel = computed(() => modeLabel(this.stagedEndpointMode()));

  protected readonly driftMessage = computed(() => DRIFT_MESSAGE[this.drift()]);

  /** Apply needs something staged, and never runs twice against one request. */
  protected readonly canApply = computed(
    () => !this.busy() && this.drift() !== 'nothing-staged' && this.drift() !== 'apply-pending',
  );

  private describe(profileId: string | null, revision: number | null): string | null {
    if (profileId === null || revision === null) return null;
    const profile = this.profiles().find((candidate) => candidate.profile_id === profileId);
    // A profile id is an opaque server-generated token: when no profile row
    // explains it (archived away, or a list this page has not loaded), it
    // reaches the operator byte-for-byte rather than as a rewritten label.
    return `${profile?.display_name ?? profileId} · revision ${revision}`;
  }
}

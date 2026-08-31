import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';

import { Drawer } from 'primeng/drawer';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { ARCHIVE_CONFIRM_TOKEN } from './archive-confirm-token';
import { CohortArchiveCommitComponent } from './cohort-archive-commit.component';
import { CohortArchiveGroupComponent } from './cohort-archive-group.component';
import { CohortArchiveOutcomeComponent } from './cohort-archive-outcome.component';
import type {
  CohortActionResult,
  CohortArchiveLeg,
  CohortArchiveView,
} from '../lib/broker-v2-panel.types';

/** An armed leg, proven to carry the identity its POST is checked against. */
type ArmedArchiveLeg = CohortArchiveLeg & {
  revision: number;
  concurrency_token: string;
};

function isArmed(leg: CohortArchiveLeg): leg is ArmedArchiveLeg {
  return leg.enabled && leg.revision !== null && leg.concurrency_token !== null;
}

/**
 * Clear finished bots off the roster, N behind one affordance (ADR 0052).
 *
 * Presentation is backend-authored: the drawer renders the legs it is sent
 * and never decides which bots are archivable. A refused leg is shown with
 * its reason rather than hidden — a surface whose job is "show me what I can
 * clear" must not quietly under-report the roster.
 *
 * Selection is explicit and travels to the server as explicit membership
 * (ADR 0051 Decision 2): exactly the checked legs are sent, each with the
 * token and revision it was presented with, so the batch executes what the
 * operator actually saw.
 */
@Component({
  selector: 'app-cohort-archive-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Drawer,
    CohortArchiveCommitComponent,
    CohortArchiveGroupComponent,
    CohortArchiveOutcomeComponent,
  ],
  templateUrl: './cohort-archive-drawer.component.html',
  styleUrl: './cohort-archive-drawer.component.scss',
})
export class CohortArchiveDrawerComponent {
  readonly visible = input.required<boolean>();
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();

  readonly closed = output();
  /** Emitted after any leg applied, so the roster re-reads its catalog. */
  readonly archived = output();

  private readonly panelService = inject(BrokerV2PanelService);

  protected readonly submitting = signal(false);
  protected readonly outcome = signal<CohortActionResult | null>(null);
  /** A POST that never returned a typed batch result — network, auth, 5xx. */
  protected readonly submitError = signal(false);
  protected readonly selected = signal<ReadonlySet<string>>(new Set());
  protected readonly confirmText = signal('');

  /**
   * Clear everything that could act on a bot.
   *
   * Called on close and whenever the account scope changes: Angular reuses
   * this component across route params, and a selection or a typed
   * confirmation carried into a different account could arm a click the
   * operator never made against a bot they never saw.
   */
  private clearDestructiveState(): void {
    this.selected.set(new Set());
    this.confirmText.set('');
    this.outcome.set(null);
    this.submitError.set(false);
  }

  /** Read while the drawer is open; idle while it is closed. */
  private readonly archivable = resource({
    params: () =>
      this.visible() ? { broker: this.broker(), accountId: this.accountId() } : undefined,
    loader: ({ params }) =>
      this.panelService.getCohortArchiveView(params.broker, params.accountId),
  });

  protected readonly view = computed<CohortArchiveView | null>(() => {
    const value = this.archivable.value();
    // Never hand back another account's legs: the resource keeps its previous
    // value across a params change, and these legs carry act-on-me tokens.
    return value?.account_id === this.accountId() ? value : null;
  });
  protected readonly loading = computed(() => this.archivable.isLoading());
  protected readonly loadFailed = computed(() => this.archivable.error() !== undefined);

  /**
   * The legs the backend armed, narrowed to those that actually carry the
   * token and revision a POST needs.
   *
   * An armed leg always carries both — the backend arms one only with the
   * facts it presents — so the guard never drops a real leg. Narrowing here
   * rather than asserting later means the submit path has no way to send a
   * leg without the identity the server will check it against.
   */
  protected readonly armedLegs = computed<readonly ArmedArchiveLeg[]>(() =>
    (this.view()?.cohorts ?? [])
      .flatMap((cohort) => cohort.legs)
      .filter(isArmed),
  );

  /**
   * The selected legs that are still present and still armed.
   *
   * Derived rather than reset, so a selection cannot outlive what it referred
   * to: switching accounts, or a reload that disarms a leg, drops it from the
   * count and from the submission without anything having to notice the
   * change and clean up after it.
   */
  protected readonly selectedLegs = computed<readonly ArmedArchiveLeg[]>(() => {
    const chosen = this.selected();
    return this.armedLegs().filter((leg) => chosen.has(leg.strategy_instance_id));
  });

  protected readonly selectedCount = computed(() => this.selectedLegs().length);

  protected readonly confirmed = computed(
    () => this.confirmText().trim().toUpperCase() === ARCHIVE_CONFIRM_TOKEN,
  );

  protected readonly canSubmit = computed(
    () => this.selectedCount() > 0 && this.confirmed() && !this.submitting(),
  );

  /**
   * Shown only when the account has no candidates at all.
   *
   * Deliberately not keyed on "nothing is armed": an account whose stopped
   * bots all hold exposure has candidates, each with a backend-authored
   * reason the operator needs. Replacing that list with a summary would hide
   * exactly the identities and remediation the service includes them for.
   */
  protected readonly emptyMessage = computed(() => {
    const cohorts = this.view()?.cohorts;
    if (this.loading() || this.loadFailed() || cohorts === undefined) return null;
    return cohorts.length === 0
      ? 'No bot on this account can be archived right now. A bot must be stopped and provably flat.'
      : null;
  });

  protected isSelected(leg: CohortArchiveLeg): boolean {
    return this.selected().has(leg.strategy_instance_id);
  }

  protected toggle(leg: CohortArchiveLeg): void {
    if (!leg.enabled) return;
    this.selected.update((current) => {
      const next = new Set(current);
      if (!next.delete(leg.strategy_instance_id)) {
        next.add(leg.strategy_instance_id);
      }
      return next;
    });
  }

  protected selectAll(): void {
    this.selected.set(
      new Set(this.armedLegs().map((leg) => leg.strategy_instance_id)),
    );
  }

  protected clearSelection(): void {
    this.selected.set(new Set());
  }

  protected onClose(): void {
    // Clearing here rather than from a visibility effect: every route out of
    // the drawer — the close button and the backdrop dismiss — comes through
    // this method, so the next open starts clean without watching for it.
    this.clearDestructiveState();
    this.closed.emit();
  }

  protected async submit(): Promise<void> {
    if (!this.canSubmit()) return;
    const legs = this.selectedLegs().map((leg) => ({
      strategy_instance_id: leg.strategy_instance_id,
      revision: leg.revision,
      concurrency_token: leg.concurrency_token,
    }));

    this.submitting.set(true);
    this.submitError.set(false);
    try {
      const result = await this.panelService.runCohortArchive(
        this.broker(),
        this.accountId(),
        {
          idempotency_key: crypto.randomUUID(),
          reason: 'Cohort archive from the bots roster',
          legs,
        },
      );
      this.outcome.set(result);
      this.selected.set(new Set());
      this.confirmText.set('');
      if (result.applied_count + result.replayed_count > 0) this.archived.emit();
      // Re-read so a leg the batch refused shows its current reason rather
      // than the one it carried before the batch ran. `reload` rather than a
      // params change: the latter drops the prior value and blanks the list
      // under the outcome the operator is still reading.
      this.archivable.reload();
    } catch {
      // The POST never returned a typed batch result, so no leg outcome is
      // known. An irreversible command must not leave the button quietly
      // re-enabling: say the outcome is unknown, and keep the selection so a
      // retry is a deliberate act rather than a re-selection from scratch.
      this.submitError.set(true);
      this.archivable.reload();
    } finally {
      this.submitting.set(false);
    }
  }
}

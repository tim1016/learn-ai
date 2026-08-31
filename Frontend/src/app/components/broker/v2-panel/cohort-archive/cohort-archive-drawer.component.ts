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
import type {
  CohortActionResult,
  CohortArchiveLeg,
  CohortArchiveView,
} from '../lib/broker-v2-panel.types';

/**
 * The token an operator types to confirm a batch archive.
 *
 * The same word the backend requires on the single-bot action's typed
 * confirmation (`required_token: "ARCHIVE"`). Archiving N registrations is
 * strictly more consequential than archiving one, so the batch asks for the
 * same deliberate act once rather than waiving it because the surface is
 * bulk.
 */
export const ARCHIVE_CONFIRM_TOKEN = 'ARCHIVE';

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
  imports: [Drawer],
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
  protected readonly selected = signal<ReadonlySet<string>>(new Set());
  protected readonly confirmText = signal('');

  /** Read while the drawer is open; idle while it is closed. */
  private readonly archivable = resource({
    params: () =>
      this.visible() ? { broker: this.broker(), accountId: this.accountId() } : undefined,
    loader: ({ params }) =>
      this.panelService.getCohortArchiveView(params.broker, params.accountId),
  });

  protected readonly view = computed<CohortArchiveView | null>(
    () => this.archivable.value() ?? null,
  );
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

  protected readonly selectedCount = computed(() => this.selected().size);

  protected readonly confirmed = computed(
    () => this.confirmText().trim().toUpperCase() === ARCHIVE_CONFIRM_TOKEN,
  );

  protected readonly canSubmit = computed(
    () => this.selectedCount() > 0 && this.confirmed() && !this.submitting(),
  );

  protected readonly emptyMessage = computed(() => {
    if (this.loading() || this.loadFailed() || this.view() === null) return null;
    return this.armedLegs().length === 0
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
    this.selected.set(new Set());
    this.confirmText.set('');
    this.outcome.set(null);
    this.closed.emit();
  }

  protected async submit(): Promise<void> {
    if (!this.canSubmit()) return;
    const chosen = this.selected();
    const legs = this.armedLegs()
      .filter((leg) => chosen.has(leg.strategy_instance_id))
      .map((leg) => ({
        strategy_instance_id: leg.strategy_instance_id,
        revision: leg.revision,
        concurrency_token: leg.concurrency_token,
      }));

    this.submitting.set(true);
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
    } finally {
      this.submitting.set(false);
    }
  }
}

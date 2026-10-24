import { LowerCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { QUALIFICATION_LIST_LIMIT, GoldenSearchService } from '../../components/golden-search/golden-search.service';
import type { GoldenQualificationSummary } from '../../components/golden-search/golden-search.types';
import { extractServerMessage } from '../../components/broker/operation-error';
import { GOLDEN_DEPLOY_HANDOFF_ROUTE } from '../../fleet/account-workspace';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';
import { QUALIFICATION_TONES, parameterPairs } from '../signal-strategy-evidence';
import { SignalStrategyChipComponent } from '../signal-strategy-chip.component';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { GOLDEN_CASE_PARAM, deployHandoffQuery, goldenStudyRoute, goldenTabRoute } from './golden-tabs';

/** Where one version's re-proof stands, kept beside that version and no other. */
type Reproof =
  | { readonly kind: 'running' }
  | { readonly kind: 'failed'; readonly message: string };

/**
 * Golden → Versions: the immutable qualified versions Golden Search approved, one
 * symbol at a time (ADR 0074). A version covers one symbol and one exact
 * configuration, and carries its own status: only a ready version is current and
 * deployable, a stale one needs its evidence re-proved, a revoked one is closed
 * and an unreadable one is unknown. The active default decides only what Deploy
 * offers first; it revokes nothing. Nothing here sums a symbol's versions into a
 * strategy verdict, and approving is a study's decision, never a button on a row.
 *
 * Re-proof replays a stale version's retained evidence against the current
 * program and publishes nothing new: the recorded results must match, and the
 * server says when they do not. "Use in Deploy" carries the strategy and the exact
 * version to the account list, where the owner chooses the account.
 */
@Component({
  selector: 'app-golden-versions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    LowerCasePipe,
    ReceiptLabelPipe,
    RouterLink,
    SignalStrategyChipComponent,
    SignalStrategySectionComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './golden-versions.component.html',
  styleUrls: ['../section-content.scss', './golden-versions.component.scss'],
})
export class GoldenVersionsComponent {
  protected readonly store = inject(SignalStrategyWorkspaceStore);
  private readonly service = inject(GoldenSearchService);

  protected readonly tones = QUALIFICATION_TONES;
  protected readonly listLimit = QUALIFICATION_LIST_LIMIT;
  protected readonly deployRoute = GOLDEN_DEPLOY_HANDOFF_ROUTE;
  protected readonly reproofs = signal<ReadonlyMap<string, Reproof>>(new Map());

  /** Why no version can exist, when the registry rules Golden Search out. */
  protected readonly goldenRefusal = computed(() => {
    const state = this.store.entry();
    return state.kind === 'found' && !state.entry.capabilities.golden.supported ? state.entry.capabilities.golden.reason : null;
  });

  protected readonly configurationRoute = computed(() => goldenTabRoute(this.store.key(), 'configuration'));

  constructor() {
    this.store.want('approvals');
  }

  protected studyRoute(version: GoldenQualificationSummary): readonly string[] {
    return goldenStudyRoute(this.store.key(), version.study_id);
  }

  protected deployQuery(version: GoldenQualificationSummary): Record<string, string> {
    return deployHandoffQuery(version.program_key, version.id);
  }

  /** The selected baseline run's review the version was approved with. */
  protected caseQuery(version: GoldenQualificationSummary): Record<string, string> {
    return { [GOLDEN_CASE_PARAM]: String(version.golden_run_id) };
  }

  protected parameters(version: GoldenQualificationSummary): string {
    return parameterPairs(version.parameters)?.join(' · ') ?? 'no settings recorded';
  }

  /** The final test's exposure the version was approved under, in the study's own words, or `null` when the record has none. */
  protected exposure(version: GoldenQualificationSummary): string | null {
    const research = version.research;
    const parts = [research.exposure_state, research.claim].filter((part): part is string => typeof part === 'string' && part !== '');
    return parts.length === 0 ? null : parts.join(' · ');
  }

  /** Replays a stale version's retained evidence. The version's own status is read again from the server afterwards. */
  protected async reprove(version: GoldenQualificationSummary): Promise<void> {
    if (this.reproofs().get(version.id)?.kind === 'running') return;
    this.mark(version.id, { kind: 'running' });
    try {
      await this.service.reproveQualification(version.id, crypto.randomUUID());
      this.mark(version.id, null);
      this.store.retry('approvals');
    } catch (error) {
      this.mark(version.id, { kind: 'failed', message: extractServerMessage(error, 'The version could not be re-proved. Its status is unchanged.') });
    }
  }

  private mark(id: string, state: Reproof | null): void {
    this.reproofs.update((current) => {
      const next = new Map(current);
      if (state === null) next.delete(id);
      else next.set(id, state);
      return next;
    });
  }
}

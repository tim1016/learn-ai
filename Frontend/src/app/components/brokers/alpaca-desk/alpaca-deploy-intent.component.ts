import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import { isQualificationId } from '../../../fleet/account-workspace';
import { PINNED_VERSION_RECHECK_ACTION, pinnedVersionWatch } from '../../../fleet/pinned-version-watch';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import type { WorkspaceAction } from '../../../shared/workspace/workspace-action';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { goldenTabRoute } from '../../../signal-strategies/golden/golden-tabs';
import { SIGNAL_STRATEGIES_ROUTE } from '../../../signal-strategies/signal-strategy-tabs';
import { versionSettingsText } from '../../../signal-strategies/signal-strategy-evidence';
import { extractServerMessage } from '../../broker/operation-error';
import { GoldenSearchService } from '../../golden-search/golden-search.service';
import type { QualificationDeployOffer } from '../../golden-search/golden-search.types';

type IntentState =
  | { readonly kind: 'reading' }
  | { readonly kind: 'ready'; readonly offer: QualificationDeployOffer }
  | { readonly kind: 'refused'; readonly content: WorkspaceStateContent };

/**
 * The version the account list is carrying to Deploy (`?golden_qualification=`, PRD
 * #3490 R19, I02), shown exactly as it was chosen while the owner chooses an account.
 *
 * It reads the version's own deploy offer, which is what Deploy will read, and says
 * what it finds: the strategy, symbol and exact settings when it is ready, and a
 * refusal that names it when it is stale, revoked, unreadable or answered as another
 * version. `pinnedVersionWatch` keeps the version (re-reads it while the owner decides,
 * holds a refusal until the owner asks again, never substitutes another); this panel
 * only words what it finds. The cards below carry the version unchanged either way,
 * so Deploy, which reads it again at its own boundary, refuses it by the same facts.
 *
 * It only reads. Nothing here sends a command.
 */
@Component({
  selector: 'app-alpaca-deploy-intent',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, WorkspaceStateBlockComponent],
  templateUrl: './alpaca-deploy-intent.component.html',
  styleUrl: './alpaca-deploy-intent.component.scss',
})
export class AlpacaDeployIntentComponent {
  /** The `golden_qualification` value as the address carries it, valid or not. */
  readonly qualificationId = input.required<string>();
  /** The `strategy` the address names, for the way back when the read says none. */
  readonly strategyKey = input<string | null>(null);

  private readonly golden = inject(GoldenSearchService);

  protected readonly watch = pinnedVersionWatch<QualificationDeployOffer>({
    id: this.qualificationId,
    isValidId: isQualificationId,
    read: (id) => this.golden.deployOffer(id),
    answeredId: (offer) => offer.qualification_id,
    isReady: (offer) => offer.status === 'ready',
  });

  protected readonly state = computed<IntentState>(() => {
    const id = this.qualificationId();
    const phase = this.watch.phase();
    switch (phase.kind) {
      case 'reading':
        return { kind: 'reading' };
      case 'invalid':
        return {
          kind: 'refused',
          content: {
            kind: 'invalid',
            happened: 'This link does not name a golden configuration.',
            next: 'Nothing was applied and no account was chosen. Choose a version in Golden to continue.',
            actions: [this.versionsAction(this.strategyKey())],
          },
        };
      case 'failed':
        return {
          kind: 'refused',
          content: {
            kind: 'failed',
            happened: `Golden configuration ${id} could not be read.`,
            why: extractServerMessage(phase.failure, 'The server did not answer.'),
            next: 'Nothing was substituted and no account was chosen. Check again, or choose a version in Golden.',
            actions: [PINNED_VERSION_RECHECK_ACTION, this.versionsAction(this.strategyKey())],
          },
        };
      case 'substituted':
        return {
          kind: 'refused',
          content: {
            kind: 'unavailable',
            happened: `The answer for golden configuration ${id} named a different one, ${phase.answeredId}.`,
            why: 'An answer about another version is never used in its place.',
            next: 'Nothing was applied and no account was chosen. Check again, or choose a version in Golden.',
            actions: [PINNED_VERSION_RECHECK_ACTION, this.versionsAction(phase.version.program_key)],
          },
        };
      case 'unavailable':
        return {
          kind: 'refused',
          content: {
            kind: 'unavailable',
            happened: `Golden configuration ${id} for ${phase.version.symbol} is ${phase.version.status} and cannot be deployed.`,
            why: phase.version.explanation,
            next: 'No other version was chosen in its place and no account was chosen. Choose a version in Golden to continue.',
            actions: [this.versionsAction(phase.version.program_key), PINNED_VERSION_RECHECK_ACTION],
          },
        };
      case 'ready':
        return { kind: 'ready', offer: phase.version };
    }
  });

  protected readonly settings = computed(() => {
    const state = this.state();
    return state.kind === 'ready' ? versionSettingsText(state.offer.parameters) : null;
  });

  /** Where a version is chosen again: the strategy's Golden Versions, or the list of strategies when none is known. */
  private versionsAction(strategyKey: string | null): WorkspaceAction {
    return strategyKey === null || strategyKey === ''
      ? { id: 'versions', label: 'Choose a strategy', route: [SIGNAL_STRATEGIES_ROUTE] }
      : { id: 'versions', label: 'Choose a version in Golden', route: goldenTabRoute(strategyKey, 'versions') };
  }
}

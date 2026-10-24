import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, input, resource } from '@angular/core';
import { DOCUMENT } from '@angular/common';

import { isQualificationId } from '../../../fleet/account-workspace';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import type { WorkspaceAction } from '../../../shared/workspace/workspace-action';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { goldenTabRoute } from '../../../signal-strategies/golden/golden-tabs';
import { SIGNAL_STRATEGIES_ROUTE } from '../../../signal-strategies/signal-strategy-tabs';
import { parameterPairs } from '../../../signal-strategies/signal-strategy-evidence';
import { extractServerMessage } from '../../broker/operation-error';
import { GoldenSearchService } from '../../golden-search/golden-search.service';
import type { QualificationDeployOffer } from '../../golden-search/golden-search.types';

/** How often a version still waiting for an account is looked at again while the page is open. */
const RECHECK_MS = 15_000;

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
 * version. A refusal is kept until the owner asks again: the page never flips back
 * on its own, never offers another version, and never picks an account. The cards
 * below carry the version unchanged either way, so Deploy, which reads it again at
 * its own boundary, refuses it by the same facts.
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
  private readonly document = inject(DOCUMENT);

  private readonly offer = resource({
    params: () => {
      const id = this.qualificationId();
      return isQualificationId(id) ? id : undefined;
    },
    loader: ({ params }) => this.golden.deployOffer(params),
  });

  private readonly recheckAction: WorkspaceAction = { id: 'recheck', label: 'Check again' };

  protected readonly state = computed<IntentState>(() => {
    const id = this.qualificationId();
    if (!isQualificationId(id)) {
      return {
        kind: 'refused',
        content: {
          kind: 'invalid',
          happened: 'This link does not name a golden configuration.',
          next: 'Nothing was applied and no account was chosen. Choose a version in Golden to continue.',
          actions: [this.versionsAction(this.strategyKey())],
        },
      };
    }
    const failure: unknown = this.offer.error();
    if (failure !== undefined) {
      return {
        kind: 'refused',
        content: {
          kind: 'failed',
          happened: `Golden configuration ${id} could not be read.`,
          why: extractServerMessage(failure, 'The server did not answer.'),
          next: 'Nothing was substituted and no account was chosen. Check again, or choose a version in Golden.',
          actions: [this.recheckAction, this.versionsAction(this.strategyKey())],
        },
      };
    }
    if (!this.offer.hasValue()) return { kind: 'reading' };
    const offer = this.offer.value();
    if (offer.qualification_id !== id) {
      return {
        kind: 'refused',
        content: {
          kind: 'unavailable',
          happened: `The answer for golden configuration ${id} named a different one, ${offer.qualification_id}.`,
          why: 'An answer about another version is never used in its place.',
          next: 'Nothing was applied and no account was chosen. Check again, or choose a version in Golden.',
          actions: [this.recheckAction, this.versionsAction(offer.program_key)],
        },
      };
    }
    if (offer.status !== 'ready') {
      return {
        kind: 'refused',
        content: {
          kind: 'unavailable',
          happened: `Golden configuration ${id} for ${offer.symbol} is ${offer.status} and cannot be deployed.`,
          why: offer.explanation,
          next: 'No other version was chosen in its place and no account was chosen. Choose a version in Golden to continue.',
          actions: [this.versionsAction(offer.program_key), this.recheckAction],
        },
      };
    }
    return { kind: 'ready', offer };
  });

  protected readonly settings = computed(() => {
    const state = this.state();
    return state.kind === 'ready' ? (parameterPairs(state.offer.parameters)?.join(' · ') ?? 'no settings recorded') : null;
  });

  constructor() {
    // A version waiting for an account can be revoked, or its program can change,
    // while the owner decides. Paused while the tab is hidden, and while a refusal
    // stands: a refusal is lifted only by asking again.
    const timer = setInterval(() => {
      if (this.document.visibilityState !== 'visible') return;
      if (this.state().kind === 'refused' || this.offer.isLoading()) return;
      this.offer.reload();
    }, RECHECK_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  protected onAction(actionId: string): void {
    if (actionId === 'recheck' && !this.offer.isLoading()) this.offer.reload();
  }

  /** Where a version is chosen again: the strategy's Golden Versions, or the list of strategies when none is known. */
  private versionsAction(strategyKey: string | null): WorkspaceAction {
    return strategyKey === null || strategyKey === ''
      ? { id: 'versions', label: 'Choose a strategy', route: [SIGNAL_STRATEGIES_ROUTE] }
      : { id: 'versions', label: 'Choose a version in Golden', route: goldenTabRoute(strategyKey, 'versions') };
  }
}

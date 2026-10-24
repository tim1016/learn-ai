import { DOCUMENT } from '@angular/common';
import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, input, resource } from '@angular/core';

import { isOptionsSetupId } from '../../../fleet/options-setup-handoff';
import { OPTIONS_SETUPS_ROOT, optionsEvidenceRoute } from '../../../options-setups/options-setups.address';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import type { WorkspaceAction } from '../../../shared/workspace/workspace-action';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { extractServerMessage } from '../../broker/operation-error';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../options-backtest/golden-option-setups.service';

/** How often a version still waiting for an account is looked at again while the page is open. */
const RECHECK_MS = 15_000;

type IntentState =
  | { readonly kind: 'reading' }
  | { readonly kind: 'ready'; readonly version: GoldenOptionSetup }
  | { readonly kind: 'refused'; readonly content: WorkspaceStateContent };

/**
 * The approved options version the account list is carrying to Deploy
 * (`?options_setup=`, PRD #3490 R23, I02), shown exactly as it was chosen while the
 * owner chooses an account. It is the options twin of the signal side's panel for
 * `?golden_qualification=`, and follows the same rules.
 *
 * It reads the version by its own id, which is what options Deploy reads, and says
 * what it finds: the name, symbol and source run when it is ready, and a refusal that
 * names it when it is stale, revoked, unreadable or answered as another version. A
 * refusal is kept until the owner asks again: the page never flips back on its own,
 * never offers another version, and never picks an account. The cards below carry the
 * version unchanged either way, so Deploy, which reads it again at its own boundary
 * and applies its own eligibility and submission checks, refuses it by the same facts.
 *
 * It only reads. Nothing here sends a command.
 */
@Component({
  selector: 'app-alpaca-options-deploy-intent',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, WorkspaceStateBlockComponent],
  templateUrl: './alpaca-options-deploy-intent.component.html',
  styleUrl: './alpaca-options-deploy-intent.component.scss',
})
export class AlpacaOptionsDeployIntentComponent {
  /** The `options_setup` value as the address carries it, valid or not. */
  readonly optionsSetupId = input.required<string>();

  private readonly golden = inject(GoldenOptionSetupsService);
  private readonly document = inject(DOCUMENT);

  private readonly version = resource({
    params: () => {
      const id = this.optionsSetupId();
      return isOptionsSetupId(id) ? id : undefined;
    },
    loader: ({ params }) => this.golden.get(params),
  });

  private readonly recheckAction: WorkspaceAction = { id: 'recheck', label: 'Check again' };
  private readonly listAction: WorkspaceAction = { id: 'versions', label: 'Choose a version in Options setups', route: [OPTIONS_SETUPS_ROOT] };

  protected readonly state = computed<IntentState>(() => {
    const id = this.optionsSetupId();
    if (!isOptionsSetupId(id)) {
      return {
        kind: 'refused',
        content: {
          kind: 'invalid',
          happened: 'This link does not name an approved options setup.',
          next: 'Nothing was applied and no account was chosen. Choose a version in Options setups to continue.',
          actions: [this.listAction],
        },
      };
    }
    const failure: unknown = this.version.error();
    if (failure !== undefined) {
      return {
        kind: 'refused',
        content: {
          kind: 'failed',
          happened: `Approved options setup ${id} could not be read.`,
          why: extractServerMessage(failure, 'The server did not answer.'),
          next: 'Nothing was substituted and no account was chosen. Check again, or choose a version in Options setups.',
          actions: [this.recheckAction, this.listAction],
        },
      };
    }
    if (!this.version.hasValue()) return { kind: 'reading' };
    const version = this.version.value();
    if (version.id !== id) {
      return {
        kind: 'refused',
        content: {
          kind: 'unavailable',
          happened: `The answer for approved options setup ${id} named a different one, ${version.id}.`,
          why: 'An answer about another version is never used in its place.',
          next: 'Nothing was applied and no account was chosen. Check again, or choose a version in Options setups.',
          actions: [this.recheckAction, this.listAction],
        },
      };
    }
    if (version.status !== 'ready') {
      return {
        kind: 'refused',
        content: {
          kind: 'unavailable',
          happened: `Approved options setup ${id} for ${version.underlying} is ${version.status} and cannot be deployed.`,
          why: version.status_explanation,
          next: 'No other version was chosen in its place and no account was chosen. Open this version, or choose another in Options setups.',
          actions: [{ id: 'open', label: 'Open this version', route: optionsEvidenceRoute('golden', id) }, this.listAction, this.recheckAction],
        },
      };
    }
    return { kind: 'ready', version };
  });

  constructor() {
    // A version waiting for an account can be revoked, or its rules can change,
    // while the owner decides. Paused while the tab is hidden, and while a refusal
    // stands: a refusal is lifted only by asking again.
    const timer = setInterval(() => {
      if (this.document.visibilityState !== 'visible') return;
      if (this.state().kind === 'refused' || this.version.isLoading()) return;
      this.version.reload();
    }, RECHECK_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  protected onAction(actionId: string): void {
    if (actionId === 'recheck' && !this.version.isLoading()) this.version.reload();
  }
}

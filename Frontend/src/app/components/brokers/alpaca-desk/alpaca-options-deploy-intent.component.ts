import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import { PINNED_VERSION_RECHECK_ACTION, pinnedVersionWatch } from '../../../fleet/pinned-version-watch';
import { isOptionsSetupId } from '../../../fleet/options-setup-handoff';
import { OPTIONS_SETUPS_ROOT, optionsEvidenceRoute } from '../../../options-setups/options-setups.address';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import type { WorkspaceAction } from '../../../shared/workspace/workspace-action';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { extractServerMessage } from '../../broker/operation-error';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../options-backtest/golden-option-setups.service';

type IntentState =
  | { readonly kind: 'reading' }
  | { readonly kind: 'ready'; readonly version: GoldenOptionSetup }
  | { readonly kind: 'refused'; readonly content: WorkspaceStateContent };

/**
 * The approved options version the account list is carrying to Deploy
 * (`?options_setup=`, PRD #3490 R23, I02), shown exactly as it was chosen while the
 * owner chooses an account. It is the options twin of the signal side's panel for
 * `?golden_qualification=`; both keep the version with `pinnedVersionWatch` and
 * word only what they find.
 *
 * It reads the version by its own id, which is what options Deploy reads, and says
 * what it finds: the name, symbol and source run when it is ready, and a refusal that
 * names it when it is stale, revoked, unreadable or answered as another version. The
 * cards below carry the version unchanged either way, so Deploy, which reads it again
 * at its own boundary and applies its own eligibility and submission checks, refuses
 * it by the same facts.
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

  protected readonly watch = pinnedVersionWatch<GoldenOptionSetup>({
    id: this.optionsSetupId,
    isValidId: isOptionsSetupId,
    read: (id) => this.golden.get(id),
    answeredId: (version) => version.id,
    isReady: (version) => version.status === 'ready',
  });

  private readonly listAction: WorkspaceAction = { id: 'versions', label: 'Choose a version in Options setups', route: [OPTIONS_SETUPS_ROOT] };

  protected readonly state = computed<IntentState>(() => {
    const id = this.optionsSetupId();
    const phase = this.watch.phase();
    switch (phase.kind) {
      case 'reading':
        return { kind: 'reading' };
      case 'invalid':
        return {
          kind: 'refused',
          content: {
            kind: 'invalid',
            happened: 'This link does not name an approved options setup.',
            next: 'Nothing was applied and no account was chosen. Choose a version in Options setups to continue.',
            actions: [this.listAction],
          },
        };
      case 'failed':
        return {
          kind: 'refused',
          content: {
            kind: 'failed',
            happened: `Approved options setup ${id} could not be read.`,
            why: extractServerMessage(phase.failure, 'The server did not answer.'),
            next: 'Nothing was substituted and no account was chosen. Check again, or choose a version in Options setups.',
            actions: [PINNED_VERSION_RECHECK_ACTION, this.listAction],
          },
        };
      case 'substituted':
        return {
          kind: 'refused',
          content: {
            kind: 'unavailable',
            happened: `The answer for approved options setup ${id} named a different one, ${phase.answeredId}.`,
            why: 'An answer about another version is never used in its place.',
            next: 'Nothing was applied and no account was chosen. Check again, or choose a version in Options setups.',
            actions: [PINNED_VERSION_RECHECK_ACTION, this.listAction],
          },
        };
      case 'unavailable':
        return {
          kind: 'refused',
          content: {
            kind: 'unavailable',
            happened: `Approved options setup ${id} for ${phase.version.underlying} is ${phase.version.status} and cannot be deployed.`,
            why: phase.version.status_explanation,
            next: 'No other version was chosen in its place and no account was chosen. Open this version, or choose another in Options setups.',
            actions: [{ id: 'open', label: 'Open this version', route: optionsEvidenceRoute('golden', id) }, this.listAction, PINNED_VERSION_RECHECK_ACTION],
          },
        };
      case 'ready':
        return { kind: 'ready', version: phase.version };
    }
  });
}

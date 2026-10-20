import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { GoldenOptionSetupsService } from '../../../options-backtest/golden-option-setups.service';
import { OptionsQualificationEvidenceComponent } from '../../../options-backtest/options-qualification-evidence.component';
import { CopyButtonComponent } from '../../../../shared/copy-button/copy-button.component';
import { extractServerMessage } from '../../operation-error';
import type { OptionsBotConfiguration } from '../../broker-deploy-page/options-bot-deploy.service';

class ProofMismatchError extends Error {}

@Component({
  selector: 'app-options-bot-proof',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CopyButtonComponent, OptionsQualificationEvidenceComponent],
  templateUrl: './options-bot-proof.component.html',
})
export class OptionsBotProofComponent {
  readonly configuration = input.required<OptionsBotConfiguration>();
  private readonly service = inject(GoldenOptionSetupsService);
  protected readonly proof = resource({
    params: () => this.configuration(),
    loader: async ({ params }) => {
      const proof = await this.service.get(params.golden_setup_id);
      if (proof.setup_sha256 !== params.setup_sha256 || proof.rules_digest !== params.rules_digest) {
        throw new ProofMismatchError('The retained proof does not match this deployment’s exact setup and rules.');
      }
      return proof;
    },
  });
  protected readonly problem = computed(() => {
    const error = this.proof.error();
    if (error === undefined) return null;
    return error instanceof ProofMismatchError ? error.message
      : extractServerMessage(error, 'The retained Options proof could not be read.');
  });
}

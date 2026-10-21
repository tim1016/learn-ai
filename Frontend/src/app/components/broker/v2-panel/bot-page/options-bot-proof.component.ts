import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { GoldenOptionSetupsService } from '../../../options-backtest/golden-option-setups.service';
import { OptionsQualificationEvidenceComponent } from '../../../options-backtest/options-qualification-evidence.component';
import { CopyButtonComponent } from '../../../../shared/copy-button/copy-button.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { extractServerMessage } from '../../operation-error';
import type { OptionsBotConfiguration } from '../../broker-deploy-page/options-bot-deploy.service';

@Component({
  selector: 'app-options-bot-proof',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CopyButtonComponent, OptionsQualificationEvidenceComponent, RouterLink, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './options-bot-proof.component.html',
  styleUrl: './options-bot-proof.component.scss',
})
export class OptionsBotProofComponent {
  readonly configuration = input.required<OptionsBotConfiguration>();
  private readonly service = inject(GoldenOptionSetupsService);
  private readonly router = inject(Router);
  protected readonly evidenceQuery = computed(() => ({ view: 'lineage', bot_return: this.router.url }));
  protected readonly proof = resource({
    params: () => this.configuration().golden_setup_id,
    loader: ({ params }) => this.service.get(params),
  });
  protected readonly identityMatches = computed(() => {
    if (!this.proof.hasValue()) return false;
    const retained = this.proof.value();
    const pinned = this.configuration();
    return retained.id === pinned.golden_setup_id && retained.setup_sha256 === pinned.setup_sha256
      && retained.rules_digest === pinned.rules_digest;
  });
  protected readonly problem = computed(() => {
    const error = this.proof.error();
    if (error === undefined) return null;
    return extractServerMessage(error, 'The retained Options proof could not be read.');
  });
}

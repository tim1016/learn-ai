import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink } from '@angular/router';
import { filter, map } from 'rxjs';
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
  private readonly botPath = toSignal(this.router.events.pipe(
    filter(event => event instanceof NavigationEnd), map(event => event.urlAfterRedirects),
  ), { initialValue: this.router.url });
  protected readonly evidenceQuery = computed(() => ({ view: 'lineage', bot_return: this.botPath() }));
  protected readonly proof = resource({
    params: () => this.configuration().golden_setup_id,
    loader: ({ params }) => this.service.get(params),
  });
  protected readonly identityMatches = computed(() => {
    const retained = this.proof.hasValue() ? this.proof.value() : null;
    const pinned = this.configuration();
    return retained !== null && retained.id === pinned.golden_setup_id && retained.setup_sha256 === pinned.setup_sha256
      && retained.rules_digest === pinned.rules_digest;
  });
  protected readonly problem = computed(() => {
    const error = this.proof.error();
    if (error === undefined) return null;
    return extractServerMessage(error, 'The retained Options proof could not be read.');
  });
}

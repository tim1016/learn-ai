import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';
import { BrokerDeployFormComponent } from '../broker-deploy-form/broker-deploy-form.component';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';

/**
 * The single Broker Deploy route. Broker context selects a capability adapter;
 * the established IBKR form remains intact while Alpaca consumes the same page.
 */
@Component({
  selector: 'app-broker-deploy-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BrokerDeployFormComponent, AlpacaDeployWorkflowComponent],
  templateUrl: './broker-deploy-page.component.html',
  styleUrl: './broker-deploy-page.component.scss',
})
export class BrokerDeployPageComponent {
  readonly broker = input('ibkr');
  readonly accountId = input('');

  private readonly brokers = inject(BrokersService);

  protected readonly normalizedBroker = computed(() => this.broker()?.trim().toLowerCase() || 'ibkr');

  protected readonly alpacaAccount = resource({
    params: () =>
      this.normalizedBroker() === 'alpaca' && (this.accountId()?.trim() ?? '') === ''
        ? 'alpaca'
        : undefined,
    loader: ({ params }) => this.brokers.getAccount(params),
  });

  protected readonly resolvedAlpacaAccountId = computed(() =>
    (this.accountId()?.trim() ?? '')
      || (this.alpacaAccount.hasValue() ? this.alpacaAccount.value().account_id : ''),
  );
}

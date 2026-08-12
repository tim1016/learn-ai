import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource } from '@angular/core';
import { Drawer } from 'primeng/drawer';

import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';

/**
 * Reusable right-side host for the established Alpaca deploy workflow.
 *
 * Route surfaces own visibility: the desk mirrors it in the URL while an
 * account-scoped Bots list keeps it as local page state. The workflow itself
 * remains the sole owner of deployment form state and submission behavior.
 */
@Component({
  selector: 'app-alpaca-deploy-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, AlpacaDeployWorkflowComponent],
  templateUrl: './alpaca-deploy-drawer.component.html',
  styleUrl: './alpaca-deploy-drawer.component.scss',
})
export class AlpacaDeployDrawerComponent {
  readonly visible = input.required<boolean>();
  readonly accountId = input('');
  readonly closed = output();

  private readonly brokers = inject(BrokersService);

  protected readonly account = resource({
    params: () => (this.visible() && this.accountId().trim() === '' ? 'alpaca' : undefined),
    loader: ({ params }) => this.brokers.getAccount(params),
  });

  protected readonly resolvedAccountId = computed(() =>
    this.accountId().trim() || (this.account.hasValue() ? this.account.value().account_id : ''),
  );

  protected close(): void {
    this.closed.emit();
  }

  protected onVisibilityChange(visible: boolean): void {
    if (!visible) this.close();
  }
}

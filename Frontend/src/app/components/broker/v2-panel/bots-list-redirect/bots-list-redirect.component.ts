import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  inject,
  input,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';

import { BrokersService } from '../../../../services/brokers.service';

/**
 * Unscoped bots-list redirect — resolves the broker's account id via
 * GET /api/brokers/{broker}/account then navigates to the account-scoped
 * route `/brokers/:broker/accounts/:accountId/bots`.
 *
 * Renders an honest error if the account fetch fails rather than
 * spinning silently.
 */
@Component({
  selector: 'app-bots-list-redirect',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (error()) {
      <div class="redirect-error" role="alert">
        <p>Could not load account for broker <strong>{{ broker() }}</strong>.</p>
        <p>{{ error() }}</p>
      </div>
    } @else {
      <div class="redirect-loading" aria-busy="true" aria-label="Redirecting to bot list">
        Loading&hellip;
      </div>
    }
  `,
  styles: `
    .redirect-error {
      padding: 1.5rem;
      color: var(--p-red-500, #ef4444);
    }
    .redirect-loading {
      padding: 1.5rem;
      color: var(--p-text-muted-color, #6b7280);
    }
  `,
})
export class BotsListRedirectComponent implements OnInit {
  readonly broker = input('alpaca');

  protected readonly error = signal<string | null>(null);

  private readonly brokersService = inject(BrokersService);
  private readonly router = inject(Router);

  async ngOnInit(): Promise<void> {
    try {
      const account = await this.brokersService.getAccount(this.broker());
      await this.router.navigate(
        ['/brokers', this.broker(), 'accounts', account.account_id, 'bots'],
        { replaceUrl: true },
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Unknown error.';
      this.error.set(msg);
    }
  }
}

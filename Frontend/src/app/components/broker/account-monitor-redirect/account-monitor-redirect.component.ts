import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { BrokerService } from '../../../services/broker.service';
import { legacyAccountMonitorFragmentTarget } from '../account-desk/account-desk-legacy-fragments';

/** One-time, read-only redirect for retired Account Monitor bookmarks. */
@Component({
  selector: 'app-account-monitor-redirect',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './account-monitor-redirect.component.html',
})
export class AccountMonitorRedirectComponent {
  private readonly broker = inject(BrokerService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  constructor() {
    void this.redirect();
  }

  private async redirect(): Promise<void> {
    try {
      const roster = await this.broker.accounts();
      const account = roster.rows.length === 1 ? roster.rows[0] : null;
      if (account === null) {
        await this.router.navigate(['/broker/accounts'], { replaceUrl: true });
        return;
      }

      const target = legacyAccountMonitorFragmentTarget(this.route.snapshot.fragment);
      await this.router.navigate(
        ['/broker/accounts', account.account_id],
        target === null
          ? { replaceUrl: true }
          : { fragment: target.anchor, replaceUrl: true },
      );
    } catch {
      await this.router.navigate(['/broker/accounts'], { replaceUrl: true });
    }
  }
}

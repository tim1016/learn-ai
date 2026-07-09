import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { AccountFreezeBanner } from '../../../api/account-reconciliation.types';

@Component({
  selector: 'app-account-freeze-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './account-freeze-banner.component.html',
  styleUrl: './account-freeze-banner.component.scss',
})
export class AccountFreezeBannerComponent {
  readonly banner = input.required<AccountFreezeBanner>();
  readonly actionLabel = input<string | null>(null);
  readonly actionClicked = output();
}

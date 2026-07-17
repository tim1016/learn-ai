import { ChangeDetectionStrategy, Component, inject, input, output } from '@angular/core';

import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';

/** Header control that changes only the account route, leaving the desk lens intact. */
@Component({
  selector: 'app-account-desk-account-switcher',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './account-desk-account-switcher.component.html',
  styleUrl: './account-desk-account-switcher.component.scss',
})
export class AccountDeskAccountSwitcherComponent {
  readonly accountId = input.required<string>();
  readonly accountChange = output<string>();
  readonly directory = inject(AccountDeskDirectoryStore);

  choose(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLSelectElement) || !target.value) return;
    this.accountChange.emit(target.value);
  }
}

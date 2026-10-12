import { ChangeDetectionStrategy, Component, input, output, signal } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ConfirmDeleteComponent } from '../../../shared/research-record/confirm-delete.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { SavedSetup } from '../options-backtest.types';

export type LibraryStatus = 'loading' | 'ready' | 'error';

/**
 * The saved setups at the left, collapsible: the shipped examples first and
 * marked as examples, then the owner's, newest first (the server's order). A
 * click loads one into the form. A saved one can be deleted; an example has no
 * delete. One that can no longer be opened is listed with its reason and
 * cannot be loaded, but can still be deleted.
 */
@Component({
  selector: 'app-options-backtest-side-list',
  imports: [AssetIdentityComponent, ConfirmDeleteComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './side-list.component.html',
  styleUrl: './side-list.component.scss',
})
export class SideListComponent {
  readonly setups = input.required<readonly SavedSetup[]>();
  readonly status = input.required<LibraryStatus>();
  readonly error = input<string | null>(null);
  /** The name of the setup the form holds, if it came from this list. */
  readonly selected = input<string | null>(null);
  /** Opening needs the session clock; until it is read nothing can be opened. */
  readonly canOpen = input(true);
  /** The server's reason the last delete did not happen. */
  readonly message = input<string | null>(null);
  readonly opened = output<SavedSetup>();
  readonly removed = output<string>();
  readonly retry = output();

  protected readonly collapsed = signal(false);

  protected toggle(): void {
    this.collapsed.update((collapsed) => !collapsed);
  }
}

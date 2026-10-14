import { ChangeDetectionStrategy, Component, ElementRef, input, output, viewChild } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ConfirmDeleteComponent } from '../../../shared/research-record/confirm-delete.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { anchorBelow, closePopover } from '../../broker/v2-panel/strategy-view/popover-placement';
import type { SavedSetup } from '../options-backtest.types';

export type LibraryStatus = 'loading' | 'ready' | 'error';

let nextMenuId = 0;

/** The space between the button and the list. */
const MENU_GAP_PX = 8;

/**
 * The saved setups behind one button in the page header, which also names the
 * setup the form holds: the shipped examples first and marked as examples, then
 * the owner's, newest first (the server's order). A click loads one into the
 * form and puts the list away. A saved one can be deleted; an example has no
 * delete. One that can no longer be opened is listed with its reason and
 * cannot be loaded, but can still be deleted.
 */
@Component({
  selector: 'app-options-backtest-saved-setups-menu',
  imports: [AssetIdentityComponent, ConfirmDeleteComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './saved-setups-menu.component.html',
  styleUrl: './saved-setups-menu.component.scss',
})
export class SavedSetupsMenuComponent {
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

  protected readonly popoverId = `ob-saved-setups-${nextMenuId++}`;
  private readonly trigger = viewChild.required<ElementRef<HTMLButtonElement>>('trigger');
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  /**
   * The list opens under its button. The place is set before the browser
   * shows it, so it never appears elsewhere for a frame; the style keeps it
   * inside the window.
   */
  protected placeUnderButton(event: Event): void {
    if (!('newState' in event) || event.newState !== 'open') return;
    const { left, bottom } = anchorBelow(this.trigger().nativeElement);
    const panel = this.panel().nativeElement;
    panel.style.setProperty('--menu-left', `${left}px`);
    panel.style.top = `${bottom + MENU_GAP_PX}px`;
  }

  protected choose(item: SavedSetup): void {
    this.opened.emit(item);
    closePopover(this.panel().nativeElement);
  }
}

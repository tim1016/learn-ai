import { ChangeDetectionStrategy, Component, ElementRef, input, model, signal, viewChild } from '@angular/core';

import { BotEndEditorComponent } from './bot-end-editor.component';
import type { BotEndFields } from './bot-end-fields';

let nextPopoverId = 0;

/**
 * A bot's end's fields over the page (#2607), one shell for Deploy and the
 * bot's page: a native `[popover]` dialog with its title and close button,
 * the shared editor, and the host's own answer and action projected below.
 *
 * Over the page, never in it: opening it never makes a Deploy column taller
 * (PR #2581), and a panel poll never rewrites what the owner types. The
 * editor takes the keyboard as it opens. The host's opening button targets
 * `id`, which is minted per instance.
 */
@Component({
  selector: 'app-bot-end-popover',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndEditorComponent],
  template: `
    <div #panel [id]="id" class="bot-end-popover" popover role="dialog" [attr.aria-labelledby]="titleId" (toggle)="onToggle($event)">
      <header>
        <h3 [id]="titleId">{{ heading() }}</h3>
        <button type="button" [attr.popovertarget]="id" popovertargetaction="hide">{{ closeLabel() }}</button>
      </header>
      <app-bot-end-editor [(fields)]="fields" [keepOffered]="keepOffered()" [locked]="locked()" />
      <ng-content />
    </div>
  `,
  styleUrl: './bot-end-popover.component.scss',
})
export class BotEndPopoverComponent {
  readonly heading = input.required<string>();
  readonly closeLabel = input('Done');
  readonly fields = model.required<BotEndFields>();
  readonly keepOffered = input(true);
  readonly locked = input(false);

  readonly id = `bot-end-popover-${nextPopoverId++}`;
  protected readonly titleId = `${this.id}-title`;
  private readonly openState = signal(false);
  /** The fields are open over the page. */
  readonly isOpen = this.openState.asReadonly();

  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');
  private readonly editor = viewChild.required(BotEndEditorComponent);

  /** Open the fields over the page, as a Change button pointing at it would. */
  show(): void {
    const panel = this.panel().nativeElement;
    if ('showPopover' in panel) panel.showPopover();
  }

  hide(): void {
    const panel = this.panel().nativeElement;
    if ('hidePopover' in panel) panel.hidePopover();
  }

  /** Says what each field needs now. */
  revealErrors(): void {
    this.editor().revealErrors();
  }

  /** The fields take the keyboard as they open over the page. */
  protected onToggle(event: Event): void {
    if (!('newState' in event)) return;
    this.openState.set(event.newState === 'open');
    if (event.newState === 'open') this.editor().focus();
  }
}

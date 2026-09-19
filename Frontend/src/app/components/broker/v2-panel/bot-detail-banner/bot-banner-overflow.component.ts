import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  input,
  signal,
  viewChild,
} from '@angular/core';

let nextOverflowId = 0;

/** Shared, intentionally small action popover for bot-detail banners. */
@Component({
  selector: 'app-bot-banner-overflow',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      #trigger
      type="button"
      [attr.aria-label]="label()"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="menuId"
      (click)="toggle()"
    >⋯</button>
    <div [id]="menuId" class="bot-banner-overflow__menu" [hidden]="!open()">
      <ng-content />
    </div>
  `,
  styleUrl: './bot-banner-overflow.component.scss',
  host: {
    // On the host so Escape closes the menu whether the trigger or a projected
    // action has the keyboard.
    '(keydown.escape)': 'dismiss()',
  },
})
export class BotBannerOverflowComponent {
  readonly label = input.required<string>();
  protected readonly open = signal(false);
  protected readonly menuId = `bot-banner-overflow-${nextOverflowId++}`;
  private readonly trigger = viewChild.required<ElementRef<HTMLButtonElement>>('trigger');

  protected toggle(): void {
    this.open.update((value) => !value);
  }

  /** Close and hand the keyboard back to the trigger — Escape from a menu
   * action would otherwise strand focus on a now-hidden node. A closed menu
   * leaves the event alone. */
  protected dismiss(): void {
    if (!this.open()) return;
    this.open.set(false);
    this.trigger().nativeElement.focus();
  }
}

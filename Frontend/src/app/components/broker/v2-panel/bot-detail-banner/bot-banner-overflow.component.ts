import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from '@angular/core';

let nextOverflowId = 0;

/** Shared, intentionally small action popover for bot-detail banners. */
@Component({
  selector: 'app-bot-banner-overflow',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      [attr.aria-label]="label()"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="menuId"
      (click)="toggle()"
    >⋯</button>
    @if (open()) {
      <div [id]="menuId" class="bot-banner-overflow__menu">
        <ng-content />
      </div>
    }
  `,
  styleUrl: './bot-banner-overflow.component.scss',
})
export class BotBannerOverflowComponent {
  readonly label = input.required<string>();
  protected readonly open = signal(false);
  protected readonly menuId = `bot-banner-overflow-${nextOverflowId++}`;

  protected toggle(): void {
    this.open.update((value) => !value);
  }
}

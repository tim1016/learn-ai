import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { NgOptimizedImage } from '@angular/common';
import { RouterLink } from '@angular/router';

export type ShellAccountMode = 'paper' | 'live' | 'unknown';

/** Universal shell chrome with typed projection boundaries for feature slices. */
@Component({
  selector: 'app-top-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgOptimizedImage, RouterLink],
  template: `
    <header
      class="top-bar"
      [class.top-bar--paper]="accountMode() === 'paper'"
      [class.top-bar--live]="accountMode() === 'live'"
      aria-label="Botasur application"
    >
      <div class="top-bar__left">
        <div class="top-bar__nav" data-shell-slot="nav">
          <ng-content select="[shell-nav]" />
        </div>
      </div>
      <a class="top-bar__brand" routerLink="/data-lab" aria-label="Botasur home">
        <img
          class="top-bar__brand-lockup"
          ngSrc="/assets/brand/botasur-header-mark.svg"
          width="188"
          height="42"
          priority
          alt=""
        />
        <span class="top-bar__brand-name">Botasur</span>
      </a>
      <div class="top-bar__right">
        <div class="top-bar__connection" data-shell-slot="connection">
          <ng-content select="[shell-connection]" />
        </div>
      </div>
    </header>
  `,
  styleUrl: './top-bar.component.scss',
})
export class TopBarComponent {
  readonly accountMode = input<ShellAccountMode>('unknown');
}

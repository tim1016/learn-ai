import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

/** Universal shell chrome with typed projection boundaries for feature slices. */
@Component({
  selector: 'app-top-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <header class="top-bar" aria-label="Market Scope application">
      <div class="top-bar__left">
        <a class="top-bar__wordmark" routerLink="/data-lab" aria-label="Market Scope home">Market Scope</a>
        <div class="top-bar__nav" data-shell-slot="nav">
          <ng-content select="[shell-nav]" />
        </div>
      </div>
      <div class="top-bar__right">
        <div class="top-bar__connection" data-shell-slot="connection">
          <ng-content select="[shell-connection]" />
        </div>
      </div>
    </header>
  `,
  styleUrl: './top-bar.component.scss',
})
export class TopBarComponent {}

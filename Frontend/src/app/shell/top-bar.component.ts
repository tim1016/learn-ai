import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { NgOptimizedImage } from '@angular/common';
import { RouterLink } from '@angular/router';

export type ShellAccountMode = 'paper' | 'live' | 'unknown';

/** Universal shell chrome with typed projection boundaries for feature slices.
 *
 * The header wears a solid worst-case account-mode tint (owner decision
 * 2026-10-03, superseding #2168's neutral header): an all-Paper fleet paints
 * Paper, anything else — Live, Shadow, an unread or errored verdict, or an
 * empty roster — paints Live. The per-lane pills (ADR 0059 D8; PR #2161)
 * remain the account-mode trust anchor; the tint is the glanceable
 * frame-level cue, never the per-lane truth. */
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

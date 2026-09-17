import { ChangeDetectionStrategy, Component, computed, effect, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { ActivatedRouteSnapshot, Router, RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { AlpacaLiveBannerComponent } from './shell/alpaca-live-banner.component';
import { MarkdownDrawerHostComponent } from './shared/markdown-drawer/markdown-drawer-host.component';
import { AlpacaLiveVerdictService } from './services/alpaca-live-verdict.service';
import { FleetDirectoryService } from './fleet/fleet-directory.service';
import { AppMenubarComponent } from './shell/app-menubar.component';
import { TopBarComponent } from './shell/top-bar.component';
import { PageBodyComponent } from './shell/page-body.component';
import { pageTitleFor } from './shell/app-menu';
import { CurrentUrlService } from './shell/current-url.service';
import { WorkspaceTitleContextService } from './shell/workspace-title-context.service';
import {
  accountWorkspaceLocation,
  accountWorkspaceTitle,
} from './fleet/account-workspace';
import { laneDisplayName, laneDisplayNameText } from './fleet/fleet-directory.types';

// The global JobsDrawer / floating "Jobs" launcher was removed in favor
// of per-feature SSE-driven progress UIs (e.g. the Engine Lab run
// banner). JobsService stays mounted via providedIn:'root' so features
// can still consume Jobs SSE without a shared drawer surface.
@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterOutlet,
    AppMenubarComponent,
    AlpacaLiveBannerComponent,
    TopBarComponent,
    PageBodyComponent,
    MarkdownDrawerHostComponent,
    Toast,
  ],
  styles: [`
    :host {
      display: flex;
      min-height: 100dvh;
      background: var(--bg-canvas);
      color: var(--text-primary);
    }

    .shell {
      display: flex;
      flex: 1;
      min-width: 0;
      min-height: 100dvh;
      flex-direction: column;
    }

    .shell-actions {
      display: flex;
      min-width: 0;
      align-items: center;
      justify-content: flex-end;
      gap: var(--space-2);
      white-space: nowrap;
      /* The live-verdict pills are the account-mode trust anchor (ADR 0059
         D8): one badge per lane (FleetDirectoryService.lanesOf('alpaca')),
         so the row's natural width grows with the fleet. The header's
         minmax(0, 1fr) side columns let content overflow past their own
         track rather than being clamped, which used to bleed the pills
         under the centered Botasur logo at in-between widths. Wrapping
         unconditionally (not just under the old 760px mobile query) keeps
         every pill inside this column at any width, never past it. */
      flex-wrap: wrap;
    }

    @media (max-width: 760px) {
      .shell-actions {
        gap: 3px;
      }
    }

    /* Named container "ide" drives the .ide-grid breakpoints declared in
       styles.scss. Lives here (outside any per-page component) so
       container-query measurement is unaffected by tab switches, modal
       mounts, or page-level transforms. */
    .main {
      flex: 1;
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow-x: auto;
      container: ide / inline-size;
    }

    .main-content {
      flex: 1;
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
  `],
  template: `
    <div class="shell">
      <app-top-bar>
        <app-menubar shell-nav />
        <div class="shell-actions" shell-connection>
          @for (lane of alpacaLanes(); track lane.clerk_id) {
            <app-alpaca-live-banner [lane]="lane" />
          } @empty {
            <!-- Never zero badges. An unresolved roster is itself an
                 undetermined mode, and the banner says so loudly. -->
            <app-alpaca-live-banner [lane]="null" />
          }
        </div>
      </app-top-bar>
      <main class="main">
        <div class="main-content">
          <app-page-body [fullBleed]="isFullBleedRoute()">
            <router-outlet />
          </app-page-body>
        </div>
      </main>
    </div>
    <app-markdown-drawer-host />
    <p-toast position="top-right" />
  `,
})
export class AppComponent {
  private readonly alpacaLive = inject(AlpacaLiveVerdictService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly title = inject(Title);
  private readonly router = inject(Router);
  private readonly currentUrl = inject(CurrentUrlService).url;
  private readonly titleContext = inject(WorkspaceTitleContextService);

  /** Inside an account workspace the window names what is open and the account
   * it is open on — "Gallery · Paper" (ADR 0064 Decision 6). The account's
   * *name* is `laneDisplayName`, never its Paper/Live mode: the two are
   * separate facts that only read alike while a lane has no nickname.
   *
   * Everything a workspace title needs is in the URL except the open bot's
   * label, which only the bot panel holds; it writes that into
   * `WorkspaceTitleContextService` while its page is open. Reading it only
   * when the URL names a bot is what keeps a stale label from ever reaching a
   * tab's title.
   *
   * A workflow opened over the workspace — the Deploy drawer's `?deploy` — is
   * not a tab, so it does not rename the window; the menubar names it. */
  private readonly workspaceTitle = computed(() => {
    const location = accountWorkspaceLocation(this.currentUrl());
    if (location === null) return null;
    const lanes = this.fleetDirectory.lanesOf(location.broker);
    const lane = lanes.find((candidate) => candidate.clerk_id === location.clerkId);
    return accountWorkspaceTitle(
      location.tab,
      lane === undefined ? null : laneDisplayNameText(laneDisplayName(lane, lanes)),
      location.botSid === null ? null : this.titleContext.botLabel(),
    );
  });

  protected readonly pageTitle = computed(
    () => this.workspaceTitle() ?? pageTitleFor(this.currentUrl()),
  );
  protected readonly alpacaLanes = computed(() => this.fleetDirectory.lanesOf('alpaca'));
  protected readonly isFullBleedRoute = computed(() => {
    this.currentUrl();
    return activeRouteHasData(this.router.routerState.snapshot.root, 'fullBleed');
  });

  constructor() {
    effect(() => this.title.setTitle(this.pageTitle() ?? 'Botasur'));
    // The Alpaca account-mode banner is the ADR 0011 trust anchor for the
    // Alpaca path (ADR 0059 D8): one root poll, rendered from the server
    // verdict, never composed on the client.
    this.alpacaLive.start();
  }
}

function activeRouteHasData(route: ActivatedRouteSnapshot, key: string): boolean {
  return Boolean(route.data[key]) || route.children.some((child) => activeRouteHasData(child, key));
}

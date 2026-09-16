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
  protected readonly pageTitle = computed(() => pageTitleFor(this.currentUrl()));
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

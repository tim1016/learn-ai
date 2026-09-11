import { ChangeDetectionStrategy, Component, computed, effect, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { ActivatedRouteSnapshot, Router, RouterLink, RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { BrokerBannerComponent } from './shell/broker-banner.component';
import { AlpacaLiveBannerComponent } from './shell/alpaca-live-banner.component';
import { MarkdownDrawerHostComponent } from './shared/markdown-drawer/markdown-drawer-host.component';
import { BrokerHealthService } from './services/broker-health.service';
import { AlpacaLiveVerdictService } from './services/alpaca-live-verdict.service';
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
    RouterLink,
    AppMenubarComponent,
    BrokerBannerComponent,
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
    }

    .shell-quick-link {
      display: inline-flex;
      min-height: 30px;
      align-items: center;
      gap: 5px;
      padding: 0 7px;
      border-radius: var(--radius-sm);
      color: var(--text-primary);
      font-size: var(--fs-xs);
      font-weight: var(--fw-semi);
      text-decoration: none;
    }

    .shell-quick-link:hover {
      background: rgba(255, 255, 255, 0.08);
    }

    .shell-quick-link i {
      color: var(--text-subtle);
      font-size: 0.7rem;
    }

    @media (max-width: 760px) {
      .shell-actions {
        gap: 3px;
      }

      .shell-quick-link {
        width: 30px;
        justify-content: center;
        padding: 0;
      }

      .shell-quick-link span {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
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
      <app-top-bar [accountMode]="shellAccountMode()">
        <app-menubar shell-nav />
        <nav class="shell-actions" shell-connection aria-label="Quick links and account status">
          <a class="shell-quick-link" routerLink="/brokers/alpaca/bots">
            <i class="pi pi-server" aria-hidden="true"></i>
            <span>Bots</span>
          </a>
          <a class="shell-quick-link" routerLink="/brokers/alpaca/gallery">
            <i class="pi pi-th-large" aria-hidden="true"></i>
            <span>Gallery</span>
          </a>
          <app-broker-banner />
          <app-alpaca-live-banner />
        </nav>
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
  private readonly brokerHealth = inject(BrokerHealthService);
  private readonly alpacaLive = inject(AlpacaLiveVerdictService);
  private readonly title = inject(Title);
  private readonly router = inject(Router);
  private readonly currentUrl = inject(CurrentUrlService).url;
  protected readonly pageTitle = computed(() => pageTitleFor(this.currentUrl()));
  protected readonly shellAccountMode = computed(() => {
    const verdict = this.alpacaLive.verdict()?.final_verdict;
    if (verdict === 'paper') return 'paper';
    if (verdict === 'live-unarmed' || verdict === 'live-armed') return 'live';
    return 'unknown';
  });
  protected readonly isFullBleedRoute = computed(() => {
    this.currentUrl();
    return activeRouteHasData(this.router.routerState.snapshot.root, 'fullBleed');
  });

  constructor() {
    effect(() => this.title.setTitle(this.pageTitle() ?? 'Botasur'));
    // Single-source-of-truth poll for the global banner. Components
    // read ``BrokerHealthService.health()`` instead of polling
    // /api/broker/health from per-page mounts.
    this.brokerHealth.start();
    // The Alpaca account-mode banner is the ADR 0011 trust anchor for the
    // Alpaca path (ADR 0059 D8): one root poll, rendered from the server
    // verdict, never composed on the client.
    this.alpacaLive.start();
  }
}

function activeRouteHasData(route: ActivatedRouteSnapshot, key: string): boolean {
  return Boolean(route.data[key]) || route.children.some((child) => activeRouteHasData(child, key));
}

import { ChangeDetectionStrategy, Component, computed, effect, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Title } from '@angular/platform-browser';
import { ActivatedRouteSnapshot, NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { filter, map, startWith } from 'rxjs';
import { AppSidebarComponent } from './shell/app-sidebar.component';
import { BrokerBannerComponent } from './shell/broker-banner.component';
import { BreadcrumbComponent } from './shell/breadcrumb.component';
import { MarkdownDrawerHostComponent } from './shared/markdown-drawer/markdown-drawer-host.component';
import { BrokerHealthService } from './services/broker-health.service';
import { TopBarComponent } from './shell/top-bar.component';
import { PageBodyComponent } from './shell/page-body.component';
import { pageTitleFor } from './shell/app-menu';

// The global JobsDrawer / floating "Jobs" launcher was removed in favor
// of per-feature SSE-driven progress UIs (e.g. the Engine Lab run
// banner). JobsService stays mounted via providedIn:'root' so features
// can still consume Jobs SSE without a shared drawer surface.
@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterOutlet,
    AppSidebarComponent,
    BrokerBannerComponent,
    BreadcrumbComponent,
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
    <app-sidebar />
    <div class="shell">
      <app-top-bar [pageTitle]="pageTitle()">
        <app-breadcrumb shell-breadcrumbs />
        <app-broker-banner shell-connection />
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
  private readonly title = inject(Title);
  private readonly router = inject(Router);
  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((event): event is NavigationEnd => event instanceof NavigationEnd),
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );
  protected readonly pageTitle = computed(() => pageTitleFor(this.currentUrl()));
  protected readonly isFullBleedRoute = computed(() => {
    this.currentUrl();
    return activeRouteHasData(this.router.routerState.snapshot.root, 'fullBleed');
  });

  constructor() {
    effect(() => this.title.setTitle(this.pageTitle() ?? 'Market Scope'));
    // Single-source-of-truth poll for the global banner. Components
    // read ``BrokerHealthService.health()`` instead of polling
    // /api/broker/health from per-page mounts.
    this.brokerHealth.start();
  }
}

function activeRouteHasData(route: ActivatedRouteSnapshot, key: string): boolean {
  return Boolean(route.data[key]) || route.children.some((child) => activeRouteHasData(child, key));
}

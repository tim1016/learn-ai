import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { AppSidebarComponent } from './shell/app-sidebar.component';
import { BrokerBannerComponent } from './shell/broker-banner.component';
import { BreadcrumbComponent } from './shell/breadcrumb.component';
import { MarkdownDrawerHostComponent } from './shared/markdown-drawer/markdown-drawer-host.component';
import { BrokerHealthService } from './services/broker-health.service';
import { TopBarComponent } from './shell/top-bar.component';

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
      <app-top-bar>
        <app-breadcrumb shell-breadcrumbs />
        <app-broker-banner shell-connection />
      </app-top-bar>
      <main class="main">
        <div class="main-content">
          <router-outlet />
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

  constructor() {
    this.title.setTitle('Market Scope');
    // Single-source-of-truth poll for the global banner. Components
    // read ``BrokerHealthService.health()`` instead of polling
    // /api/broker/health from per-page mounts.
    this.brokerHealth.start();
  }
}

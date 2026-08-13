import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { AppSidebarComponent } from './shell/app-sidebar.component';
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
    TopBarComponent,
    MarkdownDrawerHostComponent,
    Toast,
  ],
  styles: [`
    :host {
      display: flex;
      min-height: 100vh;
      background: var(--bg-canvas);
      color: var(--text-primary);
    }

    /* Named container "ide" drives the .ide-grid breakpoints declared in
       styles.scss. Lives here (outside any per-page component) so
       container-query measurement is unaffected by tab switches, modal
       mounts, or page-level transforms. */
    .main {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      overflow-x: auto;
      container: ide / inline-size;
    }

    .main-content {
      flex: 1;
      min-width: 0;
    }
  `],
  template: `
    <app-sidebar />
    <main class="main">
      <app-top-bar />
      <div class="main-content">
        <router-outlet />
      </div>
    </main>
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

import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { AppSidebarComponent } from './shell/app-sidebar.component';
import { BrokerBannerComponent } from './shell/broker-banner.component';
import { MethodologyDrawerComponent } from './shared/methodology-drawer/methodology-drawer.component';
import { BrokerHealthService } from './services/broker-health.service';

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
    MethodologyDrawerComponent,
    Toast,
  ],
  styles: [`
    :host {
      display: flex;
      min-height: 100vh;
      background: var(--bg-canvas);
      color: var(--text-primary);
    }

    .main {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      overflow-x: auto;
    }

    .main-content {
      flex: 1;
      min-width: 0;
      padding: var(--page-pad-y) var(--page-pad-x);
    }
  `],
  template: `
    <app-sidebar />
    <main class="main">
      <app-broker-banner />
      <div class="main-content">
        <router-outlet />
      </div>
    </main>
    <app-methodology-drawer />
    <p-toast position="top-right" />
  `,
})
export class AppComponent {
  private readonly brokerHealth = inject(BrokerHealthService);

  constructor() {
    // Single-source-of-truth poll for the global banner. Components
    // read ``BrokerHealthService.health()`` instead of polling
    // /api/broker/health from per-page mounts.
    this.brokerHealth.start();
  }
}

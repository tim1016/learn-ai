import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { AppSidebarComponent } from './shell/app-sidebar.component';
import { MethodologyDrawerComponent } from './shared/methodology-drawer/methodology-drawer.component';

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, AppSidebarComponent, MethodologyDrawerComponent, Toast],
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
      padding: var(--page-pad-y) var(--page-pad-x);
      overflow-x: auto;
    }
  `],
  template: `
    <app-sidebar />
    <main class="main">
      <router-outlet />
    </main>
    <app-methodology-drawer />
    <p-toast position="top-right" />
  `,
})
export class AppComponent {}

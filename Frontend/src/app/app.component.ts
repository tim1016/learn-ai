import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { AppSidebarComponent } from './shell/app-sidebar.component';
import { MethodologyDrawerComponent } from './shared/methodology-drawer/methodology-drawer.component';

@Component({
  selector: 'app-root',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, AppSidebarComponent, MethodologyDrawerComponent],
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
      padding: 1.25rem 1.5rem;
      overflow-x: auto;
    }
  `],
  template: `
    <app-sidebar />
    <main class="main">
      <router-outlet />
    </main>
    <app-methodology-drawer />
  `,
})
export class AppComponent {}

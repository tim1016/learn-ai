import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { Router, RouterModule } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { MessageService } from 'primeng/api';
import { vi } from 'vitest';
import axe from 'axe-core';
import { AppComponent } from './app.component';
import { BrokerHealthService } from './services/broker-health.service';
import { LiveRunsService } from './services/live-runs.service';

class FakeBrokerHealthService {
  readonly health = signal(null);
  readonly bannerState = signal(null);
  readonly lifecycleAction = signal(null);
  start = vi.fn();
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
}

class FakeLiveRunsService {
  startHostRunner = vi.fn().mockResolvedValue(undefined);
}

@Component({ template: '<p>Route body</p>', changeDetection: ChangeDetectionStrategy.OnPush })
class ShellSmokeRouteComponent {}

describe('AppComponent', () => {
  let fixture: ComponentFixture<AppComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [
        AppComponent,
        RouterModule.forRoot([
          { path: 'data-lab', component: ShellSmokeRouteComponent },
          { path: 'options-lab', component: ShellSmokeRouteComponent, data: { fullBleed: true } },
          { path: 'research-lab/strategy-runs/:id', component: ShellSmokeRouteComponent },
          { path: 'research-lab/walk-forward/:id', component: ShellSmokeRouteComponent },
          { path: 'research-lab/monte-carlo/:id', component: ShellSmokeRouteComponent },
          { path: 'research-lab/baselines/:id', component: ShellSmokeRouteComponent },
          { path: 'research-lab/signal-report/:id', component: ShellSmokeRouteComponent },
        ]),
      ],
      providers: [
        MessageService,
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render the app sidebar', () => {
    expect(fixture.nativeElement.querySelector('app-sidebar')).toBeTruthy();
  });

  it('renders the universal top bar and applies the fallback browser title', () => {
    expect(fixture.nativeElement.querySelector('.shell > app-top-bar')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-page-body')).toBeTruthy();
    expect(TestBed.inject(Title).getTitle()).toBe('Market Scope');
  });

  it('sets the browser title from the current menu page title', async () => {
    const router = TestBed.inject(Router);

    await router.navigateByUrl('/options-lab');
    fixture.detectChanges();

    expect(TestBed.inject(Title).getTitle()).toBe('Options Lab');
  });

  it('keeps the application banner outside the main landmark', async () => {
    const results = await axe.run(fixture.nativeElement, {
      runOnly: { type: 'rule', values: ['landmark-banner-is-top-level'] },
    });

    expect(results.violations).toEqual([]);
  });

  it('keeps standard pages inset and declared workspaces full-bleed', async () => {
    const router = TestBed.inject(Router);
    const pageBody = () => fixture.nativeElement.querySelector('app-page-body') as HTMLElement;

    await router.navigateByUrl('/data-lab');
    fixture.detectChanges();
    expect(pageBody().classList.contains('page-body--full-bleed')).toBe(false);

    await router.navigateByUrl('/options-lab');
    fixture.detectChanges();
    expect(pageBody().classList.contains('page-body--full-bleed')).toBe(true);
  });

  it('keeps the shell visible for standard and research-detail routes', async () => {
    const router = TestBed.inject(Router);

    for (const url of [
      '/data-lab',
      '/research-lab/strategy-runs/run-42',
      '/research-lab/walk-forward/wf-42',
      '/research-lab/monte-carlo/mc-42',
      '/research-lab/baselines/baseline-42',
      '/research-lab/signal-report/42',
    ]) {
      await router.navigateByUrl(url);
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('app-top-bar')).toBeTruthy();
      expect(fixture.nativeElement.textContent).toContain('Route body');
    }
  });

  it('renders the global broker banner in the top-bar connection region', () => {
    const sidebar = fixture.nativeElement.querySelector('app-sidebar');
    const connection = fixture.nativeElement.querySelector('[data-shell-slot="connection"]');
    expect(sidebar?.querySelector('app-broker-banner')).toBeNull();
    expect(connection?.querySelector('app-broker-banner')).toBeTruthy();
  });

  it('should contain a router-outlet', () => {
    expect(fixture.nativeElement.querySelector('router-outlet')).toBeTruthy();
  });
});

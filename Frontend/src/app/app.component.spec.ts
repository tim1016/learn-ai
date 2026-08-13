import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { Router, RouterModule } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { MessageService } from 'primeng/api';
import { vi } from 'vitest';
import { AppComponent } from './app.component';
import { BrokerHealthService } from './services/broker-health.service';

class FakeBrokerHealthService {
  readonly health = signal(null);
  readonly bannerState = signal(null);
  readonly lifecycleAction = signal(null);
  start = vi.fn();
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
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
          { path: 'research-lab/strategy-runs/:id', component: ShellSmokeRouteComponent },
        ]),
      ],
      providers: [
        MessageService,
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
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

  it('renders the universal top bar and applies the Market Scope browser title', () => {
    expect(fixture.nativeElement.querySelector('main > app-top-bar')).toBeTruthy();
    expect(TestBed.inject(Title).getTitle()).toBe('Market Scope');
  });

  it('keeps the shell visible while representative routes change', async () => {
    const router = TestBed.inject(Router);

    for (const url of ['/data-lab', '/research-lab/strategy-runs/run-42']) {
      await router.navigateByUrl(url);
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('app-top-bar')).toBeTruthy();
      expect(fixture.nativeElement.textContent).toContain('Route body');
    }
  });

  it('should render the broker banner inside the sidebar', () => {
    const sidebar = fixture.nativeElement.querySelector('app-sidebar');
    expect(sidebar?.querySelector('app-broker-banner')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('main > app-broker-banner')).toBeNull();
  });

  it('should contain a router-outlet', () => {
    expect(fixture.nativeElement.querySelector('router-outlet')).toBeTruthy();
  });
});

import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { Router, RouterModule } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { MessageService } from 'primeng/api';
import { vi } from 'vitest';
import axe from 'axe-core';
import { AppComponent } from './app.component';
import { BrokerHealthService } from './services/broker-health.service';
import {
  AlpacaLiveVerdictService,
  UNPOLLED_LANE_STATE,
  type LaneVerdictState,
} from './services/alpaca-live-verdict.service';
import { FleetDirectoryService } from './fleet/fleet-directory.service';
import { provideFleetDirectory, testLane, TEST_CLERK_ID } from './fleet/fleet-directory-testing';
import type { AlpacaLiveVerdict } from './api/alpaca.types';

class FakeBrokerHealthService {
  readonly health = signal(null);
  readonly bannerState = signal(null);
  readonly lifecycleAction = signal(null);
  start = vi.fn();
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
}

class FakeAlpacaLiveVerdictService {
  private readonly states = signal<ReadonlyMap<string, LaneVerdictState>>(new Map());
  start = vi.fn();

  stateFor(clerkId: string): LaneVerdictState {
    return this.states().get(clerkId) ?? UNPOLLED_LANE_STATE;
  }

  setState(clerkId: string, state: LaneVerdictState): void {
    this.states.update((current) => new Map(current).set(clerkId, state));
  }
}

function verdictStub(finalVerdict: AlpacaLiveVerdict['final_verdict']): AlpacaLiveVerdict {
  return {
    configured_mode: finalVerdict === 'paper' ? 'paper' : 'live',
    observed_account_id: null,
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    envelope_agreement: 'not_applicable',
    shadow_state: 'not_applicable',
    loss_hold: 'not_applicable',
    final_verdict: finalVerdict,
    headline: 'stub verdict',
    detail: 'stub detail',
    observed_at_ms: 1_700_000_000_000,
  };
}

@Component({ template: '<p>Route body</p>', changeDetection: ChangeDetectionStrategy.OnPush })
class ShellSmokeRouteComponent {}

describe('AppComponent', () => {
  let fixture: ComponentFixture<AppComponent>;
  let directory: ReturnType<typeof provideFleetDirectory>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    directory = provideFleetDirectory();
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
        { provide: AlpacaLiveVerdictService, useClass: FakeAlpacaLiveVerdictService },
        { provide: FleetDirectoryService, useValue: directory.useValue },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render the app menubar inside the top bar', () => {
    const nav = fixture.nativeElement.querySelector('[data-shell-slot="nav"]');
    expect(nav?.querySelector('app-menubar')).toBeTruthy();
  });

  it('renders the universal top bar and applies the fallback browser title', () => {
    expect(fixture.nativeElement.querySelector('.shell > app-top-bar')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-page-body')).toBeTruthy();
    expect(TestBed.inject(Title).getTitle()).toBe('Botasur');
  });

  it.each([
    ['paper', 'top-bar--paper'],
    ['live-unarmed', 'top-bar--live'],
    ['live-armed', 'top-bar--live'],
  ] as const)('colors the shell from the server-owned %s verdict', (finalVerdict, expectedClass) => {
    const service = TestBed.inject(AlpacaLiveVerdictService) as unknown as FakeAlpacaLiveVerdictService;

    service.setState(TEST_CLERK_ID, { verdict: verdictStub(finalVerdict), lastError: null });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.top-bar')?.classList.contains(expectedClass)).toBe(true);
  });

  it('treats a lane whose mode cannot be determined as live, never as calm-unknown', () => {
    const service = TestBed.inject(AlpacaLiveVerdictService) as unknown as FakeAlpacaLiveVerdictService;

    service.setState(TEST_CLERK_ID, { verdict: null, lastError: new Error('down') });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.top-bar')?.classList.contains('top-bar--live')).toBe(true);
  });

  it('renders one badge per lane, each labeled with its own lane and isolated from the others', () => {
    directory.rebind({
      observed_at_ms: 2,
      clerks: [
        testLane({ clerk_id: 'clrk_paper', broker: 'alpaca', display_label: 'Paper' }),
        testLane({ clerk_id: 'clrk_live', broker: 'alpaca', display_label: 'Live' }),
      ],
    });
    const service = TestBed.inject(AlpacaLiveVerdictService) as unknown as FakeAlpacaLiveVerdictService;
    service.setState('clrk_paper', { verdict: verdictStub('paper'), lastError: null });
    service.setState('clrk_live', { verdict: null, lastError: new Error('down') });
    fixture.detectChanges();

    const badges = fixture.nativeElement.querySelectorAll('app-alpaca-live-banner [role="status"]');
    expect(badges.length).toBe(2);
    expect(badges[0].textContent).toContain('Paper');
    expect(badges[0].textContent).not.toContain('assume real money');
    expect(badges[1].textContent).toContain('Live');
    expect(badges[1].textContent).toContain('assume real money');
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

  it('renders quick broker links and global status controls in the top-bar connection region', () => {
    const nav = fixture.nativeElement.querySelector('[data-shell-slot="nav"]');
    const connection = fixture.nativeElement.querySelector('[data-shell-slot="connection"]');
    expect(nav?.querySelector('app-broker-banner')).toBeNull();
    expect(connection?.querySelector('app-broker-banner')).toBeTruthy();
    expect(connection?.querySelector('a[href="/brokers/alpaca?surface=bots"]')).toBeTruthy();
    expect(connection?.querySelector('a[href="/brokers/alpaca?surface=gallery"]')).toBeTruthy();
  });

  it('should contain a router-outlet', () => {
    expect(fixture.nativeElement.querySelector('router-outlet')).toBeTruthy();
  });
});

import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { Router, RouterModule } from '@angular/router';
import { Title } from '@angular/platform-browser';
import { MessageService } from 'primeng/api';
import { vi } from 'vitest';
import axe from 'axe-core';
import { AppComponent } from './app.component';
import {
  AlpacaLiveVerdictService,
  UNPOLLED_LANE_STATE,
  type LaneVerdictState,
} from './services/alpaca-live-verdict.service';
import { fakeVerdictState } from './testing/alpaca-live-verdict-fixtures';
import { FleetDirectoryService } from './fleet/fleet-directory.service';
import { provideFleetDirectory, testLane, TEST_CLERK_ID } from './fleet/fleet-directory-testing';

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

  it('renders the shell mode-neutral for every verdict — the per-lane pills own the mode signal', () => {
    const service = TestBed.inject(AlpacaLiveVerdictService) as unknown as FakeAlpacaLiveVerdictService;

    for (const finalVerdict of ['paper', 'live-unarmed', 'live-armed'] as const) {
      service.setState(TEST_CLERK_ID, fakeVerdictState(finalVerdict));
      fixture.detectChanges();

      const header = fixture.nativeElement.querySelector('.top-bar');
      expect(header?.classList.contains('top-bar--live')).toBe(false);
      expect(header?.classList.contains('top-bar--paper')).toBe(false);
    }
  });

  it('keeps the header neutral for a lane whose mode cannot be determined — the badge carries the warning', () => {
    const service = TestBed.inject(AlpacaLiveVerdictService) as unknown as FakeAlpacaLiveVerdictService;

    service.setState(TEST_CLERK_ID, { verdict: null, lastError: new Error('down') });
    fixture.detectChanges();

    const header = fixture.nativeElement.querySelector('.top-bar');
    expect(header?.classList.contains('top-bar--live')).toBe(false);
    expect(header?.classList.contains('top-bar--paper')).toBe(false);
    const badge = fixture.nativeElement.querySelector('app-alpaca-live-banner [role="status"]');
    expect(badge?.textContent).toContain('assume real money');
  });

  it('renders one badge per lane, each labeled with its own lane and isolated from the others', async () => {
    directory.rebind({
      observed_at_ms: 2,
      clerks: [
        testLane({ clerk_id: 'clrk_paper', broker: 'alpaca', display_label: 'Paper' }),
        testLane({ clerk_id: 'clrk_live', broker: 'alpaca', display_label: 'Live' }),
      ],
    });
    // `rebind()` only stages the replacement, like the real service's next
    // `/api/broker-clerks` response; `refresh()` promotes it to what
    // `lanesOf()` reports.
    await directory.useValue.refresh?.();
    const service = TestBed.inject(AlpacaLiveVerdictService) as unknown as FakeAlpacaLiveVerdictService;
    service.setState('clrk_paper', fakeVerdictState('paper'));
    service.setState('clrk_live', { verdict: null, lastError: new Error('down') });
    fixture.detectChanges();

    const badges = fixture.nativeElement.querySelectorAll('app-alpaca-live-banner [role="status"]');
    expect(badges.length).toBe(2);
    expect(badges[0].textContent).toContain('Paper');
    expect(badges[0].textContent).not.toContain('assume real money');
    expect(badges[1].textContent).toContain('Live');
    expect(badges[1].textContent).toContain('assume real money');
  });

  it('renders a loud lanes-unknown badge while the header stays neutral when the roster is empty', async () => {
    // Every boot until /api/broker-clerks resolves, and indefinitely after a
    // failed load. Zero badges plus a silent shell is the calm-while-real-
    // money-trades failure this anchor exists to kill (#2110 D2) — the badge
    // is the loud surface now, not the header tint.
    directory.rebind({ observed_at_ms: 3, clerks: [] });
    // `rebind()` only stages the replacement; `refresh()` promotes it to
    // what `lanesOf()` reports, like the real service's next load.
    await directory.useValue.refresh?.();
    fixture.detectChanges();

    const badges = fixture.nativeElement.querySelectorAll('app-alpaca-live-banner [role="status"]');
    expect(badges.length).toBe(1);
    expect(badges[0].className).toContain('is-undetermined');
    expect(badges[0].textContent).toContain('Alpaca lanes unknown');
    expect(badges[0].textContent).toContain('assume real money');
    expect(fixture.nativeElement.querySelector('.top-bar')?.classList.contains('top-bar--live')).toBe(false);
    expect(fixture.nativeElement.querySelector('.top-bar')?.classList.contains('top-bar--paper')).toBe(false);
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
    const connection = fixture.nativeElement.querySelector('[data-shell-slot="connection"]');
    // The IBKR-era broker banner is gone (#2149): its health poll 404'd on
    // every cycle, so it could never render anything — the per-lane
    // alpaca-live badges below are the shell's only account-mode anchor.
    expect(connection?.querySelector('app-broker-banner')).toBeNull();
    expect(connection?.querySelector('a[href="/brokers/alpaca/bots"]')).toBeTruthy();
    expect(connection?.querySelector('a[href="/brokers/alpaca/gallery"]')).toBeTruthy();
  });

  it('should contain a router-outlet', () => {
    expect(fixture.nativeElement.querySelector('router-outlet')).toBeTruthy();
  });
});

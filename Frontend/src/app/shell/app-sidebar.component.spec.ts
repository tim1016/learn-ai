import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, RouterModule } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { BrokerHealthService } from '../services/broker-health.service';
import { LiveRunsService } from '../services/live-runs.service';
import { AppSidebarComponent } from './app-sidebar.component';

class FakeBrokerHealthService {
  readonly health = signal(null);
  readonly bannerState = signal(null);
  readonly lifecycleAction = signal(null);
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
}

class FakeLiveRunsService {
  startHostRunner = vi.fn().mockResolvedValue(undefined);
}

describe('AppSidebarComponent', () => {
  it('does not expose the retired Interactive Broker menu', () => {
    const fixture = setup();

    const groups = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>(
        'button.nav-group-header',
      ),
    ).map((button) => button.textContent?.trim());

    expect(groups).not.toContain('Interactive Broker');
  });

  it('adds Alpaca as a sibling broker group with one Deploy entry', () => {
    const fixture = setup();

    clickGroup(fixture, 'Alpaca');

    const links = navLinks(fixture);
    expect(links.get('Accounts')).toBe('/brokers/alpaca');
    expect(links.get('Deploy')).toBe('/brokers/alpaca/deploy');
    expect(links.get('Bots')).toBe('/brokers/alpaca/bots');
  });

  it('maps account-scoped Alpaca Deploy and Bots routes to their navigation slots', async () => {
    const fixture = setup();
    const router = TestBed.inject(Router);
    router.resetConfig([{
      path: 'brokers/:broker/accounts/:accountId/:surface',
      component: AppSidebarComponent,
    }]);

    for (const [surface, expectedLabel] of [['deploy', 'Deploy'], ['bots', 'Bots']]) {
      await router.navigateByUrl(`/brokers/alpaca/accounts/PA9/${surface}`);
      fixture.detectChanges();
      const activeLabels = Array.from(
        (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLAnchorElement>('a.nav-link.active'),
      ).map((link) => link.textContent?.trim());
      expect(activeLabels).toEqual([expectedLabel]);
    }
  });

  it('surfaces live options visualizations in the Options menu', () => {
    const fixture = setup();

    clickGroup(fixture, 'Options');

    const links = navLinks(fixture);
    expect(links.get('Options Chain (Live)')).toBe('/broker/options-chain');
    expect(links.get('Options Surface (3D)')).toBe('/broker/options-surface');
  });

  it('keeps live options visualizations in the Options menu', () => {
    const fixture = setup();

    clickGroup(fixture, 'Options');

    const labels = Array.from(navLinks(fixture).keys());
    expect(labels).toContain('Options Chain (Live)');
    expect(labels).toContain('Options Surface (3D)');
  });

  it('keeps validation and engine tooling under Strategy Tools', () => {
    const fixture = setup();

    clickGroup(fixture, 'Strategy Tools');

    const links = navLinks(fixture);
    expect(links.get('Strategy Validation')).toBe('/strategy-validation');
    expect(links.has('Deploy')).toBe(false);
    expect(links.get('Strategy Spec')).toBe('/spec-strategy');
    expect(links.get('Strategy Lab')).toBe('/strategy-lab');
    expect(links.has('Dashboard')).toBe(false);
    expect(links.has('Tracked Instruments')).toBe(false);
  });

  it('links the user-accessible legal notices page from Documentation', () => {
    const fixture = setup();

    clickGroup(fixture, 'Documentation');

    expect(navLinks(fixture).get('Legal Notices')).toBe('/legal/notices');
  });
});

function setup(): ComponentFixture<AppSidebarComponent> {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    imports: [AppSidebarComponent, RouterModule.forRoot([])],
    providers: [
      { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
      { provide: LiveRunsService, useClass: FakeLiveRunsService },
    ],
  });
  const fixture = TestBed.createComponent(AppSidebarComponent);
  fixture.detectChanges();
  return fixture;
}

function clickGroup(
  fixture: ComponentFixture<AppSidebarComponent>,
  label: string,
): void {
  const button = Array.from(
    (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>(
      'button.nav-group-header',
    ),
  ).find((candidate) => candidate.textContent?.includes(label));
  if (button === undefined) throw new Error(`menu group not found: ${label}`);
  button.click();
  fixture.detectChanges();
}

function navLinks(fixture: ComponentFixture<AppSidebarComponent>): Map<string, string> {
  const links = Array.from(
    (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLAnchorElement>(
      'a.nav-link',
    ),
  );
  return new Map(
    links.map((link) => [
      link.textContent?.trim().replace(/\s+/g, ' ') ?? '',
      link.getAttribute('href') ?? '',
    ]),
  );
}

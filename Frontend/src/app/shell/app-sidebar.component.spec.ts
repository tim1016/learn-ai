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
  it('surfaces the broker session mirror route in the Broker menu', () => {
    const fixture = setup();

    clickGroup(fixture, 'Broker');

    const link = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLAnchorElement>(
        'a.nav-link',
      ),
    ).find((candidate) => candidate.textContent?.includes('Session Mirror'));

    expect(link?.getAttribute('href')).toBe('/broker/session-mirror');
  });

  it('surfaces deploy and the operator manual beside bots in the Broker menu', () => {
    const fixture = setup();

    clickGroup(fixture, 'Broker');

    const links = navLinks(fixture);
    expect(links.get('Deploy')).toBe('/broker/deploy');
    expect(links.get('Bots')).toBe('/broker/bots');
    expect(links.get('Bot Manual')).toBe('/broker/bot-manual');
  });

  it('marks only the most specific Broker route active', async () => {
    const fixture = setup();
    const router = TestBed.inject(Router);
    router.resetConfig([{ path: 'broker/bot-manual', component: AppSidebarComponent }]);

    await router.navigateByUrl('/broker/bot-manual');
    fixture.detectChanges();

    const activeLabels = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLAnchorElement>('a.nav-link.active'),
    ).map((link) => link.textContent?.trim());

    expect(activeLabels).toEqual(['Bot Manual']);
  });

  it('uses Accounts as the Broker account navigation slot', () => {
    const fixture = setup();

    clickGroup(fixture, 'Broker');

    const links = navLinks(fixture);
    expect(links.get('Accounts')).toBe('/broker/accounts');
    expect(links.has('Account Monitor')).toBe(false);
  });

  it('surfaces live options visualizations in the Options menu', () => {
    const fixture = setup();

    clickGroup(fixture, 'Options');

    const links = navLinks(fixture);
    expect(links.get('Options Chain (Live)')).toBe('/broker/options-chain');
    expect(links.get('Options Surface (3D)')).toBe('/broker/options-surface');
  });

  it('keeps live options visualizations out of the Broker menu', () => {
    const fixture = setup();

    clickGroup(fixture, 'Broker');

    const labels = Array.from(navLinks(fixture).keys());
    expect(labels).toContain('Session Mirror');
    expect(labels).not.toContain('Options Chain (Live)');
    expect(labels).not.toContain('Options Surface (3D)');
  });

  it('keeps validation and engine tooling under Strategy Lab', () => {
    const fixture = setup();

    clickGroup(fixture, 'Strategy Lab');

    const links = navLinks(fixture);
    expect(links.get('Strategy Validation')).toBe('/strategy-validation');
    expect(links.has('Deploy')).toBe(false);
    expect(links.get('Strategy Spec')).toBe('/spec-strategy');
    expect(links.get('Engine Lab')).toBe('/engine');
    expect(links.has('Dashboard')).toBe(false);
    expect(links.has('Tracked Instruments')).toBe(false);
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

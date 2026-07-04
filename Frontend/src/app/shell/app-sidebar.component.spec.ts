import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterModule } from '@angular/router';
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

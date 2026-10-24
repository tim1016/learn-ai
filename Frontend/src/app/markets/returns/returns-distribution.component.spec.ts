import { Component, signal } from '@angular/core';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { of } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { ReturnsDistributionService, type ReturnDistributionStudy } from './returns-distribution.service';
import { DAY_MS, STUDY } from '../testing/returns-study';
import { ReturnsDistributionComponent } from './returns-distribution.component';
import { ReturnsStudyHold } from './returns-study-hold';

/** chart.js is module-mocked: the page spec drives bin selection through the
 * same options.onClick the real library invokes on canvas clicks. */
const harness = vi.hoisted(() => {
  return {
    instances: [] as {
      config: { options: { onClick?: (event: unknown, elements: { index: number }[]) => void } };
    }[],
  };
});

vi.mock('chart.js', () => {
  return {
    Chart: class {
      static register(..._args: unknown[]): void {}
      constructor(
        _canvas: unknown,
        config: { options: { onClick?: (event: unknown, elements: { index: number }[]) => void } },
      ) {
        harness.instances.push({ config: config as never });
      }
      destroy(): void {}
    },
    registerables: [],
  };
});

function fakeStore() {
  return {
    committedTicker: signal('SPY'),
    committedWindow: signal({
      startMsUtc: Date.UTC(2024, 6, 1),
      endMsUtc: Date.UTC(2024, 6, 30, 23, 59, 59),
    }),
  } as unknown as DataLabWorkspaceStore;
}

function fakeService() {
  return {
    distribution: vi.fn(() => of(STUDY)),
    minuteCandles: vi.fn(() =>
      of([
        {
          open: 545.1,
          high: 545.2,
          low: 545,
          close: 545.15,
          volume: 1200,
          timestamp: DAY_MS(2),
        },
      ]),
    ),
  } as unknown as ReturnsDistributionService;
}

/** The study mounted under a switch, as the router mounts it, so a tab change can unmount and mount it again. */
@Component({
  selector: 'app-remount-host',
  imports: [ReturnsDistributionComponent],
  template: '@if (mounted()) { <app-returns-distribution /> }',
})
class RemountHostComponent {
  readonly mounted = signal(true);
}

async function renderRemountable(service: ReturnsDistributionService, hold: ReturnsStudyHold) {
  const result = await render(RemountHostComponent, {
    providers: [
      { provide: DataLabWorkspaceStore, useValue: fakeStore() },
      { provide: ReturnsDistributionService, useValue: service },
      { provide: ReturnsStudyHold, useValue: hold },
    ],
  });
  return { ...result, host: result.fixture.componentInstance };
}

beforeEach(() => {
  harness.instances.length = 0;
});

describe('ReturnsDistributionComponent', () => {
  it('renders the study for the committed scope: stats, histogram, kind toggle', async () => {
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: fakeService() },
        ReturnsStudyHold,
      ],
    });

    expect(screen.getByText('Days studied')).toBeTruthy();
    expect(screen.getAllByText('2').length).toBeGreaterThan(0);
    expect(screen.getByText('VaR 95% (1 day)')).toBeTruthy();
    expect(
      screen.getByText('This request populated the data lake with 480 artifact(s) first.'),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Close → close' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Session only' })).toBeTruthy();
    expect(document.querySelector('canvas')).toBeTruthy();
  });

  it('renders every capture state distinctly — failure never reads as success', async () => {
    const failedStudy: ReturnDistributionStudy = {
      ...STUDY,
      capture: { status: 'failed', fetchedArtifactCount: 0, detail: 'TimeoutError: deadline' },
    };
    const service = {
      distribution: vi.fn(() => of(failedStudy)),
      minuteCandles: vi.fn(() => of([])),
    } as unknown as ReturnsDistributionService;
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: service },
        ReturnsStudyHold,
      ],
    });

    expect(
      screen.getByText(/This request could not populate the data lake/),
    ).toBeTruthy();
    expect(screen.getByText(/TimeoutError: deadline/)).toBeTruthy();
    expect(screen.queryByText(/already up to date/)).toBeNull();
  });

  it('switches the shown distribution when the return kind toggles', async () => {
    const service = fakeService();
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: service },
        ReturnsStudyHold,
      ],
    });

    // Both kinds report nDays=2 in this fixture; the kind identity is what
    // the toggle must swap — visible through which bins the chart redraws
    // with when the signal changes.
    await userEvent.click(screen.getByRole('button', { name: 'Session only' }));
    await waitFor(() => {
      const chart = harness.instances.at(-1);
      expect(chart?.config.options.onClick).toBeTypeOf('function');
    });
  });

  it('opens the basket drill-down on a chart bin click, then the candle pane on a day click', async () => {
    const service = fakeService();
    await render(ReturnsDistributionComponent, {
      providers: [
        { provide: DataLabWorkspaceStore, useValue: fakeStore() },
        { provide: ReturnsDistributionService, useValue: service },
        ReturnsStudyHold,
      ],
    });

    expect(screen.queryByText(/^Basket /)).toBeNull();
    await waitFor(() => expect(harness.instances.length).toBeGreaterThan(0));
    harness.instances[0]?.config.options.onClick?.(null, [{ index: 0 }]);

    expect(await screen.findByText('Basket 0.0% to 0.5% — 2 day(s)')).toBeTruthy();
    expect(screen.getByText('2024-07-02')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: /2024-07-02/ }));
    expect(await screen.findByText(/2024-07-02 — full trading day/)).toBeTruthy();
    expect(service.minuteCandles).toHaveBeenCalledWith('SPY', DAY_MS(2));
  });

  it('shows the study the place already holds when it is mounted again with the request unchanged, and asks Python nothing', async () => {
    const service = fakeService();
    const { host, fixture } = await renderRemountable(service, new ReturnsStudyHold());
    await screen.findByText('Days studied');
    expect(service.distribution).toHaveBeenCalledTimes(1);

    host.mounted.set(false);
    fixture.detectChanges();
    expect(screen.queryByText('Days studied'), 'the study was unmounted').toBeNull();
    host.mounted.set(true);
    fixture.detectChanges();

    expect(await screen.findByText('Days studied')).toBeTruthy();
    expect(service.distribution, 'a remount alone is no reason to ask again').toHaveBeenCalledTimes(1);
  });

  it('asks again once the place drops the instrument\'s studies, since the lake changed under them', async () => {
    const service = fakeService();
    const hold = new ReturnsStudyHold();
    const { host, fixture } = await renderRemountable(service, hold);
    await screen.findByText('Days studied');

    host.mounted.set(false);
    fixture.detectChanges();
    hold.invalidate('SPY');
    host.mounted.set(true);
    fixture.detectChanges();

    expect(await screen.findByText('Days studied')).toBeTruthy();
    expect(service.distribution).toHaveBeenCalledTimes(2);
  });

  it('asks for scope before anything else when the store has no committed window', async () => {
    const service = fakeService();
    await render(ReturnsDistributionComponent, {
      providers: [
        {
          provide: DataLabWorkspaceStore,
          useValue: { committedTicker: signal(''), committedWindow: signal(null) } as unknown as DataLabWorkspaceStore,
        },
        { provide: ReturnsDistributionService, useValue: service },
        ReturnsStudyHold,
      ],
    });

    expect(screen.getByText(/Pick an instrument and a window above/)).toBeTruthy();
    expect(service.distribution).not.toHaveBeenCalled();
  });
});

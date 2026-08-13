import { ChangeDetectionStrategy, Component } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { BotDetailBannerComponent } from './bot-detail-banner.component';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotDetailBannerComponent],
  template: `
    <app-bot-detail-banner
      [backLink]="backLink"
      backLabel="alpaca bots"
      [updatedAtMs]="updatedAtMs"
      snapshotStatus="Revision 42 stopped"
    >
      <span botBannerIdentity>EMA crossover · sid-001</span>
      <span botBannerQuote>SPY $625.10</span>
      <span botBannerStatus>Working</span>
      <button botBannerAction type="button">Stop</button>
      <button botBannerOverflow type="button" aria-label="More actions">⋯</button>
    </app-bot-detail-banner>
  `,
})
class BotDetailBannerHarnessComponent {
  readonly backLink = ['/brokers', 'alpaca', 'accounts', 'acc-1', 'bots'];
  readonly updatedAtMs = 1_753_800_000_000;
}

describe('BotDetailBannerComponent', () => {
  it('renders shared navigation and freshness with lens-specific projected content', async () => {
    await render(BotDetailBannerHarnessComponent, {
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('link', { name: /alpaca bots/i })).toBeTruthy();
    expect(screen.getByText('EMA crossover · sid-001')).toBeTruthy();
    expect(screen.getByText('SPY $625.10')).toBeTruthy();
    expect(screen.getByText('Working')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'More actions' })).toBeTruthy();
    expect(screen.getByText(/Updated/)).toBeTruthy();
    const revisionStatus = screen.getByRole('status', { name: 'Revision 42 stopped' });
    expect(revisionStatus.textContent).toBe('Revision 42 stopped');
  });
});

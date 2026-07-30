import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';
import { Router } from '@angular/router';

import { BrokersService } from '../../../../services/brokers.service';
import { BotsListRedirectComponent } from './bots-list-redirect.component';

/** Mock Router that records navigate calls without actually navigating. */
const mockRouter = {
  navigate: vi.fn().mockResolvedValue(true),
  url: '/',
};

function makeMockBrokersService(accountId: string | null) {
  return {
    getAccount:
      accountId !== null
        ? vi.fn().mockResolvedValue({ account_id: accountId, broker: 'alpaca' })
        : vi.fn().mockRejectedValue(new Error('Network error')),
  };
}

describe('BotsListRedirectComponent', () => {
  it('resolves account id and navigates to the scoped bots route', async () => {
    mockRouter.navigate.mockClear();
    const mockBrokersService = makeMockBrokersService('PA9');

    await render(BotsListRedirectComponent, {
      providers: [
        provideRouter([]),
        { provide: BrokersService, useValue: mockBrokersService },
        { provide: Router, useValue: mockRouter },
      ],
      componentInputs: { broker: 'alpaca' },
    });

    // Wait for the async ngOnInit to complete
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(mockBrokersService.getAccount).toHaveBeenCalledWith('alpaca');
    expect(mockRouter.navigate).toHaveBeenCalledWith(
      ['/brokers', 'alpaca', 'accounts', 'PA9', 'bots'],
      { replaceUrl: true },
    );
  });

  it('renders an error message when account fetch fails', async () => {
    const mockBrokersService = makeMockBrokersService(null);

    await render(BotsListRedirectComponent, {
      providers: [
        provideRouter([]),
        { provide: BrokersService, useValue: mockBrokersService },
        { provide: Router, useValue: mockRouter },
      ],
      componentInputs: { broker: 'alpaca' },
    });

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(await screen.findByText(/Network error/i)).toBeTruthy();
  });
});

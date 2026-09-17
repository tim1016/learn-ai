import { render, screen } from '@testing-library/angular';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it } from 'vitest';

import { AlpacaAccountListPageComponent } from './alpaca-account-list-page.component';
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';

async function renderList(query: Record<string, string> = {}) {
  const queryParamMap = convertToParamMap(query);
  const paramMap = convertToParamMap({});
  return render(AlpacaAccountListPageComponent, {
    providers: [
      provideFleetDirectory(),
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: {
          queryParamMap: of(queryParamMap),
          paramMap: of(paramMap),
          snapshot: { queryParamMap, paramMap },
        },
      },
    ],
  });
}

describe('AlpacaAccountListPageComponent', () => {
  it('lists every account under the Alpaca heading', async () => {
    await renderList();

    // #2183: the "Broker desk" eyebrow line above the page heading is
    // retired — the single "Alpaca" heading carries the eyebrow look itself.
    expect(screen.getByRole('heading', { name: 'Alpaca' })).toBeTruthy();
    expect(screen.queryByText('Broker desk')).toBeNull();
    expect(screen.getByRole('region', { name: 'Alpaca clerk lanes' })).toBeTruthy();
  });

  it('makes a broker-wide deploy intent an explicit lane choice', async () => {
    await renderList({ deploy: '' });

    expect(
      screen.getByText(/choose a ready Paper or Live account below to deploy a strategy/i),
    ).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Alpaca clerk lanes' })).toBeTruthy();
  });

  it('retires the surface hints — a ?surface query renders the plain directory', async () => {
    // The surface hints moved to real chooser routes (`/brokers/alpaca/bots`
    // and `/gallery`); a stale `?surface=` query on the list itself renders
    // the plain directory rather than annotating it.
    await renderList({ surface: 'bots' });

    expect(screen.getByRole('heading', { name: 'Choose an account' })).toBeTruthy();
    expect(
      screen.queryByText(/choose a ready Paper or Live account below to open its bots roster/i),
    ).toBeNull();
  });
});

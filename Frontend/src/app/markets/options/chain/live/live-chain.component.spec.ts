import { HttpErrorResponse } from '@angular/common/http';
import { screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { reads } from '../../../../components/options-backtest/testing/page-actions';
import { deferred } from '../../../../components/options-backtest/testing/deferred';
import { fakeScopedHealth, healthItem, projection } from '../../../../shared/scoped-health/testing/scoped-health-fixtures';
import type { ExpirationsResponse } from '../../../../api/broker-models';
import {
  AS_OF_MS,
  FAR,
  NEAR,
  connectedHealth,
  disconnectedHealth,
  engineReply,
  ibkrFrame,
  ibkrQuote,
  silentQuote,
} from './testing/fake-ibkr';
import { liveWorld, renderLive, startWith } from './testing/render-live';

const facts = () => reads(document.querySelector('app-chain-facts'));
const statement = () => reads(document.querySelector('app-chain-cross-check'));
const grid = () => screen.findByRole('table', { name: /Calls on the left/ });
const hasGrid = () => screen.queryByRole('table', { name: /Calls on the left/ }) !== null;

describe('Now · Live IBKR with no Gateway session', () => {
  it('says Live is unavailable with the reason the data plane gave, reads nothing from IBKR and opens no stream', async () => {
    const world = liveWorld(vi);
    world.health.set(disconnectedHealth());

    await renderLive(vi, world);

    expect(await screen.findByText('Now · Live IBKR is unavailable.')).toBeTruthy();
    expect(reads(document.querySelector('app-workspace-state-block'))).toContain('The FastAPI data-plane IBKR client is disconnected. IB Gateway/TWS may still be logged in, but market data cannot stream until this app session connects.');
    expect(facts()).toContain('Unavailable: Data-plane broker session disconnected.');
    expect(facts()).toContain('Not started. Nothing is subscribed until you press Start.');
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
    expect(world.expirations).not.toHaveBeenCalled();
    expect(world.feed.streams).toHaveLength(0);
    expect(document.querySelector('app-chain-cross-check')).toBeNull();
  });

  it('says what the data plane makes of the IBKR feed, and keeps its own reason where the controls would be', async () => {
    const feed = fakeScopedHealth(
      projection(healthItem({ kind: 'ibkr_market_data', severity: 'critical', explanation: 'The IBKR session is down. Live option chain and bot feed readiness are affected; archived history is not.' })),
    );
    const world = liveWorld(vi);
    world.health.set(disconnectedHealth());

    await renderLive(vi, world, {}, [feed.provider]);

    expect((await screen.findByText(/archived history is not/)).closest('.health-notice')?.textContent).toContain('Failing.');
    expect(feed.read).toHaveBeenCalledWith({ operations: ['live_chain'], instrument: null });
    expect(await screen.findByText('Now · Live IBKR is unavailable.')).toBeTruthy();
  });

  it('says so too when the data plane did not answer the connection check', async () => {
    const world = liveWorld(vi);
    world.health.set(null);
    world.healthFailure.set(new HttpErrorResponse({ status: 502, error: { detail: 'The data plane is restarting.' } }));

    await renderLive(vi, world);

    expect(await screen.findByText('The data plane did not answer the IBKR connection check: The data plane is restarting.')).toBeTruthy();
    expect(world.expirations).not.toHaveBeenCalled();
  });

  it('only checks, and claims nothing, before the first answer', async () => {
    const world = liveWorld(vi);
    world.health.set(null);

    await renderLive(vi, world);

    expect(await screen.findByText('Asking the data plane about its IBKR session.')).toBeTruthy();
    expect(screen.queryByText('Now · Live IBKR is unavailable.')).toBeNull();
  });

  it('offers the chain as soon as the session connects, and reads its expirations only then', async () => {
    const world = liveWorld(vi);
    world.health.set(disconnectedHealth());
    await renderLive(vi, world);
    await screen.findByText('Now · Live IBKR is unavailable.');

    world.health.set(connectedHealth());

    expect(await screen.findByRole('button', { name: 'Start' })).toBeTruthy();
    await waitFor(() => expect(world.expirations).toHaveBeenCalledWith('SPY'));
    expect(screen.queryByText('Now · Live IBKR is unavailable.')).toBeNull();
  });

  it('releases the stream and leaves the chain the moment the session is lost, and does not subscribe again when it returns', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame());
    await grid();

    world.health.set(disconnectedHealth());

    expect(await screen.findByText('Now · Live IBKR is unavailable.')).toBeTruthy();
    expect(world.feed.streams[0].closes).toBe(1);
    expect(hasGrid()).toBe(false);
    world.health.set(connectedHealth());
    await screen.findByRole('button', { name: 'Start' });
    expect(world.feed.streams).toHaveLength(1);
  });
});

describe('Now · Live IBKR with a session', () => {
  it('states the connection, the entitlement and the subscription, and subscribes to nothing before Start', async () => {
    const world = liveWorld(vi);

    await renderLive(vi, world);
    await screen.findByRole('button', { name: '500' });

    expect(facts()).toContain('Data-plane paper session connected.');
    expect(facts()).toContain('Not known until a subscription delivers quotes. The feed asks IBKR for real-time quotes only and never falls back to delayed ones.');
    expect(facts()).toContain('Not started. Nothing is subscribed until you press Start.');
    expect((screen.getByRole('button', { name: 'Start' }) as HTMLButtonElement).disabled).toBe(true);
    expect(world.feed.streams).toHaveLength(0);
    expect(screen.getByText('Nothing is subscribed.')).toBeTruthy();
  });

  it('opens one stream for the strikes picked when Start is pressed, and says what it holds', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);

    await startWith(499, 501);

    expect(world.feed.streams).toHaveLength(1);
    expect(world.feed.streams[0].symbol).toBe('SPY');
    expect(world.feed.streams[0].strikes).toEqual([499, 501]);
    expect(world.feed.streams[0].url).toContain(`expiry_ms=${NEAR}`);
    expect(facts()).toContain('Opening the stream for 4 contracts (2 strikes, calls and puts) and the underlying, 5 of IBKR\'s shared market-data lines.');
    expect(await screen.findByText('Waiting for the first snapshot.')).toBeTruthy();
  });

  it('draws the chain from the stream, and states what the cross-check columns compare', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);

    world.feed.streams[0].emit(ibkrFrame());
    const table = await grid();

    expect(facts()).toMatch(/Open: 2 contracts .* Latest snapshot \d{2}:\d{2}:\d{2}\./);
    expect(facts()).toContain('Real-time quotes have arrived for all 2 contracts.');
    expect(within(table).getAllByRole('columnheader', { name: 'Δ IBKR' })).toHaveLength(2);
    expect(within(table).getAllByRole('columnheader', { name: 'Δ Engine' })[0].getAttribute('title')).toContain('QuantLib delta');
    expect(within(table).getAllByRole('columnheader', { name: '|ΔΔ|' })[0].getAttribute('title')).toContain('never zero');
    for (const label of ['Quantity', 'Δ IBKR', 'Δ Engine', '|ΔΔ|', 'Valued with', 'As of', 'Not compared']) {
      expect.soft(statement(), label).toContain(label);
    }
    expect(statement()).toContain('Green up to 0.005, yellow up to 0.02, red above');
    expect(statement()).toContain('engine analytic_bs');
    const received = [...table.querySelectorAll('.vol-text[title^="Received "]')];
    expect(received.length).toBeGreaterThan(0);
    expect(received[0].getAttribute('title')).toMatch(/^Received \d{2}:\d{2}:\d{2}$/);
  });

  it('compares each contract\'s IBKR delta with the engine\'s, valued with IBKR\'s volatility and the snapshot\'s underlying price', async () => {
    const world = liveWorld(vi);
    world.price.mockImplementation(async (request) => engineReply(request.optionType === 'call' ? 0.41 : -0.4));
    await renderLive(vi, world);
    await startWith(500);

    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 503.25, quotes: [ibkrQuote(500, 'C', { iv: 0.31 }), ibkrQuote(500, 'P', { iv: 0.31 })] }));
    const table = await grid();

    await waitFor(() => expect(reads(table)).toContain('0.0100'));
    expect(reads(table)).toContain('0.0000');
    expect(world.price).toHaveBeenCalledWith({ spot: 503.25, strike: 500, volatility: 0.31, expirationMs: NEAR, optionType: 'call', engine: 'analytic_bs' });
    expect(world.price).toHaveBeenCalledWith({ spot: 503.25, strike: 500, volatility: 0.31, expirationMs: NEAR, optionType: 'put', engine: 'analytic_bs' });
    expect(statement()).toContain('Compared 2 of 2 contracts.');
    expect(table.querySelector('[data-band="yellow"]')).not.toBeNull();
    expect(table.querySelector('[data-band="green"]')).not.toBeNull();
  });

  it('reads unavailable, with its reason, where a delta is missing or a quote is stale, and never a gap of zero', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(499, 500, 501);

    world.feed.streams[0].emit(ibkrFrame({
      quotes: [
        silentQuote(499, 'C'),
        ibkrQuote(499, 'P', { ts_ms: AS_OF_MS - 41_000 }),
        ibkrQuote(500, 'C'),
        ibkrQuote(500, 'P'),
        ibkrQuote(501, 'C', { iv: null }),
        ibkrQuote(501, 'P', { bid: null }),
      ],
    }));
    const table = await grid();

    await waitFor(() => expect(statement()).toContain('Compared 2 of 6 contracts.'));
    const cells = [...table.querySelectorAll('.absent[title^="Not compared"]')];
    const reasons = cells.map((cell) => cell.getAttribute('title'));
    expect(cells.every((cell) => cell.textContent?.trim() === 'unavailable')).toBe(true);
    expect(reasons).toContain('Not compared: IBKR sent no delta for this contract');
    expect(reasons).toContain('Not compared: its quote is 41 s older than the snapshot, past the 5 s limit');
    expect(reasons).toContain('Not compared: IBKR sent no usable implied volatility, and the engine is valued with it');
    expect(reasons).toContain('Not compared: the contract has no two-sided quote (a bid and an ask above zero)');
    expect(statement()).toContain('Not compared: 1 no IBKR delta; 1 no usable IBKR volatility; 1 no two-sided quote; 1 quote older than 5 s.');
    // The two contracts that were compared agree with the engine exactly; no other cell shows a figure for the gap.
    expect(table.querySelectorAll('[data-band]')).toHaveLength(2);
    expect(table.querySelectorAll('[data-band="green"]')).toHaveLength(2);
    expect(table.textContent?.match(/0\.0000/g)).toHaveLength(2);
  });

  it('says a contract that expires today is not compared, because the engine has no whole day left', async () => {
    const world = liveWorld(vi);
    const expiryDay = Date.UTC(2099, 0, 15, 15, 0, 0);
    await renderLive(vi, world);
    await startWith(500);

    world.feed.streams[0].emit(ibkrFrame({ as_of_ms: expiryDay, quotes: [ibkrQuote(500, 'C', { ts_ms: expiryDay - 50 }), ibkrQuote(500, 'P', { ts_ms: expiryDay - 50 })] }));
    const table = await grid();

    expect(statement()).toContain('Compared 0 of 2 contracts. Not compared: 2 expire today.');
    expect(table.querySelector('.absent[title*="it expires today"]')).not.toBeNull();
    expect(world.price).not.toHaveBeenCalled();
  });

  it('says what the feed can and cannot say about entitlement when no contract has sent a quote', async () => {
    const world = liveWorld(vi);
    world.health.set(connectedHealth({ last_ibkr_code: 10089, last_ibkr_message: 'Requested market data requires additional subscription for API.' }));
    await renderLive(vi, world);
    await startWith(500);

    world.feed.streams[0].emit(ibkrFrame({ quotes: [silentQuote(500, 'C'), silentQuote(500, 'P')] }));
    await grid();

    expect(facts()).toContain('No contract has sent a quote.');
    expect(facts()).toContain('IBKR\'s latest message to the data plane, for any request, was 10089: Requested market data requires additional subscription for API.');
    expect(statement()).toContain('Compared 0 of 2 contracts. Not compared: 2 no IBKR delta.');
  });

  it('shows the reason a stream gave for stopping, and compares nothing while it is stopped', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame());
    await grid();
    await waitFor(() => expect(statement()).toContain('Compared 2 of 2'));

    world.feed.streams[0].fail('The option-chain stream stopped: the broker dropped the market-data request.');

    await waitFor(() => expect(facts()).toContain('Stopped: The option-chain stream stopped: the broker dropped the market-data request. The snapshot on screen is from'));
    expect(statement()).toContain('Compared 0 of 2 contracts. Not compared: 2 stream not live.');
  });

  it('fails in place when IBKR cannot list the expirations, with the reason, and offers to try again', async () => {
    const world = liveWorld(vi);
    world.expirations.mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: { detail: 'IBKR client not connected to Gateway.' } }));
    await renderLive(vi, world);

    expect(await screen.findByText('IBKR client not connected to Gateway.')).toBeTruthy();
    expect(screen.getByText('IBKR could not list SPY\'s expirations or strikes.')).toBeTruthy();

    await userEvent.setup().click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('button', { name: '500' })).toBeTruthy();
    expect(world.expirations).toHaveBeenCalledTimes(2);
  });

  it('says an expiry IBKR does not list is not, and opens none in its place', async () => {
    const world = liveWorld(vi);
    const unlisted = FAR + 86_400_000;

    await renderLive(vi, world, { expiryMs: unlisted });

    expect(await screen.findByText(/expiry is not among IBKR's current expirations for SPY/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '500' })).toBeNull();
    expect(world.strikes).not.toHaveBeenCalled();
  });

  it('picks the strikes within twenty dollars of the underlying only once a stream has told its price', async () => {
    const world = liveWorld(vi);
    world.strikes.mockResolvedValue({ symbol: 'SPY', expiry_ms: NEAR, strikes: [470, 480, 481, 500, 519, 521], fetched_at_ms: 0 });
    await renderLive(vi, world);
    await screen.findByRole('button', { name: '500' });
    const band = screen.getByRole('button', { name: /ATM ±/ }) as HTMLButtonElement;
    expect(band.disabled).toBe(true);

    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 500 }));
    await grid();
    await waitFor(() => expect(band.disabled).toBe(false));
    await userEvent.setup().click(band);

    const pressed = screen.getAllByRole('button', { pressed: true }).map((chip) => chip.textContent?.trim());
    expect(pressed).toEqual(['480', '481', '500', '519']);
  });
});

describe('switching what a live chain is of', () => {
  it('releases the subscription when the expiry changes, and a frame the old stream delivers late never reaches the new chain', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 111.11 }));
    await grid();
    expect(facts()).toContain('SPY 111.11');

    await userEvent.setup().click(screen.getByRole('button', { name: '19' }));

    await waitFor(() => expect(world.feed.streams[0].closes).toBe(1));
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 222.22 }));
    await screen.findByText('Nothing is subscribed.');
    expect(hasGrid()).toBe(false);
    expect(document.body.textContent).not.toContain('222.22');
    expect(document.body.textContent).not.toContain('111.11');
    expect(world.feed.streams).toHaveLength(1);
    await waitFor(() => expect(world.strikes).toHaveBeenLastCalledWith('SPY', FAR));
  });

  it('releases the subscription when the underlying changes, reads the new underlying\'s expirations, and ignores the old one\'s late frames', async () => {
    const world = liveWorld(vi);
    const { host, fixture } = await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 111.11 }));
    await grid();

    host.symbol.set('QQQ');
    fixture.detectChanges();

    await waitFor(() => expect(world.feed.streams[0].closes).toBe(1));
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 222.22 }));
    await waitFor(() => expect(world.expirations).toHaveBeenLastCalledWith('QQQ'));
    await screen.findByRole('button', { name: '500' });
    expect(hasGrid()).toBe(false);
    expect(document.body.textContent).not.toContain('222.22');
    expect(world.feed.streams).toHaveLength(1);
  });

  it('shows nothing of a frame on the held stream that names another expiry or underlying', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);

    world.feed.streams[0].emit(ibkrFrame({ expiry_ms: FAR, underlying_price: 333.33 }));
    world.feed.streams[0].emit(ibkrFrame({ symbol: 'QQQ', underlying_price: 444.44 }));

    expect(await screen.findByText('Waiting for the first snapshot.')).toBeTruthy();
    expect(hasGrid()).toBe(false);
    expect(document.body.textContent).not.toContain('333.33');
    expect(document.body.textContent).not.toContain('444.44');
  });

  it('drops an expiry listing, a strike list and an engine answer that arrive after the chain they were asked for was left', async () => {
    const world = liveWorld(vi);
    const lateListing = deferred<ExpirationsResponse>();
    world.expirations.mockImplementationOnce(() => lateListing.promise);
    const { host, fixture } = await renderLive(vi, world);

    host.symbol.set('QQQ');
    fixture.detectChanges();
    await screen.findByRole('button', { name: '500' });
    lateListing.resolve({ symbol: 'SPY', expirations_ms: [FAR + 86_400_000] });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.getByRole('button', { name: '15' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '20' })).toBeNull();
    expect(world.expirations).toHaveBeenLastCalledWith('QQQ');
  });

  it('closes the stream when the mode is left', async () => {
    const world = liveWorld(vi);
    const { fixture } = await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame());
    await grid();

    fixture.destroy();

    expect(world.feed.streams[0].closes).toBe(1);
  });
});

describe('pausing and a silent stream', () => {
  afterEach(() => vi.useRealTimers());

  it('pauses by releasing the stream, keeps the last snapshot as stale with nothing compared, and resumes with a new stream', async () => {
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 111.11 }));
    await grid();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Pause' }));

    expect(world.feed.streams[0].closes).toBe(1);
    await waitFor(() => expect(facts()).toMatch(/Paused\. The stream is closed and its lines are released\. The snapshot on screen is from .* and is stale\./));
    expect(facts()).toContain('SPY 111.11');
    expect(statement()).toContain('Compared 0 of 2 contracts. Not compared: 2 stream not live.');
    world.feed.streams[0].emit(ibkrFrame({ underlying_price: 999.99 }));
    expect(document.body.textContent).not.toContain('999.99');

    await user.click(screen.getByRole('button', { name: 'Resume' }));

    expect(world.feed.streams).toHaveLength(2);
    expect(world.feed.streams[1].url).toBe(world.feed.streams[0].url);
  });

  it('stops comparing once no frame has arrived for five seconds, though the stream never said it had stopped', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    const world = liveWorld(vi);
    await renderLive(vi, world);
    await startWith(500);
    world.feed.streams[0].emit(ibkrFrame());
    await grid();
    await vi.waitFor(() => expect(statement()).toContain('Compared 2 of 2 contracts.'));

    await vi.advanceTimersByTimeAsync(6_000);

    await vi.waitFor(() => expect(statement()).toContain('Compared 0 of 2 contracts. Not compared: 2 stream not live.'));
    expect(facts()).toContain('Open, but no snapshot has arrived for over 5 s.');
    world.feed.streams[0].emit(ibkrFrame());
    await vi.advanceTimersByTimeAsync(1_000);
    await vi.waitFor(() => expect(statement()).toContain('Compared 2 of 2 contracts.'));
  });
});

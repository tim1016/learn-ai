import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';
import {
  LastSessionCardComponent,
  type LastSessionLogTarget,
  type LastSessionNotice,
} from './last-session-card.component';

const CLEAN_NOTICE: LastSessionNotice = {
  tone: 'ok',
  title: 'Last session ended cleanly',
  detail: 'The previous run stopped without error (exit 0).',
  fix: 'Press Start Trading to begin a new session.',
};

const DIRTY_NOTICE: LastSessionNotice = {
  tone: 'bad',
  title: 'Safety halt — the bot stopped to protect the account',
  detail:
    'A trade the bot did not place was seen on the account. A position may still be open at the broker.',
  fix: 'Check the broker account, reconcile, and flatten any position the bot is no longer tracking before restarting.',
};

const WARN_NOTICE: LastSessionNotice = {
  tone: 'warn',
  title: 'Daily order cap reached',
  detail: 'The last run stopped after hitting its max-orders-per-day limit.',
  fix: 'This resets next session. Raise the cap on redeploy if that was intentional.',
};

function render(opts: {
  notice: LastSessionNotice | null;
  canRedeploy?: boolean;
  redeployQueryParams?: Record<string, string>;
  runLogTarget?: LastSessionLogTarget | null;
}): { el: HTMLElement; component: LastSessionCardComponent } {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection(), provideRouter([])],
  });
  const fixture = TestBed.createComponent(LastSessionCardComponent);
  fixture.componentRef.setInput('notice', opts.notice);
  fixture.componentRef.setInput('canRedeploy', opts.canRedeploy ?? false);
  fixture.componentRef.setInput(
    'redeployQueryParams',
    opts.redeployQueryParams ?? {},
  );
  fixture.componentRef.setInput('runLogTarget', opts.runLogTarget ?? null);
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('LastSessionCardComponent', () => {
  it('renders nothing when notice is null', () => {
    const { el } = render({ notice: null });

    expect(el.querySelector('[data-testid="last-session-stub"]')).toBeNull();
    expect(el.querySelector('[data-testid="last-session-card"]')).toBeNull();
    expect(el.textContent?.trim()).toBe('');
  });

  it('renders the thin clean stub (User Story #15) when notice.tone is ok', () => {
    const { el } = render({ notice: CLEAN_NOTICE });

    const stub = el.querySelector<HTMLElement>('[data-testid="last-session-stub"]');
    expect(stub).not.toBeNull();
    expect(stub?.textContent ?? '').toContain('Last session ended cleanly');
    // The thin stub is *not* the full card.
    expect(el.querySelector('[data-testid="last-session-card"]')).toBeNull();
  });

  it('renders the full card with title + detail + fix (User Story #16) when dirty', () => {
    const { el } = render({ notice: DIRTY_NOTICE });

    const card = el.querySelector<HTMLElement>('[data-testid="last-session-card"]');
    expect(card).not.toBeNull();
    expect(card?.classList.contains('bad')).toBe(true);
    expect(el.textContent ?? '').toContain('Safety halt');
    expect(
      (el.querySelector('[data-testid="last-session-detail"]')?.textContent ?? '').length,
    ).toBeGreaterThan(0);
    expect(
      (el.querySelector('[data-testid="last-session-fix"]')?.textContent ?? '').length,
    ).toBeGreaterThan(0);
  });

  it('renders the warn-tone card for a daily-cap exhaustion', () => {
    const { el } = render({ notice: WARN_NOTICE });

    const card = el.querySelector<HTMLElement>('[data-testid="last-session-card"]');
    expect(card?.classList.contains('warn')).toBe(true);
    expect(el.textContent ?? '').toContain('Daily order cap reached');
  });

  it('renders the Re-deploy link only when canRedeploy is true', () => {
    const { el: withRedeploy } = render({
      notice: DIRTY_NOTICE,
      canRedeploy: true,
      redeployQueryParams: { strategy_key: 'spy_15m_breakout' },
    });
    expect(
      withRedeploy.querySelector('[data-testid="redeploy-link"]'),
    ).not.toBeNull();

    const { el: withoutRedeploy } = render({
      notice: DIRTY_NOTICE,
      canRedeploy: false,
    });
    expect(
      withoutRedeploy.querySelector('[data-testid="redeploy-link"]'),
    ).toBeNull();
  });

  it('renders the View run log button only when a runLogTarget is present', () => {
    const { el: withLog } = render({
      notice: DIRTY_NOTICE,
      runLogTarget: { runId: 'run_abc', live: false },
    });
    expect(withLog.querySelector('[data-testid="view-run-log"]')).not.toBeNull();

    const { el: withoutLog } = render({ notice: DIRTY_NOTICE, runLogTarget: null });
    expect(withoutLog.querySelector('[data-testid="view-run-log"]')).toBeNull();
  });

  it('emits viewRunLogRequested with the target when the View run log button is clicked', () => {
    const target: LastSessionLogTarget = { runId: 'run_abc', live: false };
    const { el, component } = render({
      notice: DIRTY_NOTICE,
      runLogTarget: target,
    });

    let received: LastSessionLogTarget | undefined;
    component.viewRunLogRequested.subscribe((t) => (received = t));
    el.querySelector<HTMLButtonElement>('[data-testid="view-run-log"]')?.click();

    expect(received).toEqual(target);
  });
});

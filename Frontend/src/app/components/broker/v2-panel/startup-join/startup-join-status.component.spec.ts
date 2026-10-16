import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { StartupJoinView } from '../lib/broker-v2-panel.types';
import { StartupJoinStatusComponent } from './startup-join-status.component';

describe('StartupJoinStatusComponent', () => {
  it('counts down a filling run to its refusal deadline', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(1_790_000_000_000);
    try {
      const view: StartupJoinView = {
        run_id: 'run-2', state: 'filling', label: 'Filling history', explanation: 'IBKR history is being read.',
        opened_at_ms: 1_790_000_000_000, live_from_ms: null, joined_minute_start_ms: null,
        deadline_ms: 1_790_000_061_000, missing_start_ms: null, missing_end_ms: null, reason_code: null,
      };
      const { fixture } = await render(StartupJoinStatusComponent, { inputs: { view } });

      expect(screen.getByText('1:01')).toBeTruthy();
      await vi.advanceTimersByTimeAsync(2_000);
      fixture.detectChanges();
      expect(screen.getByText('0:59')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

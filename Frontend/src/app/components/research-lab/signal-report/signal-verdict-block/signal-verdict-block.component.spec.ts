import signalJobResult from '@repo-contracts/fixtures/signal-engine-job-result-v1.json';
import { render } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { SignalEngineResult } from '../../../../services/research.service';
import { toSignalEngineResultFromJob, type SignalEngineJobResultRaw } from '../../signal-runner/signal-result.mapper';
import { SignalVerdictBlockComponent } from './signal-verdict-block.component';

const RAW = signalJobResult as unknown as SignalEngineJobResultRaw;

async function renderBlock(result: SignalEngineResult) {
  const view = await render(SignalVerdictBlockComponent, { inputs: { result } });
  return view.container as HTMLElement;
}

describe('SignalVerdictBlockComponent', () => {
  it('shows the stage the server recorded', async () => {
    const page = await renderBlock(toSignalEngineResultFromJob(RAW));

    expect(page.querySelector('.badge-value')?.textContent?.trim()).toBe('2');
    expect(page.querySelector('.stage-chip')?.textContent?.trim()).toBe('Research');
  });

  it('says the stage is not recorded when the result has none, instead of a Stage 0 rejection', async () => {
    const mapped = toSignalEngineResultFromJob(RAW);
    if (mapped.graduation === null) throw new Error('the fixture carries a graduation');
    const page = await renderBlock({ ...mapped, graduation: { ...mapped.graduation, stageInfo: null } });

    expect(page.querySelector('.badge-value')?.textContent?.trim()).toBe('—');
    expect(page.querySelector('.stage-chip')?.textContent?.trim()).toBe('Stage not recorded');
    expect(page.querySelector('.verdict-card')?.getAttribute('data-band')).toBe('na');
    expect(page.textContent).toContain('The server recorded no graduation stage for this signal');
    expect(page.textContent).not.toContain('Stage 0 kill switch');
    expect(page.textContent).not.toContain('Rejected');
  });
});

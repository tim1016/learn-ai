import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { SampleDataBannerComponent } from './sample-data-banner.component';

describe('SampleDataBannerComponent', () => {
  it('says the data is not real when the dataset is a sample', async () => {
    await render(SampleDataBannerComponent, { inputs: { provenance: 'sample' } });

    expect(screen.getByRole('note').textContent).toContain('Sample data — not real');
  });

  it('draws nothing for real data', async () => {
    await render(SampleDataBannerComponent, { inputs: { provenance: 'real' } });

    expect(screen.queryByRole('note')).toBeNull();
  });
});

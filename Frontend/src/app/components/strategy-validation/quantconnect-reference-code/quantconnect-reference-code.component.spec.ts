import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { QuantConnectReferenceCodeComponent } from './quantconnect-reference-code.component';

const REFERENCE_CODE = {
  path: 'references/qc-shadow/SpyEmaCrossoverAlgorithm.py',
  sha256: 'cfc7f18877b8dcf9b99af4bb26e4f36f0b7ac6799fa5f4d6dc286945653d6078',
  language: 'python',
  source: 'class SpyEmaCrossoverAlgorithm(QCAlgorithm):\n    pass\n',
};

describe('QuantConnectReferenceCodeComponent', () => {
  it('copies the exact SHA-pinned audit copy for a QuantConnect backtest', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    await render(QuantConnectReferenceCodeComponent, {
      inputs: { referenceCode: REFERENCE_CODE },
    });

    expect(screen.getByRole('heading', { name: 'QuantConnect reference algorithm' })).toBeTruthy();
    expect(screen.getByText('references/qc-shadow/SpyEmaCrossoverAlgorithm.py')).toBeTruthy();
    expect(screen.getByText(REFERENCE_CODE.sha256)).toBeTruthy();

    const copyButton = screen.getByRole('button', { name: 'Copy QuantConnect algorithm' });
    fireEvent.click(copyButton);

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(REFERENCE_CODE.source);
      expect(copyButton.textContent).toContain('Copied');
    });
  });

  it('explains when the browser blocks clipboard access', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'));
    Object.assign(navigator, { clipboard: { writeText } });
    await render(QuantConnectReferenceCodeComponent, {
      inputs: { referenceCode: REFERENCE_CODE },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Copy QuantConnect algorithm' }));

    expect((await screen.findByRole('alert')).textContent).toContain('Copy was blocked');
  });
});

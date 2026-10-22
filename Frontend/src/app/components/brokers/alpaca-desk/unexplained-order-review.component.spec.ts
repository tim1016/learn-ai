import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
import { resourceTarget } from '../../../fleet/resource-target';
import { UnexplainedOrderReviewComponent } from './unexplained-order-review.component';

describe('UnexplainedOrderReviewComponent', () => {
  it.each(['unavailable', 'wrong account', 'wrong order', 'owned order', 'missing version'])(
    'offers no acknowledgement when evidence is %s', async (failure) => {
      const accountTransaction = failure === 'unavailable'
        ? vi.fn().mockRejectedValue(new Error('unavailable'))
        : vi.fn().mockResolvedValue({
          account_id: failure === 'wrong account' ? 'other-account' : 'PA1',
          external_order_id: failure === 'wrong order' ? 'other-order' : 'leg-1',
          transaction_origin: failure === 'owned order' ? 'strategy' : 'external',
          receipt: failure === 'missing version' ? {} : { latest_observation_sequence: 18 },
        });
      await render(UnexplainedOrderReviewComponent, {
        inputs: {
          accountId: 'PA1', brokerOrderId: 'leg-1',
          target: resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA1' }),
        },
        providers: [{ provide: BrokersService, useValue: { accountTransaction } }],
      });
      expect(await screen.findByText(/The recorded order is unavailable/)).toBeTruthy();
      expect(screen.queryByRole('button', { name: /Acknowledge/ })).toBeNull();
    },
  );
});

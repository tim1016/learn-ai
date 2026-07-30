import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { DeployBotBody } from '../lib/broker-v2-panel.service';
import { DeployDialogComponent } from './deploy-dialog.component';

function makeMockPanelService(deployResult: 'resolve' | 'reject' = 'resolve') {
  return {
    deployBot:
      deployResult === 'resolve'
        ? vi.fn().mockResolvedValue({})
        : vi.fn().mockRejectedValue(new Error('Deploy failed')),
  };
}

async function renderDialog(mockPanelService = makeMockPanelService()) {
  return render(DeployDialogComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: mockPanelService },
    ],
    componentInputs: {
      broker: 'alpaca',
      accountId: 'PA9',
      visible: true,
    },
  });
}

describe('DeployDialogComponent', () => {
  it('defaults to mode=log_only and shows "Log only" option', async () => {
    await renderDialog();

    expect(await screen.findByText(/Log only \(no orders\)/i)).toBeTruthy();
  });

  it('shows quantity-ignored hint when mode is log_only', async () => {
    await renderDialog();

    expect(await screen.findByText(/Quantity is ignored in log-only mode/i)).toBeTruthy();
  });

  it('hides quantity-ignored hint after mode switches to trade', async () => {
    const { fixture } = await renderDialog();
    const component = fixture.componentInstance as DeployDialogComponent;

    // Switch mode to trade via form control (mode selection)
    component['form'].controls.mode.setValue('trade');
    fixture.detectChanges();

    // The hint should no longer be visible
    expect(screen.queryByText(/Quantity is ignored in log-only mode/i)).toBeNull();
  });

  it('default payload has mode=log_only and quantity=1', async () => {
    const mockPanelService = makeMockPanelService();
    const { fixture } = await renderDialog(mockPanelService);
    const component = fixture.componentInstance as DeployDialogComponent;

    // Fill required fields and submit
    component['form'].controls.strategy_instance_id.setValue('spy-test-01');
    component['form'].controls.symbol.setValue('SPY');
    fixture.detectChanges();

    await component['submit']();
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(mockPanelService.deployBot).toHaveBeenCalled();
    const body = mockPanelService.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body.mode).toBe('log_only');
    expect(body.quantity).toBe(1);
  });

  it('submitted payload carries mode=trade and chosen quantity', async () => {
    const mockPanelService = makeMockPanelService();
    const { fixture } = await renderDialog(mockPanelService);
    const component = fixture.componentInstance as DeployDialogComponent;

    component['form'].controls.strategy_instance_id.setValue('spy-test-01');
    component['form'].controls.symbol.setValue('SPY');
    component['form'].controls.mode.setValue('trade');
    component['form'].controls.quantity.setValue(10);
    fixture.detectChanges();

    await component['submit']();
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(mockPanelService.deployBot).toHaveBeenCalled();
    const body = mockPanelService.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body.mode).toBe('trade');
    expect(body.quantity).toBe(10);
  });
});

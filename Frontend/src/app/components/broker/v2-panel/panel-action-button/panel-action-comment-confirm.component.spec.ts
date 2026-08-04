import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { PanelActionCommentConfirmComponent } from './panel-action-comment-confirm.component';

function baseInputs() {
  return {
    open: true,
    heading: 'Clear the account hold?',
    message: 'This clears the hold on acct-1.',
    consequence: 'Order submission resumes immediately.',
    confirmLabel: 'Clear hold',
  };
}

describe('PanelActionCommentConfirmComponent', () => {
  it('disables confirm until a reason is entered when no token is required', async () => {
    const confirmed = vi.fn();
    const view = await render(PanelActionCommentConfirmComponent, {
      inputs: baseInputs(),
    });
    view.fixture.componentInstance.confirmed.subscribe(confirmed);

    const confirm = screen.getByRole('button', {
      name: 'Clear hold',
      hidden: true,
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-reason'), {
      target: { value: 'Stale hold, broker confirms flat.' },
    });
    expect(confirm.disabled).toBe(false);

    fireEvent.click(confirm);
    expect(confirmed).toHaveBeenCalledWith({ reason: 'Stale hold, broker confirms flat.' });
  });

  it('does not render a token field when requiredToken is empty', async () => {
    await render(PanelActionCommentConfirmComponent, { inputs: baseInputs() });

    expect(screen.queryByTestId('panel-action-comment-confirm-token')).toBeNull();
  });

  it('gates confirm on BOTH a non-blank reason and the required token', async () => {
    const confirmed = vi.fn();
    const view = await render(PanelActionCommentConfirmComponent, {
      inputs: { ...baseInputs(), requiredToken: 'BASELINE' },
    });
    view.fixture.componentInstance.confirmed.subscribe(confirmed);

    const confirm = screen.getByRole('button', {
      name: 'Clear hold',
      hidden: true,
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-reason'), {
      target: { value: 'Adopting current broker inventory.' },
    });
    expect(confirm.disabled).toBe(true); // token still empty

    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-token'), {
      target: { value: 'wrong' },
    });
    expect(confirm.disabled).toBe(true); // wrong token

    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-token'), {
      target: { value: 'BASELINE' },
    });
    expect(confirm.disabled).toBe(false);

    fireEvent.click(confirm);
    expect(confirmed).toHaveBeenCalledWith({ reason: 'Adopting current broker inventory.' });
  });

  it('clears the reason and token every time the dialog re-opens, not just the first time', async () => {
    const view = await render(PanelActionCommentConfirmComponent, {
      inputs: { ...baseInputs(), requiredToken: 'BASELINE' },
    });

    const reason = screen.getByTestId(
      'panel-action-comment-confirm-reason',
    ) as HTMLTextAreaElement;
    const token = screen.getByTestId('panel-action-comment-confirm-token') as HTMLInputElement;
    fireEvent.input(reason, { target: { value: 'first reason' } });
    fireEvent.input(token, { target: { value: 'BASELINE' } });

    const confirm = screen.getByRole('button', {
      name: 'Clear hold',
      hidden: true,
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);

    // Close then re-open — a SECOND open, not the first — must still reset.
    view.fixture.componentRef.setInput('open', false);
    view.fixture.detectChanges();
    view.fixture.componentRef.setInput('open', true);
    view.fixture.detectChanges();

    expect(reason.value).toBe('');
    expect(token.value).toBe('');
    expect(confirm.disabled).toBe(true);
  });

  it('emits cancelled on cancel click', async () => {
    const cancelled = vi.fn();
    const view = await render(PanelActionCommentConfirmComponent, { inputs: baseInputs() });
    view.fixture.componentInstance.cancelled.subscribe(cancelled);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel', hidden: true }));
    expect(cancelled).toHaveBeenCalledTimes(1);
  });

  it('renders the backend-authored heading, message, and consequence verbatim', async () => {
    await render(PanelActionCommentConfirmComponent, { inputs: baseInputs() });

    expect(screen.getByText('Clear the account hold?')).toBeTruthy();
    expect(screen.getByText('This clears the hold on acct-1.')).toBeTruthy();
    expect(screen.getByText('Order submission resumes immediately.')).toBeTruthy();
  });
});

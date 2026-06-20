import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import { TypedHaltConfirmComponent } from './typed-halt-confirm.component';

interface Harness {
  el: HTMLElement;
  setOpen(open: boolean): void;
  type(value: string): void;
  confirmed: number;
  cancelled: number;
}

function render(opts: { open: boolean }): Harness {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(TypedHaltConfirmComponent);
  fixture.componentRef.setInput('open', opts.open);
  let confirmed = 0;
  let cancelled = 0;
  fixture.componentInstance.confirmed.subscribe(() => (confirmed += 1));
  fixture.componentInstance.cancelled.subscribe(() => (cancelled += 1));
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    setOpen(open) {
      fixture.componentRef.setInput('open', open);
      fixture.detectChanges();
    },
    type(value) {
      fixture.componentInstance.onTyped(value);
      fixture.detectChanges();
    },
    get confirmed() {
      return confirmed;
    },
    get cancelled() {
      return cancelled;
    },
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('TypedHaltConfirmComponent', () => {
  it('renders nothing when open is false', () => {
    const h = render({ open: false });
    expect(h.el.querySelector('[data-testid="typed-halt-confirm-dialog"]')).toBeNull();
  });

  it('renders the dialog when open is true', () => {
    const h = render({ open: true });
    expect(
      h.el.querySelector('[data-testid="typed-halt-confirm-dialog"]'),
    ).not.toBeNull();
  });

  it('disables the confirm button until the operator types HALT exactly', () => {
    const h = render({ open: true });
    const submit = (): HTMLButtonElement | null =>
      h.el.querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]');

    expect(submit()?.disabled).toBe(true);
    h.type('halt'); // case-sensitive
    expect(submit()?.disabled).toBe(true);
    h.type('HALT');
    expect(submit()?.disabled).toBe(false);
  });

  it('emits confirmed only after the token matches and the button is clicked', () => {
    const h = render({ open: true });
    h.type('WRONG');
    h.el
      .querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]')
      ?.click();
    expect(h.confirmed).toBe(0);

    h.type('HALT');
    h.el
      .querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]')
      ?.click();
    expect(h.confirmed).toBe(1);
  });

  it('emits cancelled when the cancel button is clicked', () => {
    const h = render({ open: true });
    h.el
      .querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-cancel"]')
      ?.click();
    expect(h.cancelled).toBe(1);
  });

  it('resets the typed field when the dialog re-opens', () => {
    const h = render({ open: true });
    h.type('HALT');
    h.setOpen(false);
    h.setOpen(true);
    // re-opened -> confirm should be disabled until re-typed
    expect(
      h.el.querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]')
        ?.disabled,
    ).toBe(true);
  });
});

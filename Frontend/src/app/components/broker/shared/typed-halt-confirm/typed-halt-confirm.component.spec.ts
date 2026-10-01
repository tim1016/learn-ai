import { ChangeDetectionStrategy, Component, provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { TypedHaltConfirmComponent } from './typed-halt-confirm.component';

interface Harness {
  el: HTMLElement;
  type(value: string): void;
  confirmed: number;
  cancelled: number;
}

function render(opts: {
  requiredToken?: string;
  confirmLabel?: string;
  heading?: string;
  message?: string;
  consequence?: string;
}): Harness {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(TypedHaltConfirmComponent);
  fixture.componentRef.setInput('heading', opts.heading ?? 'Backend title');
  fixture.componentRef.setInput('message', opts.message ?? 'Backend body.');
  fixture.componentRef.setInput('consequence', opts.consequence ?? 'Backend consequence.');
  fixture.componentRef.setInput('confirmLabel', opts.confirmLabel ?? 'Backend confirm');
  if (opts.requiredToken !== undefined) {
    fixture.componentRef.setInput('requiredToken', opts.requiredToken);
  }
  let confirmed = 0;
  let cancelled = 0;
  fixture.componentInstance.confirmed.subscribe(() => (confirmed += 1));
  fixture.componentInstance.cancelled.subscribe(() => (cancelled += 1));
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
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

  it('disables the confirm button until the operator types HALT exactly', () => {
    const h = render({});
    const submit = (): HTMLButtonElement | null =>
      h.el.querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]');

    expect(submit()?.disabled).toBe(true);
    h.type('halt'); // case-sensitive
    expect(submit()?.disabled).toBe(true);
    h.type('HALT');
    expect(submit()?.disabled).toBe(false);
  });

  it('emits confirmed only after the token matches and the button is clicked', () => {
    const h = render({});
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
    const h = render({});
    h.el
      .querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-cancel"]')
      ?.click();
    expect(h.cancelled).toBe(1);
  });

  describe('Escape', () => {
    interface EscapePress {
      /** `data-testid` of the element the keydown was dispatched from. */
      origin: string | null;
      /** Keys an enclosing element (as the bot banner's overflow menu would
       *  be) received. */
      ancestorSaw: string[];
    }

    /** Focuses the control and presses Escape there. Opening the dialog
     *  queues a microtask that moves focus to its first control, so that runs
     *  first; otherwise it would steal focus between `focus()` and the key. */
    async function pressEscapeOn(h: Harness, testId: string): Promise<EscapePress> {
      await Promise.resolve();
      const element = h.el.querySelector<HTMLElement>(`[data-testid="${testId}"]`);
      if (element === null) throw new Error(`${testId} not rendered`);
      const ancestor = h.el.parentElement;
      if (ancestor === null) throw new Error('component host is not attached');

      const press: EscapePress = { origin: null, ancestorSaw: [] };
      const recordOrigin = (event: Event): void => {
        press.origin = event.target instanceof HTMLElement ? event.target.dataset['testid'] ?? null : null;
      };
      const recordAncestor = (event: KeyboardEvent): void => {
        press.ancestorSaw.push(event.key);
      };
      document.addEventListener('keydown', recordOrigin, { capture: true });
      ancestor.addEventListener('keydown', recordAncestor);
      try {
        element.focus();
        expect(document.activeElement).toBe(element);
        await userEvent.keyboard('{Escape}');
      } finally {
        document.removeEventListener('keydown', recordOrigin, { capture: true });
        ancestor.removeEventListener('keydown', recordAncestor);
      }
      return press;
    }

    it('cancels once when pressed in the typed-confirm input', async () => {
      const h = render({});

      const press = await pressEscapeOn(h, 'typed-halt-confirm-input');

      expect(press.origin).toBe('typed-halt-confirm-input');
      expect(h.cancelled).toBe(1);
      expect(h.confirmed).toBe(0);
    });

    it('cancels when pressed on a dialog button, even the confirm button', async () => {
      const h = render({ requiredToken: '' });

      const press = await pressEscapeOn(h, 'typed-halt-confirm-submit');

      expect(press.origin).toBe('typed-halt-confirm-submit');
      expect(h.cancelled).toBe(1);
      expect(h.confirmed).toBe(0);
    });

    it('stays inside the dialog, so an enclosing menu that closes on Escape does not also act', async () => {
      const h = render({});

      const press = await pressEscapeOn(h, 'typed-halt-confirm-input');

      expect(press.origin).toBe('typed-halt-confirm-input');
      expect(h.cancelled).toBe(1);
      expect(press.ancestorSaw).toEqual([]);
    });

    it('cancels once from the backdrop, and an enclosing menu does not also act', async () => {
      // Shift+Tab from the token input lands on the backdrop button, which
      // sits outside the dialog element.
      const h = render({});

      const press = await pressEscapeOn(h, 'typed-halt-confirm-backdrop');

      expect(press.origin).toBe('typed-halt-confirm-backdrop');
      expect(h.cancelled).toBe(1);
      expect(press.ancestorSaw).toEqual([]);
    });

    it('still cancels from the document when focus is outside the dialog', () => {
      const h = render({});

      document.body.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      );

      expect(h.cancelled).toBe(1);
    });
  });

  describe('plain confirm mode (no required token)', () => {
    it('hides the token field and enables confirm immediately', () => {
      const h = render({ requiredToken: '' });
      expect(h.el.querySelector('[data-testid="typed-halt-confirm-input"]')).toBeNull();
      expect(
        h.el.querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]')
          ?.disabled,
      ).toBe(false);
    });

    it('emits confirmed on a single click with no typing', () => {
      const h = render({ requiredToken: '' });
      h.el
        .querySelector<HTMLButtonElement>('[data-testid="typed-halt-confirm-submit"]')
        ?.click();
      expect(h.confirmed).toBe(1);
    });

    it('moves keyboard focus into the dialog (onto Cancel) when there is no token input', async () => {
      const host = mountHost();
      host.open();
      await host.stable();

      expect(document.activeElement).toBe(host.query('typed-halt-confirm-cancel'));
      host.destroy();
    });

    it('hands the keyboard back to what opened it when cancelled and the host removes it', async () => {
      const host = mountHost();
      host.open();
      await host.stable();
      expect(document.activeElement).not.toBe(host.opener);

      host.query('typed-halt-confirm-cancel')?.click();
      await host.stable();

      expect(host.query('typed-halt-confirm-dialog')).toBeNull();
      expect(document.activeElement).toBe(host.opener);
      host.destroy();
    });

    it('leaves the keyboard to the host after a confirm', async () => {
      const host = mountHost();
      host.open();
      await host.stable();

      host.query('typed-halt-confirm-submit')?.click();
      await host.stable();

      expect(host.query('typed-halt-confirm-dialog')).toBeNull();
      expect(document.activeElement).not.toBe(host.opener);
      host.destroy();
    });
  });
});

/** The shape every real host has: the dialog is mounted under the host's own
 * `@if` and removed on either output. */
@Component({
  selector: 'app-typed-confirm-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TypedHaltConfirmComponent],
  template: `
    <button type="button" data-testid="opener" (click)="shown.set(true)">Flatten…</button>
    @if (shown()) {
      <app-typed-halt-confirm
        heading="Backend title"
        message="Backend body."
        consequence="Backend consequence."
        requiredToken=""
        confirmLabel="Backend confirm"
        (confirmed)="shown.set(false)"
        (cancelled)="shown.set(false)"
      />
    }
  `,
})
class TypedConfirmHostComponent {
  readonly shown = signal(false);
}

function mountHost(): {
  opener: HTMLButtonElement;
  open(): void;
  stable(): Promise<void>;
  query(testId: string): HTMLButtonElement | null;
  destroy(): void;
} {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
  const fixture = TestBed.createComponent(TypedConfirmHostComponent);
  const el = fixture.nativeElement as HTMLElement;
  document.body.appendChild(el);
  fixture.detectChanges();
  const query = (testId: string): HTMLButtonElement | null =>
    el.querySelector<HTMLButtonElement>(`[data-testid="${testId}"]`);
  const opener = query('opener');
  if (opener === null) throw new Error('opener not rendered');
  return {
    opener,
    open() {
      opener.focus();
      opener.click();
    },
    stable: () => fixture.whenStable(),
    query,
    destroy() {
      fixture.destroy();
      el.remove();
    },
  };
}

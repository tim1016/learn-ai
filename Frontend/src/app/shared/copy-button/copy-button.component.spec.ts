import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CopyButtonComponent } from './copy-button.component';

@Component({
  imports: [CopyButtonComponent],
  template: `<app-copy-button [text]="text" [variant]="variant" [label]="label" />`,
})
class Host {
  text = 'cd PythonDataService && run';
  variant: 'icon' | 'button' = 'icon';
  label = 'Copy';
}

function button(el: HTMLElement): HTMLButtonElement {
  const node = el.querySelector('button.copy-button');
  if (node === null) throw new Error('expected a copy button');
  return node as HTMLButtonElement;
}

describe('CopyButtonComponent', () => {
  // Mutate only `navigator.clipboard` (never the whole `navigator`) so we
  // don't strip `userAgent` — Angular Forms' DefaultValueAccessor reads
  // `navigator.userAgent.toLowerCase()`, and a replaced navigator would leak
  // to later test files in the same worker and crash them.
  const originalClipboard = navigator.clipboard;

  afterEach(() => {
    Object.assign(navigator, { clipboard: originalClipboard });
    vi.restoreAllMocks();
  });

  it('exposes an accessible name even in the icon variant', () => {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();

    expect(button(fixture.nativeElement).getAttribute('aria-label')).toBe('Copy');
  });

  it('copies the text and confirms on success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();

    button(fixture.nativeElement).click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(writeText).toHaveBeenCalledWith('cd PythonDataService && run');
    const btn = button(fixture.nativeElement);
    expect(btn.getAttribute('aria-label')).toBe('Copied');
    expect(btn.querySelector('.pi-check')).not.toBeNull();
  });

  it('surfaces a fallback message when the clipboard is unavailable', async () => {
    Object.assign(navigator, { clipboard: undefined });

    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();

    button(fixture.nativeElement).click();
    await fixture.whenStable();
    fixture.detectChanges();

    const alert = fixture.nativeElement.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(button(fixture.nativeElement).getAttribute('aria-label')).toBe('Copy');
  });

  it('renders the label text in the button variant', () => {
    const fixture = TestBed.createComponent(Host);
    fixture.componentInstance.variant = 'button';
    fixture.componentInstance.label = 'Copy command';
    fixture.detectChanges();

    expect(button(fixture.nativeElement).textContent?.trim()).toBe('Copy command');
  });
});

import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';
import { PageErrorComponent } from './page-error.component';
import { GraphqlError } from '../graphql/graphql-error';

describe('PageErrorComponent', () => {
  it('renders the catalog title when the error has a known code', () => {
    const fixture = TestBed.createComponent(PageErrorComponent);
    fixture.componentRef.setInput(
      'error',
      new GraphqlError([{ message: 'fail', extensions: { code: 'BROKER_DISCONNECTED' } }]),
    );
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.page-error-title')?.textContent ?? '').toContain('IB Gateway');
  });

  it('renders the eyebrow when one is supplied', () => {
    const fixture = TestBed.createComponent(PageErrorComponent);
    fixture.componentRef.setInput('error', new Error('boom'));
    fixture.componentRef.setInput('eyebrow', 'Page failed');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.page-error-eyebrow')?.textContent ?? '').toBe('Page failed');
  });

  it('renders the math-sources-of-truth link when extensions.mathRef is present', () => {
    const fixture = TestBed.createComponent(PageErrorComponent);
    fixture.componentRef.setInput(
      'error',
      new GraphqlError([
        {
          message: 'greeks',
          extensions: { code: 'NUMERIC_DIVERGENCE', mathRef: '/docs/math.md' },
        },
      ]),
    );
    fixture.detectChanges();
    const a = (fixture.nativeElement as HTMLElement).querySelector(
      '.page-error-mathref',
    ) as HTMLAnchorElement;
    expect(a.getAttribute('href')).toBe('/docs/math.md');
  });

  it('emits retry when the retry button is clicked', () => {
    const fixture = TestBed.createComponent(PageErrorComponent);
    fixture.componentRef.setInput('error', new Error('boom'));
    fixture.detectChanges();
    const handler = vi.fn();
    fixture.componentInstance.retry.subscribe(handler);
    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      '.page-error-retry',
    ) as HTMLButtonElement;
    btn.click();
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

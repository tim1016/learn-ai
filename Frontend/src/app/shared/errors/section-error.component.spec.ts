import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';
import { SectionErrorComponent } from './section-error.component';
import { GraphqlError } from '../graphql/graphql-error';

function createFixture() {
  return TestBed.createComponent(SectionErrorComponent);
}

describe('SectionErrorComponent', () => {
  it('renders nothing when no error is supplied', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', null);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.section-error')).toBeNull();
  });

  it('renders catalog copy when the error carries a known code', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput(
      'error',
      new GraphqlError([{ message: 'gw down', extensions: { code: 'BROKER_DISCONNECTED' } }]),
    );
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const what = el.querySelector('.section-error-what');
    const tryNode = el.querySelector('.section-error-try');
    expect(what?.textContent ?? '').toContain('IB Gateway');
    expect(tryNode?.textContent ?? '').toContain('Retry');
  });

  it('emits retry when the button is clicked', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', new Error('nope'));
    fixture.detectChanges();
    const handler = vi.fn();
    fixture.componentInstance.retry.subscribe(handler);
    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      '.section-error-retry',
    ) as HTMLButtonElement;
    btn.click();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('hides retry button when canRetry=false', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', new Error('nope'));
    fixture.componentRef.setInput('canRetry', false);
    fixture.detectChanges();
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('.section-error-retry'),
    ).toBeNull();
  });

  it('disables retry while retrying=true', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', new Error('nope'));
    fixture.componentRef.setInput('retrying', true);
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector(
      '.section-error-retry',
    ) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.textContent?.trim()).toBe('Retrying…');
  });

  it('renders the technical-details drawer with the underlying payload', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput(
      'error',
      new GraphqlError([{ message: 'boom', extensions: { code: 'BROKER_DISCONNECTED' } }]),
    );
    fixture.detectChanges();
    const pre = (fixture.nativeElement as HTMLElement).querySelector(
      '.section-error-details pre',
    ) as HTMLElement;
    expect(pre.textContent).toContain('BROKER_DISCONNECTED');
    expect(pre.textContent).toContain('"message": "boom"');
  });
});

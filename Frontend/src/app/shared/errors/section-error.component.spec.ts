import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';
import { SectionErrorComponent } from './section-error.component';

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

  it('echoes the error message as what failed, with the generic next step', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', new Error('Network broken'));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.section-error-what')?.textContent).toBe('Network broken');
    expect(el.querySelector('.section-error-try')?.textContent).toContain('Try the action again.');
  });

  it('echoes a thrown string as what failed', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', 'stream closed');
    fixture.detectChanges();
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('.section-error-what')?.textContent,
    ).toBe('stream closed');
  });

  it('prefers an explicit contextWhat over the raw message', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', new Error('ECONNREFUSED'));
    fixture.componentRef.setInput('contextWhat', 'We could not reach the broker.');
    fixture.detectChanges();
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('.section-error-what')?.textContent,
    ).toBe('We could not reach the broker.');
  });

  it('renders the technical-details drawer with the stack of a thrown Error', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', new Error('boom'));
    fixture.detectChanges();
    const pre = (fixture.nativeElement as HTMLElement).querySelector(
      '.section-error-details pre',
    ) as HTMLElement;
    expect(pre.textContent).toContain('boom');
  });

  it('renders the technical-details drawer with a thrown payload as JSON', () => {
    const fixture = createFixture();
    fixture.componentRef.setInput('error', { code: 'BROKER_DISCONNECTED', message: 'boom' });
    fixture.detectChanges();
    const pre = (fixture.nativeElement as HTMLElement).querySelector(
      '.section-error-details pre',
    ) as HTMLElement;
    expect(pre.textContent).toContain('"code": "BROKER_DISCONNECTED"');
    expect(pre.textContent).toContain('"message": "boom"');
  });
});

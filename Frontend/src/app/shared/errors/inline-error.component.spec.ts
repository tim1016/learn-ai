import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import { InlineErrorComponent } from './inline-error.component';
import { GraphqlError } from '../graphql/graphql-error';

describe('InlineErrorComponent', () => {
  it('renders nothing when no error or message is supplied', () => {
    const fixture = TestBed.createComponent(InlineErrorComponent);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.inline-error')).toBeNull();
  });

  it('prefers an explicit message over the error', () => {
    const fixture = TestBed.createComponent(InlineErrorComponent);
    fixture.componentRef.setInput('error', new Error('verbose internal'));
    fixture.componentRef.setInput('message', 'Strike must be greater than zero.');
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement)
      .querySelector('.inline-error-text')?.textContent ?? '';
    expect(text).toBe('Strike must be greater than zero.');
  });

  it('falls back to catalog copy when the error has a known code', () => {
    const fixture = TestBed.createComponent(InlineErrorComponent);
    fixture.componentRef.setInput(
      'error',
      new GraphqlError([{ message: 'x', extensions: { code: 'BROKER_DISCONNECTED' } }]),
    );
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement)
      .querySelector('.inline-error-text')?.textContent ?? '';
    expect(text).toContain('IB Gateway');
  });
});

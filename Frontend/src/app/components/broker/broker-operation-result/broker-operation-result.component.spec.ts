import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { BrokerOperationResultComponent } from './broker-operation-result.component';
import type { OperationError } from '../operation-error';

const ERR: OperationError = {
  category: 'precondition',
  title: 'Deploy — blocked',
  detail: 'Working tree is dirty at PythonDataService',
  remediation: 'Commit or stash the listed paths, then deploy again.',
  status: 409,
};

function render(error: OperationError | null) {
  const fixture = TestBed.createComponent(BrokerOperationResultComponent);
  fixture.componentRef.setInput('error', error);
  fixture.detectChanges();
  return fixture;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerOperationResultComponent', () => {
  it('renders title, detail and remediation as an alert', () => {
    const fixture = render(ERR);
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('[role="alert"]')).toBeTruthy();
    expect(el.textContent).toContain('Deploy — blocked');
    expect(el.textContent).toContain('Working tree is dirty');
    expect(el.textContent).toContain('Commit or stash');
  });

  it('separates the blocked title from the bot-scoped detail in alert text', () => {
    const fixture = render({
      ...ERR,
      detail: 'deiagAPPL6 is durably STOPPED. Resume the bot to clear the stop latch.',
    });
    const alert = (fixture.nativeElement as HTMLElement).querySelector<HTMLElement>('[role="alert"]');
    const announcedCopy = alert?.querySelector<HTMLElement>('.sr-only');

    expect(alert?.textContent).toContain('Deploy — blocked deiagAPPL6');
    expect(announcedCopy?.textContent).toContain('Deploy — blocked deiagAPPL6');
    expect(alert?.textContent).not.toContain('Deploy — blockeddeiagAPPL6');
  });

  it('renders nothing when error is null', () => {
    const fixture = render(null);
    expect((fixture.nativeElement as HTMLElement).querySelector('.op-result')).toBeNull();
  });

  it('omits the detail line when detail is empty', () => {
    const fixture = render({ ...ERR, detail: '' });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.op-detail')).toBeNull();
    expect(el.textContent).toContain('Commit or stash');
  });
});

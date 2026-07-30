import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, expect, it, beforeEach } from 'vitest';
import { MarkdownDrawerHostComponent } from '../../../../shared/markdown-drawer/markdown-drawer-host.component';
import { MarkdownDrawerService } from '../../../../shared/markdown-drawer/markdown-drawer.service';

/**
 * Tests the shell-level markdown drawer host wired to the broker-v2 manual.
 *
 * Migrated from the former BrokerV2HelpDrawerComponent tests.  The host
 * component replaced the per-document drawer; `BrokerV2HelpDrawerService` is
 * now a façade over `MarkdownDrawerService`.
 */
function setup() {
  const svc = new MarkdownDrawerService();
  TestBed.configureTestingModule({
    imports: [MarkdownDrawerHostComponent],
    providers: [provideRouter([]), { provide: MarkdownDrawerService, useValue: svc }],
  });
  const fixture = TestBed.createComponent(MarkdownDrawerHostComponent);
  fixture.detectChanges();
  return { fixture, svc };
}

describe('MarkdownDrawerHostComponent (broker-v2 manual)', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  it('drawer is not visible initially', () => {
    const { svc } = setup();
    expect(svc.visible()).toBe(false);
  });

  it('after service.open with broker-v2-manual and anchor, drawer becomes visible', () => {
    const { fixture, svc } = setup();

    svc.open('broker-v2-manual', 'station-1-signal');
    fixture.detectChanges();

    expect(svc.visible()).toBe(true);
    expect(svc.anchor()).toBe('station-1-signal');
    expect(svc.activeDocId()).toBe('broker-v2-manual');
  });

  it('close button hides the drawer', () => {
    const { fixture, svc } = setup();

    svc.open('broker-v2-manual', 'station-2-intent');
    fixture.detectChanges();

    const closeButton = fixture.nativeElement.querySelector(
      'button[aria-label="Close documentation drawer"]',
    ) as HTMLButtonElement | null;
    closeButton?.click();
    fixture.detectChanges();

    expect(svc.visible()).toBe(false);
  });

  it('opening methodology after broker-v2 switches activeDocId', () => {
    const { fixture, svc } = setup();

    svc.open('broker-v2-manual', 'station-1-signal');
    fixture.detectChanges();
    expect(svc.activeDocId()).toBe('broker-v2-manual');

    svc.open('methodology', 'intro');
    fixture.detectChanges();
    expect(svc.activeDocId()).toBe('methodology');
    expect(svc.visible()).toBe(true);
  });

  it('the component does not navigate away when the drawer is open', () => {
    const { fixture, svc } = setup();

    const initialUrl = window.location.href;
    svc.open('broker-v2-manual', 'glossary');
    fixture.detectChanges();

    expect(window.location.href).toBe(initialUrl);
  });
});

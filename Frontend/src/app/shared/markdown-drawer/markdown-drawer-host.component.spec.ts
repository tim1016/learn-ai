import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, expect, it, beforeEach } from 'vitest';
import { MarkdownDrawerHostComponent } from './markdown-drawer-host.component';
import { MarkdownDrawerService } from './markdown-drawer.service';

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

describe('MarkdownDrawerHostComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  it('renders nothing when no document is open', () => {
    const { fixture } = setup();
    const drawer = fixture.nativeElement.querySelector('p-drawer');
    // The @if(doc()) guard means no drawer is rendered until open() is called.
    expect(drawer).toBeNull();
  });

  it('is not visible initially', () => {
    const { svc } = setup();
    expect(svc.visible()).toBe(false);
    expect(svc.activeDocId()).toBeNull();
  });

  it('opens the methodology document', () => {
    const { fixture, svc } = setup();
    svc.open('methodology', 'intro');
    fixture.detectChanges();

    expect(svc.visible()).toBe(true);
    expect(svc.activeDocId()).toBe('methodology');
    expect(svc.anchor()).toBe('intro');
  });

  it('opens the broker-v2-manual document', () => {
    const { fixture, svc } = setup();
    svc.open('broker-v2-manual', 'station-1-signal');
    fixture.detectChanges();

    expect(svc.visible()).toBe(true);
    expect(svc.activeDocId()).toBe('broker-v2-manual');
    expect(svc.anchor()).toBe('station-1-signal');
  });

  it('switching documents closes the previous one', () => {
    const { fixture, svc } = setup();

    svc.open('methodology');
    fixture.detectChanges();
    expect(svc.activeDocId()).toBe('methodology');

    svc.open('broker-v2-manual', 'glossary');
    fixture.detectChanges();
    expect(svc.activeDocId()).toBe('broker-v2-manual');
    expect(svc.visible()).toBe(true);
  });

  it('close sets visible to false', () => {
    const { fixture, svc } = setup();
    svc.open('broker-v2-manual');
    fixture.detectChanges();
    expect(svc.visible()).toBe(true);

    svc.close();
    fixture.detectChanges();
    expect(svc.visible()).toBe(false);
  });
});

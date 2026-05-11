import { TestBed } from '@angular/core/testing';
import { IdeSandboxComponent } from './ide-sandbox.component';

describe('IdeSandboxComponent', () => {
  it('renders the three rail elements that drive the .ide-grid primitive', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [IdeSandboxComponent] });
    const fixture = TestBed.createComponent(IdeSandboxComponent);
    fixture.detectChanges();

    const host: HTMLElement = fixture.nativeElement;

    expect(host.querySelector('[data-testid="ide-grid"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="ide-rail-left"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="ide-main"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="ide-rail-right"]')).not.toBeNull();
  });

  it('seeds enough tiles per rail to force overflow for sticky/scroll verification', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [IdeSandboxComponent] });
    const fixture = TestBed.createComponent(IdeSandboxComponent);
    fixture.detectChanges();

    const host: HTMLElement = fixture.nativeElement;
    const leftTiles = host.querySelectorAll('[data-testid="ide-rail-left"] .tile');
    expect(leftTiles.length).toBeGreaterThanOrEqual(10);
  });
});

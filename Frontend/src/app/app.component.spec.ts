import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterModule } from '@angular/router';
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async';
import { AppComponent } from './app.component';

describe('AppComponent', () => {
  let fixture: ComponentFixture<AppComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [AppComponent, RouterModule.forRoot([])],
      providers: [provideAnimationsAsync()],
    }).compileComponents();
    fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render PrimeNG menubar', () => {
    const menubar = fixture.nativeElement.querySelector('p-menubar');
    expect(menubar).toBeTruthy();
  });

  it('should have 3 top-level menu items (Stocks, Options, Tracked Instruments)', () => {
    const items = fixture.componentInstance.items();
    expect(items.length).toBe(3);
    expect(items[0].label).toBe('Stocks');
    expect(items[1].label).toBe('Options');
    expect(items[2].label).toBe('Tracked Instruments');
  });

  it('should have 4 sub-items under Stocks', () => {
    const stockItems = fixture.componentInstance.items()[0].items!;
    expect(stockItems.length).toBe(5);
  });

  it('should have 4 sub-items under Options', () => {
    const optionItems = fixture.componentInstance.items()[1].items!;
    expect(optionItems.length).toBe(4);
  });

  it('should contain a router-outlet', () => {
    expect(fixture.nativeElement.querySelector('router-outlet')).toBeTruthy();
  });
});

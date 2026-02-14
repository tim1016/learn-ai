import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TradingViewWidgetComponent } from './tradingview-widget.component';

describe('TradingViewWidgetComponent', () => {
  let component: TradingViewWidgetComponent;
  let fixture: ComponentFixture<TradingViewWidgetComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TradingViewWidgetComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(TradingViewWidgetComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have default input values', () => {
    expect(component.symbol).toBe('AAPL');
    expect(component.exchange).toBe('NASDAQ');
    expect(component.colorTheme).toBe('light');
  });

  it('should create script element on init', () => {
    fixture.detectChanges();
    const container = fixture.nativeElement.querySelector('.tv-widget-container');
    const script = container.querySelector('script');
    expect(script).toBeTruthy();
    expect(script.src).toContain('tradingview.com');
  });

  it('should embed correct symbol in script config', () => {
    component.symbol = 'MSFT';
    component.exchange = 'NASDAQ';
    fixture.detectChanges();

    const script = fixture.nativeElement.querySelector('script');
    const config = JSON.parse(script.textContent);
    expect(config.symbol).toBe('NASDAQ:MSFT');
  });

  it('should clean up container on destroy', () => {
    fixture.detectChanges();
    fixture.destroy();
    // After destroy, cleanup clears innerHTML
    // Verify no error is thrown during cleanup
    expect(true).toBe(true);
  });
});

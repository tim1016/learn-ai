import {
  Component, Input, ElementRef, ViewChild,
  AfterViewInit, OnDestroy, OnChanges, SimpleChanges
} from '@angular/core';

@Component({
  selector: 'app-tradingview-widget',
  standalone: true,
  template: `<div #container class="tv-widget-container"></div>`,
  styles: [`
    .tv-widget-container {
      width: 100%;
      min-height: 180px;
    }
  `]
})
export class TradingViewWidgetComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() symbol = 'AAPL';
  @Input() exchange = 'NASDAQ';
  @Input() width = '100%';
  @Input() colorTheme: 'light' | 'dark' = 'light';

  @ViewChild('container') container!: ElementRef<HTMLDivElement>;

  private scriptElement: HTMLScriptElement | null = null;

  ngAfterViewInit(): void {
    this.loadWidget();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if ((changes['symbol'] || changes['exchange']) && this.container) {
      this.loadWidget();
    }
  }

  ngOnDestroy(): void {
    this.cleanup();
  }

  private loadWidget(): void {
    this.cleanup();

    const el = this.container.nativeElement;

    const widgetDiv = document.createElement('div');
    widgetDiv.className = 'tradingview-widget-container__widget';
    el.appendChild(widgetDiv);

    this.scriptElement = document.createElement('script');
    this.scriptElement.type = 'text/javascript';
    this.scriptElement.src = 'https://s3.tradingview.com/external-embedding/embed-widget-symbol-info.js';
    this.scriptElement.async = true;
    this.scriptElement.textContent = JSON.stringify({
      symbol: `${this.exchange}:${this.symbol}`,
      width: this.width,
      locale: 'en',
      colorTheme: this.colorTheme,
      isTransparent: false
    });

    el.appendChild(this.scriptElement);
  }

  private cleanup(): void {
    if (this.container) {
      this.container.nativeElement.innerHTML = '';
    }
    this.scriptElement = null;
  }
}

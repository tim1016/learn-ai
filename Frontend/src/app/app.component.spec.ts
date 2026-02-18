import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterModule } from '@angular/router';
import { AppComponent } from './app.component';

describe('AppComponent', () => {
  let fixture: ComponentFixture<AppComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [AppComponent, RouterModule.forRoot([])],
    }).compileComponents();
    fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render 9 navigation links', () => {
    const links = fixture.nativeElement.querySelectorAll('nav a');
    expect(links.length).toBe(9);
  });

  it('should have links to all routes', () => {
    const links: HTMLAnchorElement[] = Array.from(fixture.nativeElement.querySelectorAll('nav a'));
    const hrefs = links.map(a => a.getAttribute('routerLink'));
    expect(hrefs).toContain('/books');
    expect(hrefs).toContain('/authors');
    expect(hrefs).toContain('/market-data');
    expect(hrefs).toContain('/tickers');
    expect(hrefs).toContain('/technical-analysis');
    expect(hrefs).toContain('/stock-analysis');
    expect(hrefs).toContain('/ticker-explorer');
    expect(hrefs).toContain('/strategy-lab');
    expect(hrefs).toContain('/options-history');
  });

  it('should contain a router-outlet', () => {
    expect(fixture.nativeElement.querySelector('router-outlet')).toBeTruthy();
  });
});

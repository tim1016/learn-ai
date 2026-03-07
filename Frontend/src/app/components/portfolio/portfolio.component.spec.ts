import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { PortfolioComponent } from './portfolio.component';
import { environment } from '../../../environments/environment';
import { Account } from '../../graphql/portfolio-types';

const GRAPHQL_URL = environment.backendUrl;

const mockAccounts: Account[] = [
  { id: 'acc-1', name: 'Paper Trading', type: 'Paper', baseCurrency: 'USD', initialCash: 100000, cash: 95000, createdAt: '2026-01-01' },
  { id: 'acc-2', name: 'Live Account', type: 'Live', baseCurrency: 'USD', initialCash: 50000, cash: 52000, createdAt: '2026-02-01' },
];

describe('PortfolioComponent', () => {
  let component: PortfolioComponent;
  let fixture: ComponentFixture<PortfolioComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [PortfolioComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(PortfolioComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    // Flush any outstanding child component requests before verifying
    const pending = httpMock.match(GRAPHQL_URL);
    pending.forEach(r => r.flush({ data: {} }));
    httpMock.verify();
  });

  function flushAccountsRequest(accounts: Account[] = mockAccounts): void {
    const req = httpMock.expectOne(GRAPHQL_URL);
    expect(req.request.body.query).toContain('getAccounts');
    req.flush({ data: { getAccounts: accounts } });
  }

  function flushAllPending(): void {
    // When an account is selected, child components fire requests. Flush them all.
    const pending = httpMock.match(GRAPHQL_URL);
    pending.forEach(r => r.flush({ data: {} }));
  }

  it('should create', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();
    expect(component).toBeTruthy();
  });

  it('should load accounts on init', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();

    expect(component.accounts().length).toBe(2);
    expect(component.loadingAccounts()).toBe(false);
  });

  it('should auto-select first account on load', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();

    expect(component.selectedAccountId()).toBe('acc-1');
  });

  it('should render account options in the selector', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const options = el.querySelectorAll('.account-selector select option');
    expect(options.length).toBe(2);
    expect(options[0].textContent).toContain('Paper Trading');
    expect(options[1].textContent).toContain('Live Account');
  });

  it('should toggle create form when button clicked', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const newBtn = el.querySelector('.account-selector button') as HTMLButtonElement;

    expect(component.showCreateForm()).toBe(false);
    expect(el.querySelector('.create-form')).toBeNull();

    newBtn.click();
    fixture.detectChanges();

    expect(component.showCreateForm()).toBe(true);
    expect(el.querySelector('.create-form')).not.toBeNull();

    newBtn.click();
    fixture.detectChanges();

    expect(component.showCreateForm()).toBe(false);
    expect(el.querySelector('.create-form')).toBeNull();
  });

  it('should show button text "Cancel" when create form is open', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const newBtn = el.querySelector('.account-selector button') as HTMLButtonElement;

    expect(newBtn.textContent?.trim()).toContain('+ New Account');

    newBtn.click();
    fixture.detectChanges();

    expect(newBtn.textContent?.trim()).toBe('Cancel');
  });

  it('should call createAccount mutation and add to list on success', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();

    component.showCreateForm.set(true);
    component.newAccountName.set('Backtest Account');
    component.newAccountType.set('Backtest');
    component.newAccountCash.set(25000);

    component.createAccount();

    const req = httpMock.expectOne(GRAPHQL_URL);
    expect(req.request.body.query).toContain('createAccount');
    expect(req.request.body.variables).toEqual({
      name: 'Backtest Account',
      type: 'Backtest',
      initialCash: 25000,
    });

    const newAccount: Account = {
      id: 'acc-3', name: 'Backtest Account', type: 'Backtest',
      baseCurrency: 'USD', initialCash: 25000, cash: 25000, createdAt: '2026-03-06',
    };
    req.flush({
      data: {
        createAccount: { success: true, error: null, account: newAccount },
      },
    });
    // Flush any requests from child components triggered by the new account selection
    flushAllPending();

    expect(component.accounts().length).toBe(3);
    expect(component.selectedAccountId()).toBe('acc-3');
    expect(component.showCreateForm()).toBe(false);
  });

  it('should not call createAccount when name is empty', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();

    component.newAccountName.set('   ');
    component.createAccount();

    httpMock.expectNone(GRAPHQL_URL);
  });

  it('should display error when createAccount returns error', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();

    component.newAccountName.set('Duplicate');
    component.createAccount();

    const req = httpMock.expectOne(GRAPHQL_URL);
    req.flush({
      data: {
        createAccount: { success: false, error: 'Duplicate name', account: null },
      },
    });

    expect(component.error()).toBe('Duplicate name');
  });

  it('should render tabs when account is selected', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const tabs = el.querySelectorAll('p-tab');
    expect(tabs.length).toBe(7);
  });

  it('should show empty state when no accounts', () => {
    fixture.detectChanges();
    flushAccountsRequest([]);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const emptyState = el.querySelector('.empty-state');
    expect(emptyState).not.toBeNull();
    expect(emptyState?.textContent).toContain('No accounts found');
  });

  it('should show loading text while accounts load', () => {
    component.loadingAccounts.set(true);
    fixture.detectChanges();
    // Don't trigger ngOnInit loading since we manually set state
    const pending = httpMock.match(GRAPHQL_URL);
    pending.forEach(r => r.flush({ data: { getAccounts: [] } }));

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Loading accounts...');
  });

  it('should display error banner and dismiss it', () => {
    fixture.detectChanges();
    flushAccountsRequest();
    flushAllPending();

    component.error.set('Something broke');
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const banner = el.querySelector('.error-banner');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Something broke');

    const dismissBtn = banner?.querySelector('button') as HTMLButtonElement;
    dismissBtn.click();
    fixture.detectChanges();

    expect(component.error()).toBeNull();
    expect(el.querySelector('.error-banner')).toBeNull();
  });

  it('should handle GraphQL error on loadAccounts', () => {
    fixture.detectChanges();

    const req = httpMock.expectOne(GRAPHQL_URL);
    req.flush({ data: null, errors: [{ message: 'Network error' }] });

    expect(component.error()).toBe('Network error');
    expect(component.accounts()).toEqual([]);
    expect(component.loadingAccounts()).toBe(false);
  });

  it('should handle HTTP error on loadAccounts', () => {
    fixture.detectChanges();

    const req = httpMock.expectOne(GRAPHQL_URL);
    req.error(new ProgressEvent('error'), { status: 500, statusText: 'Server Error' });

    expect(component.error()).toBeTruthy();
    expect(component.accounts()).toEqual([]);
    expect(component.loadingAccounts()).toBe(false);
  });
});

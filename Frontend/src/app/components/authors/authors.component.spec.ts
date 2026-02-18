import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AuthorsComponent } from './authors.component';
import { AuthorService } from '../../services/author.service';

describe('AuthorsComponent', () => {
  let component: AuthorsComponent;
  let fixture: ComponentFixture<AuthorsComponent>;
  let authorServiceMock: { getAuthors: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    authorServiceMock = { getAuthors: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [AuthorsComponent],
      providers: [{ provide: AuthorService, useValue: authorServiceMock }],
    }).compileComponents();

    fixture = TestBed.createComponent(AuthorsComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    authorServiceMock.getAuthors.mockReturnValue(of([]));
    expect(component).toBeTruthy();
  });

  it('should start in loading state', () => {
    authorServiceMock.getAuthors.mockReturnValue(of([]));
    expect(component.loading).toBe(true);
  });

  it('should load and display authors', () => {
    const mockAuthors = [
      { id: 1, name: 'George Orwell', bio: 'English novelist', books: [] },
      { id: 2, name: 'Jane Austen', bio: 'English novelist', books: [] },
    ];
    authorServiceMock.getAuthors.mockReturnValue(of(mockAuthors));

    fixture.detectChanges(); // triggers ngOnInit

    expect(component.loading).toBe(false);
    expect(component.authors.length).toBe(2);
    expect(component.authors[0].name).toBe('George Orwell');
  });

  it('should render author cards in template', () => {
    authorServiceMock.getAuthors.mockReturnValue(of([
      { id: 1, name: 'Test Author', books: [] },
    ]));
    fixture.detectChanges();

    const cards = fixture.nativeElement.querySelectorAll('.card');
    expect(cards.length).toBe(1);
    expect(cards[0].textContent).toContain('Test Author');
  });

  it('should handle error from service', () => {
    authorServiceMock.getAuthors.mockReturnValue(throwError(() => new Error('Network error')));

    fixture.detectChanges();

    expect(component.loading).toBe(false);
    expect(component.error).toBe('Failed to load authors. Is the backend running?');
  });
});

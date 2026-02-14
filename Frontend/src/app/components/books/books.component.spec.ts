import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { BooksComponent } from './books.component';
import { BookService } from '../../services/book.service';

describe('BooksComponent', () => {
  let component: BooksComponent;
  let fixture: ComponentFixture<BooksComponent>;
  let bookServiceMock: jest.Mocked<Pick<BookService, 'getBooks'>>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    bookServiceMock = { getBooks: jest.fn() };

    await TestBed.configureTestingModule({
      imports: [BooksComponent],
      providers: [{ provide: BookService, useValue: bookServiceMock }],
    }).compileComponents();

    fixture = TestBed.createComponent(BooksComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    bookServiceMock.getBooks.mockReturnValue(of([]));
    expect(component).toBeTruthy();
  });

  it('should start in loading state', () => {
    bookServiceMock.getBooks.mockReturnValue(of([]));
    expect(component.loading).toBe(true);
  });

  it('should load and display books', () => {
    const mockBooks = [
      { id: 1, title: '1984', publishedYear: 1949, authorId: 1 },
      { id: 2, title: 'Animal Farm', publishedYear: 1945, authorId: 1 },
    ];
    bookServiceMock.getBooks.mockReturnValue(of(mockBooks));

    fixture.detectChanges();

    expect(component.loading).toBe(false);
    expect(component.books.length).toBe(2);
  });

  it('should render book cards in template', () => {
    bookServiceMock.getBooks.mockReturnValue(of([
      { id: 1, title: 'Test Book', publishedYear: 2026, authorId: 1 },
    ]));
    fixture.detectChanges();

    const cards = fixture.nativeElement.querySelectorAll('.card');
    expect(cards.length).toBe(1);
    expect(cards[0].textContent).toContain('Test Book');
  });

  it('should handle error from service', () => {
    bookServiceMock.getBooks.mockReturnValue(throwError(() => new Error('Network error')));

    fixture.detectChanges();

    expect(component.loading).toBe(false);
    expect(component.error).toBe('Failed to load books. Is the backend running?');
  });
});

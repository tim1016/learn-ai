import { TestBed } from '@angular/core/testing';
import { ApolloTestingModule, ApolloTestingController } from 'apollo-angular/testing';
import { take } from 'rxjs/operators';
import { BookService } from './book.service';
import { GET_BOOKS } from '../graphql/queries';

describe('BookService', () => {
  let service: BookService;
  let apolloController: ApolloTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ApolloTestingModule],
    });
    service = TestBed.inject(BookService);
    apolloController = TestBed.inject(ApolloTestingController);
  });

  afterEach(() => apolloController.verify());

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should fetch books via Apollo', (done) => {
    const mockBooks = [
      { id: 1, title: '1984', publishedYear: 1949, authorId: 1, author: { id: 1, name: 'Orwell' } },
    ];

    service.getBooks().subscribe(books => {
      if (books.length === 0) return; // skip initial empty emission
      expect(books.length).toBe(1);
      expect(books[0].title).toBe('1984');
      done();
    });

    const op = apolloController.expectOne(GET_BOOKS);
    op.flush({ data: { books: mockBooks } });
  });

  it('should return empty array when no books', (done) => {
    service.getBooks().pipe(take(1)).subscribe(books => {
      expect(books).toEqual([]);
      done();
    });

    apolloController.expectOne(GET_BOOKS).flush({ data: { books: [] } });
  });
});

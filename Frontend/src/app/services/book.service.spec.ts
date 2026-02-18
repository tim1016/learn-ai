import { TestBed } from '@angular/core/testing';
import { ApolloTestingModule, ApolloTestingController } from 'apollo-angular/testing';
import { firstValueFrom, filter } from 'rxjs';
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

  it('should fetch books via Apollo', async () => {
    const mockBooks = [
      { id: 1, title: '1984', publishedYear: 1949, authorId: 1, author: { id: 1, name: 'Orwell' } },
    ];

    const promise = firstValueFrom(service.getBooks().pipe(filter(b => b.length > 0)));

    const op = apolloController.expectOne(GET_BOOKS);
    op.flush({ data: { books: mockBooks } });

    const books = await promise;
    expect(books.length).toBe(1);
    expect(books[0].title).toBe('1984');
  });

  it('should return empty array when no books', async () => {
    const promise = firstValueFrom(service.getBooks());

    apolloController.expectOne(GET_BOOKS).flush({ data: { books: [] } });

    const books = await promise;
    expect(books).toEqual([]);
  });
});

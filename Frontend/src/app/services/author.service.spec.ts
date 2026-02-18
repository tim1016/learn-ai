import { TestBed } from '@angular/core/testing';
import { ApolloTestingModule, ApolloTestingController } from 'apollo-angular/testing';
import { firstValueFrom, filter } from 'rxjs';
import { AuthorService } from './author.service';
import { GET_AUTHORS } from '../graphql/queries';

describe('AuthorService', () => {
  let service: AuthorService;
  let apolloController: ApolloTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ApolloTestingModule],
    });
    service = TestBed.inject(AuthorService);
    apolloController = TestBed.inject(ApolloTestingController);
  });

  afterEach(() => apolloController.verify());

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should fetch authors via Apollo', async () => {
    const mockAuthors = [
      { id: 1, name: 'George Orwell', bio: 'English novelist', books: [] },
    ];

    const promise = firstValueFrom(service.getAuthors().pipe(filter(a => a.length > 0)));

    const op = apolloController.expectOne(GET_AUTHORS);
    op.flush({ data: { authors: mockAuthors } });

    const authors = await promise;
    expect(authors.length).toBe(1);
    expect(authors[0].name).toBe('George Orwell');
  });

  it('should return empty array when no authors', async () => {
    const promise = firstValueFrom(service.getAuthors());

    apolloController.expectOne(GET_AUTHORS).flush({ data: { authors: [] } });

    const authors = await promise;
    expect(authors).toEqual([]);
  });
});

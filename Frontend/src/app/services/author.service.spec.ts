import { TestBed } from '@angular/core/testing';
import { ApolloTestingModule, ApolloTestingController } from 'apollo-angular/testing';
import { take } from 'rxjs/operators';
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

  it('should fetch authors via Apollo', (done) => {
    const mockAuthors = [
      { id: 1, name: 'George Orwell', bio: 'English novelist', books: [] },
    ];

    service.getAuthors().subscribe(authors => {
      if (authors.length === 0) return; // skip initial empty emission
      expect(authors.length).toBe(1);
      expect(authors[0].name).toBe('George Orwell');
      done();
    });

    const op = apolloController.expectOne(GET_AUTHORS);
    op.flush({ data: { authors: mockAuthors } });
  });

  it('should return empty array when no authors', (done) => {
    service.getAuthors().pipe(take(1)).subscribe(authors => {
      expect(authors).toEqual([]);
      done();
    });

    apolloController.expectOne(GET_AUTHORS).flush({ data: { authors: [] } });
  });
});

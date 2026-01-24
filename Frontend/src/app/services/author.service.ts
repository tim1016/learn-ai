import { Injectable, inject } from "@angular/core";
import { Apollo } from "apollo-angular";
import { map } from "rxjs/operators";
import { GET_AUTHORS } from "../graphql/queries";
import { GetAuthorsResponse, Author } from "../graphql/types";

@Injectable({
  providedIn: "root",
})
export class AuthorService {
  private apollo = inject(Apollo);

  getAuthors() {
    return this.apollo
      .watchQuery<GetAuthorsResponse>({
        query: GET_AUTHORS,
      })
      .valueChanges.pipe(map((result) => (result.data?.authors ?? []) as Author[]));
  }
}

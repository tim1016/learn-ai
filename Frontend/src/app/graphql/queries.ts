import { gql } from "apollo-angular";

export const GET_BOOKS = gql`
  query GetBooks {
    books {
      id
      title
      publishedYear
      author {
        id
        name
      }
    }
  }
`;

export const GET_AUTHORS = gql`
  query GetAuthors {
    authors {
      id
      name
      bio
      books {
        id
        title
        publishedYear
      }
    }
  }
`;

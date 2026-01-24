# Books & Authors Application

A full-stack application with a .NET 8 GraphQL backend and Angular 20 frontend.

## Prerequisites

- Docker or Podman
- Node.js 20+
- npm

## Backend

The backend is a .NET 8 GraphQL API using Hot Chocolate with a PostgreSQL database.

### Launch Backend

```bash
# From the project root directory
docker compose up -d
# or with Podman
podman compose up -d
```

The backend will be available at:

- **GraphQL Endpoint:** http://localhost:5000/graphql
- **GraphQL Playground:** http://localhost:5000/graphql (browser)

### Sample Queries

```graphql
# Get all books
query {
  getBooks {
    id
    title
    publishedYear
    author {
      name
    }
  }
}

# Get all authors
query {
  getAuthors {
    id
    name
    bio
    books {
      title
      publishedYear
    }
  }
}
```

### Stop Backend

```bash
docker compose down
# or with Podman
podman compose down
```

## Frontend

The frontend is an Angular 20 application using Apollo Client for GraphQL.

### Install Dependencies

```bash
cd Frontend
npm install
```

### Launch Frontend

```bash
cd Frontend
npm start
```

The frontend will be available at: http://localhost:4200

### Pages

- **/books** - View all books with author information
- **/authors** - View all authors with their books

## Quick Start

1. Start the backend:

   ```bash
   docker compose up -d
   ```

2. Wait for the backend to be ready (check logs):

   ```bash
   docker compose logs -f backend
   ```

3. In a new terminal, start the frontend:

   ```bash
   cd Frontend
   npm install
   npm start
   ```

4. Open http://localhost:4200 in your browser

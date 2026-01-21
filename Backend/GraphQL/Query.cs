
using Backend.Data;
using Backend.Models;

namespace Backend.GraphQL;

public class Query
{
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Book> GetBooks(AppDbContext context)
        => context.Books;

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Author> GetAuthors(AppDbContext context)
        => context.Authors;

    [UseFirstOrDefault]
    [UseProjection]
    public IQueryable<Book?> GetBookById(AppDbContext context, int id)
        => context.Books.Where(b => b.Id == id);

    [UseFirstOrDefault]
    [UseProjection]
    public IQueryable<Author?> GetAuthorById(AppDbContext context, int id)
        => context.Authors.Where(a => a.Id == id);
}

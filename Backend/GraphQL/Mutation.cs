using Backend.Data;
using Backend.Models;

namespace Backend.GraphQL;

public class Mutation
{
    public async Task<Author> AddAuthor(
        AppDbContext context,
        string name,
        string? bio)
    {
        var author = new Author { Name = name, Bio = bio };
        context.Authors.Add(author);
        await context.SaveChangesAsync();
        return author;
    }

    public async Task<Book> AddBook(
        AppDbContext context,
        string title,
        int publishedYear,
        int authorId)
    {
        var book = new Book
        {
            Title = title,
            PublishedYear = publishedYear,
            AuthorId = authorId
        };
        context.Books.Add(book);
        await context.SaveChangesAsync();

        await context.Entry(book).Reference(b => b.Author).LoadAsync();
        return book;
    }

    public async Task<Book?> UpdateBook(
        AppDbContext context,
        int id,
        string? title,
        int? publishedYear,
        int? authorId)
    {
        var book = await context.Books.FindAsync(id);
        if (book is null) return null;

        if (title is not null) book.Title = title;
        if (publishedYear.HasValue) book.PublishedYear = publishedYear.Value;
        if (authorId.HasValue) book.AuthorId = authorId.Value;

        await context.SaveChangesAsync();
        await context.Entry(book).Reference(b => b.Author).LoadAsync();
        return book;
    }

    public async Task<bool> DeleteBook(
        AppDbContext context,
        int id)
    {
        var book = await context.Books.FindAsync(id);
        if (book is null) return false;

        context.Books.Remove(book);
        await context.SaveChangesAsync();
        return true;
    }
}

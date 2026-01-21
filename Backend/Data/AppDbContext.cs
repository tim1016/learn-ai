using Backend.Models;
using Microsoft.EntityFrameworkCore;

namespace Backend.Data;

public class AppDbContext : DbContext
{
    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options)
    {
    }

    public DbSet<Author> Authors => Set<Author>();
    public DbSet<Book> Books => Set<Book>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<Author>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.Name).IsRequired().HasMaxLength(200);
            entity.Property(a => a.Bio).HasMaxLength(1000);
        });

        modelBuilder.Entity<Book>(entity =>
        {
            entity.HasKey(b => b.Id);
            entity.Property(b => b.Title).IsRequired().HasMaxLength(300);
            entity.HasOne(b => b.Author)
                  .WithMany(a => a.Books)
                  .HasForeignKey(b => b.AuthorId);
        });

        // Seed some initial data
        modelBuilder.Entity<Author>().HasData(
            new Author { Id = 1, Name = "George Orwell", Bio = "English novelist and essayist" },
            new Author { Id = 2, Name = "Jane Austen", Bio = "English novelist known for romantic fiction" }
        );

        modelBuilder.Entity<Book>().HasData(
            new Book { Id = 1, Title = "1984", PublishedYear = 1949, AuthorId = 1 },
            new Book { Id = 2, Title = "Animal Farm", PublishedYear = 1945, AuthorId = 1 },
            new Book { Id = 3, Title = "Pride and Prejudice", PublishedYear = 1813, AuthorId = 2 }
        );
    }
}

using System.Net;

namespace Backend.Tests.Helpers;

/// <summary>
/// Shared fake HttpMessageHandler for testing HTTP client calls without a real server.
/// Supports multiple constructor patterns used across test files.
/// </summary>
public class FakeHttpMessageHandler : HttpMessageHandler
{
    private readonly HttpResponseMessage _response;

    public Uri? LastRequestUri { get; private set; }
    public HttpMethod? LastRequestMethod { get; private set; }
    public string? LastRequestBody { get; private set; }

    /// <summary>
    /// Create from a pre-built response message (used by LstmServiceTests).
    /// </summary>
    public FakeHttpMessageHandler(HttpResponseMessage response)
    {
        _response = response;
    }

    /// <summary>
    /// Create from status code and response body string (used by PolygonServiceTests, SanitizationServiceTests).
    /// </summary>
    public FakeHttpMessageHandler(HttpStatusCode statusCode, string responseBody)
    {
        _response = new HttpResponseMessage(statusCode)
        {
            Content = new StringContent(responseBody, System.Text.Encoding.UTF8, "application/json")
        };
    }

    /// <summary>
    /// Create from response body and status code (reversed parameter order, used by ResearchServiceTests).
    /// </summary>
    public FakeHttpMessageHandler(string responseBody, HttpStatusCode statusCode)
        : this(statusCode, responseBody)
    {
    }

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();

        LastRequestUri = request.RequestUri;
        LastRequestMethod = request.Method;
        if (request.Content != null)
            LastRequestBody = await request.Content.ReadAsStringAsync(cancellationToken);

        return _response;
    }
}

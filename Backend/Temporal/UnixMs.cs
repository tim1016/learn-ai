namespace Backend.Temporal;

public static class UnixMs
{
    public static long FromUtc(DateTime value)
    {
        if (value.Kind == DateTimeKind.Unspecified)
            throw new ArgumentException("Timestamp DateTime must have an explicit UTC kind", nameof(value));

        var utc = value.Kind == DateTimeKind.Utc ? value : value.ToUniversalTime();
        return new DateTimeOffset(utc).ToUnixTimeMilliseconds();
    }

    public static DateTime ToUtcDateTime(long value)
    {
        return DateTimeOffset.FromUnixTimeMilliseconds(value).UtcDateTime;
    }
}

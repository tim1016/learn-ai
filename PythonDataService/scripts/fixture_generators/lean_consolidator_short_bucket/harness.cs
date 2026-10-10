using System;
using QuantConnect.Data.Market;
using QuantConnect.Data.Consolidators;
using QuantConnect.Securities;

public static class Harness
{
    public static void Main()
    {
        var period = TimeSpan.FromMinutes(15);
        // The consolidator reads only Time/OHLCV; any Symbol instance does (Symbol.Create
        // would ask the market-hours database for one we never use here).
        QuantConnect.Symbol SymbolRepresentation(string ticker) => QuantConnect.Symbol.Empty;
        var c = new TradeBarConsolidator(period);
        // 2026-03-18, a regular session. The port floors to the ET wall clock;
        // LEAN's consolidator works on naive DateTimes, so the same wall clock
        // is fed directly (naive ET == what the pinned port derives to).
        var base0 = new DateTime(2026, 3, 18, 10, 0, 0);
        var rnd = new Random(42);
        decimal px = 5000m;
        TradeBar Minute(DateTime t, int vol)
        {
            px += rnd.Next(-10, 11);
            var p = px / 10m;
            return new TradeBar { Symbol = SymbolRepresentation("SPY"), Time = t, EndTime = t.AddMinutes(1), Open = p, High = p, Low = p, Close = p, Volume = vol };
        }
        var fired = new System.Collections.Generic.List<TradeBar>();
        c.DataConsolidated += (s, e) => fired.Add((TradeBar)e);
        // Minutes 10:00..10:13: fourteen bars; the 10:14 minute never arrives.
        for (int i = 0; i < 14; i++) { var b = Minute(base0.AddMinutes(i), 100 + i); Console.WriteLine($"INPUT  {b.Time:yyyy-MM-ddTHH:mm:ss} {b.Open} {b.High} {b.Low} {b.Close} {b.Volume}"); c.Update(b); }
        // The next bucket's first bar fires the short bucket.
        var fire = Minute(base0.AddMinutes(16), 999);
        Console.WriteLine($"INPUT  {fire.Time:yyyy-MM-ddTHH:mm:ss} {fire.Open} {fire.High} {fire.Low} {fire.Close} {fire.Volume}");
        c.Update(fire);
        foreach (var b in fired)
            Console.WriteLine(
                $"EMITTED Time={b.Time:yyyy-MM-ddTHH:mm:ss} EndTime={b.EndTime:yyyy-MM-ddTHH:mm:ss} "
                + $"O={b.Open} H={b.High} L={b.Low} C={b.Close} V={b.Volume}");
    }
}

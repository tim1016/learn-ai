namespace Backend.Models.Portfolio;

public enum AccountType
{
    Paper,
    Backtest
}

public enum OrderSide
{
    Buy,
    Sell
}

public enum OrderType
{
    Market,
    Limit,
    Stop
}

public enum OrderStatus
{
    Pending,
    Filled,
    PartiallyFilled,
    Cancelled
}

public enum AssetType
{
    Stock,
    Option
}

public enum PositionStatus
{
    Open,
    Closed
}

public enum OptionType
{
    Call,
    Put
}

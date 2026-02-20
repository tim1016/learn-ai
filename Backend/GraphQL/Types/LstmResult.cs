namespace Backend.GraphQL.Types;

public class LstmJobResult
{
    public bool Success { get; set; }
    public string JobId { get; set; } = "";
    public string? Message { get; set; }
}

public class LstmJobStatus
{
    public required string JobId { get; set; }
    public required string Status { get; set; }
    public LstmTrainResult? TrainResult { get; set; }
    public LstmValidateResult? ValidateResult { get; set; }
    public string? Error { get; set; }
    public string? CreatedAt { get; set; }
    public string? CompletedAt { get; set; }
}

public class LstmTrainResult
{
    public string Ticker { get; set; } = "";
    public double ValRmse { get; set; }
    public double TrainRmse { get; set; }
    public double BaselineRmse { get; set; }
    public double Improvement { get; set; }
    public int EpochsCompleted { get; set; }
    public int BestEpoch { get; set; }
    public string ModelId { get; set; } = "";
    public List<double> ActualValues { get; set; } = [];
    public List<double> PredictedValues { get; set; } = [];
    public List<double> HistoryLoss { get; set; } = [];
    public List<double> HistoryValLoss { get; set; } = [];
    public List<double> Residuals { get; set; } = [];
}

public class LstmValidateResult
{
    public string Ticker { get; set; } = "";
    public int NumFolds { get; set; }
    public double AvgRmse { get; set; }
    public double AvgMae { get; set; }
    public double AvgMape { get; set; }
    public double AvgDirectionalAccuracy { get; set; }
    public List<LstmFoldResult> FoldResults { get; set; } = [];
}

public class LstmFoldResult
{
    public int Fold { get; set; }
    public int TrainSize { get; set; }
    public int TestSize { get; set; }
    public double Rmse { get; set; }
    public double Mae { get; set; }
    public double Mape { get; set; }
    public double DirectionalAccuracy { get; set; }
}

public class LstmModelInfo
{
    public string ModelId { get; set; } = "";
    public string Ticker { get; set; } = "";
    public string CreatedAt { get; set; } = "";
    public double ValRmse { get; set; }
    public double TrainRmse { get; set; }
    public double BaselineRmse { get; set; }
    public double Improvement { get; set; }
    public int EpochsCompleted { get; set; }
    public int BestEpoch { get; set; }
    public int SequenceLength { get; set; }
    public List<string> Features { get; set; } = [];
}

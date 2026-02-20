namespace Backend.Models.DTOs;

#region Request DTOs (sent to Python service)

public class LstmTrainingConfigDto
{
    public required string Ticker { get; set; }
    public required string FromDate { get; set; }
    public required string ToDate { get; set; }
    public int Epochs { get; set; } = 50;
    public int SequenceLength { get; set; } = 60;
    public string Features { get; set; } = "close";
    public bool Mock { get; set; } = false;
    public string ScalerType { get; set; } = "standard";
    public bool LogReturns { get; set; } = false;
    public bool Winsorize { get; set; } = false;
    public string Timespan { get; set; } = "day";
    public int Multiplier { get; set; } = 1;
}

public class LstmValidationConfigDto
{
    public required string Ticker { get; set; }
    public required string FromDate { get; set; }
    public required string ToDate { get; set; }
    public int Folds { get; set; } = 5;
    public int Epochs { get; set; } = 20;
    public int SequenceLength { get; set; } = 60;
    public bool Mock { get; set; } = false;
    public string ScalerType { get; set; } = "standard";
    public bool LogReturns { get; set; } = false;
    public bool Winsorize { get; set; } = false;
    public string Timespan { get; set; } = "day";
    public int Multiplier { get; set; } = 1;
}

#endregion

#region Response DTOs (received from Python service)

public class LstmJobSubmitResponseDto
{
    public string JobId { get; set; } = "";
    public string Status { get; set; } = "";
}

public class LstmJobStatusResponseDto
{
    public string JobId { get; set; } = "";
    public string Status { get; set; } = "";
    public LstmTrainResultDto? TrainResult { get; set; }
    public LstmValidateResultDto? ValidateResult { get; set; }
    public string? Error { get; set; }
    public string? CreatedAt { get; set; }
    public string? CompletedAt { get; set; }
}

/// <summary>
/// Raw DTO for initial deserialization — result is a JsonElement so we
/// can detect whether it's a training or validation result.
/// </summary>
public class LstmJobStatusRawDto
{
    public string JobId { get; set; } = "";
    public string Status { get; set; } = "";
    public System.Text.Json.JsonElement? Result { get; set; }
    public string? Error { get; set; }
    public string? CreatedAt { get; set; }
    public string? CompletedAt { get; set; }
}

public class LstmTrainResultDto
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
    public double? StationarityAdfPvalue { get; set; }
    public double? StationarityKpssPvalue { get; set; }
    public bool? StationarityIsStationary { get; set; }
}

public class LstmValidateResultDto
{
    public string Ticker { get; set; } = "";
    public int NumFolds { get; set; }
    public double AvgRmse { get; set; }
    public double AvgMae { get; set; }
    public double AvgMape { get; set; }
    public double AvgDirectionalAccuracy { get; set; }
    public double? AvgSharpeRatio { get; set; }
    public double? AvgMaxDrawdown { get; set; }
    public double? AvgProfitFactor { get; set; }
    public List<LstmFoldResultDto> FoldResults { get; set; } = [];
}

public class LstmFoldResultDto
{
    public int Fold { get; set; }
    public int TrainSize { get; set; }
    public int TestSize { get; set; }
    public double Rmse { get; set; }
    public double Mae { get; set; }
    public double Mape { get; set; }
    public double DirectionalAccuracy { get; set; }
    public double? SharpeRatio { get; set; }
    public double? MaxDrawdown { get; set; }
    public double? ProfitFactor { get; set; }
}

public class LstmModelInfoDto
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

#endregion

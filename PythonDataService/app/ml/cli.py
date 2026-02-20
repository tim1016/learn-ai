from __future__ import annotations

import logging
from pathlib import Path

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@click.group()
def cli() -> None:
    """LSTM Stock Price Predictor CLI."""
    pass


@cli.command()
@click.option("--ticker", required=True, help="Stock ticker symbol")
@click.option("--from-date", required=True, help="Start date YYYY-MM-DD")
@click.option("--to-date", required=True, help="End date YYYY-MM-DD")
@click.option("--epochs", default=50, help="Training epochs")
@click.option("--sequence-length", default=60, help="Lookback window in days")
@click.option("--features", default="close", help="Comma-separated feature list")
@click.option("--mock", is_flag=True, help="Use mock data instead of Polygon")
@click.option(
    "--output-dir",
    default="trained_models",
    help="Directory for saved models and plots",
)
def train(
    ticker: str,
    from_date: str,
    to_date: str,
    epochs: int,
    sequence_length: int,
    features: str,
    mock: bool,
    output_dir: str,
) -> None:
    """Train an LSTM model on historical data."""
    from app.ml.evaluation.visualization import (
        plot_predictions,
        plot_residuals,
        plot_training_history,
        plot_zoom,
    )
    from app.ml.models.schemas import TrainingConfig
    from app.ml.services.prediction_service import PredictionService

    if mock:
        from app.ml.providers.mock_provider import MockDataProvider

        provider = MockDataProvider()
    else:
        from app.ml.providers.polygon_provider import PolygonDataProvider

        provider = PolygonDataProvider()

    config = TrainingConfig(
        ticker=ticker,
        from_date=from_date,
        to_date=to_date,
        epochs=epochs,
        sequence_length=sequence_length,
        features=features.split(","),
    )

    service = PredictionService(provider, model_dir=Path(output_dir))
    result, test_pred, y_test, history = service.train(config)

    # Generate plots
    plots_dir = Path(output_dir) / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    plot_predictions(
        y_test,
        test_pred,
        title=f"{ticker} — Actual vs Predicted",
        save_path=plots_dir / f"{ticker}_predictions.png",
    )
    plot_training_history(
        history,
        save_path=plots_dir / f"{ticker}_training_history.png",
    )
    plot_residuals(
        y_test,
        test_pred,
        save_path=plots_dir / f"{ticker}_residuals.png",
    )
    plot_zoom(
        y_test,
        test_pred,
        title=f"{ticker} — Last 60 Steps",
        save_path=plots_dir / f"{ticker}_zoom.png",
    )

    click.echo(f"\n{'=' * 60}")
    click.echo(f"  Training Complete: {result.ticker}")
    click.echo(f"{'=' * 60}")
    click.echo(f"  Val RMSE:          {result.val_rmse}")
    click.echo(f"  Baseline RMSE:     {result.baseline_rmse}")
    click.echo(f"  Improvement:       {result.improvement_over_baseline}%")
    click.echo(f"  Best Epoch:        {result.best_epoch}/{result.epochs_completed}")
    click.echo(f"  Model Saved:       {result.model_path}")
    click.echo(f"  Plots Saved:       {plots_dir}")
    click.echo(f"{'=' * 60}")

    if result.improvement_over_baseline <= 0:
        click.echo(
            "\n  WARNING: LSTM did NOT beat the naive persistence baseline."
        )
        click.echo("  The model may be ineffective for this data/configuration.\n")


@cli.command()
@click.option("--ticker", required=True, help="Stock ticker symbol")
@click.option("--from-date", required=True, help="Start date YYYY-MM-DD")
@click.option("--to-date", required=True, help="End date YYYY-MM-DD")
@click.option("--folds", default=5, help="Number of walk-forward folds")
@click.option("--epochs", default=20, help="Training epochs per fold")
@click.option("--sequence-length", default=60, help="Lookback window in days")
@click.option("--mock", is_flag=True, help="Use mock data instead of Polygon")
def validate(
    ticker: str,
    from_date: str,
    to_date: str,
    folds: int,
    epochs: int,
    sequence_length: int,
    mock: bool,
) -> None:
    """Run walk-forward validation."""
    from app.ml.models.schemas import TrainingConfig
    from app.ml.services.prediction_service import PredictionService

    if mock:
        from app.ml.providers.mock_provider import MockDataProvider

        provider = MockDataProvider()
    else:
        from app.ml.providers.polygon_provider import PolygonDataProvider

        provider = PolygonDataProvider()

    config = TrainingConfig(
        ticker=ticker,
        from_date=from_date,
        to_date=to_date,
        epochs=epochs,
        sequence_length=sequence_length,
    )

    service = PredictionService(provider)
    result = service.validate(config, n_folds=folds)

    click.echo(f"\n{'=' * 60}")
    click.echo(f"  Walk-Forward Validation: {result.ticker}")
    click.echo(f"{'=' * 60}")
    click.echo(f"  Folds Completed:         {result.num_folds}")
    click.echo(f"  Avg RMSE:                {result.avg_rmse}")
    click.echo(f"  Avg MAE:                 {result.avg_mae}")
    click.echo(f"  Avg MAPE:                {result.avg_mape}%")
    click.echo(f"  Avg Directional Accuracy: {result.avg_directional_accuracy}%")
    click.echo(f"{'=' * 60}")

    for fold in result.fold_results:
        click.echo(
            f"  Fold {fold['fold']}: "
            f"RMSE={fold['rmse']:.6f}, "
            f"Dir.Acc={fold['directional_accuracy']:.1f}%"
        )


if __name__ == "__main__":
    cli()

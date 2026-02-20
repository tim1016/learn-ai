from __future__ import annotations

import logging
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def plot_predictions(
    actual: np.ndarray,
    predicted: np.ndarray,
    title: str = "LSTM Predictions vs Actual",
    save_path: Path | None = None,
) -> None:
    """Plot actual vs predicted values."""
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(actual, label="Actual", linewidth=1.5)
    ax.plot(predicted, label="Predicted", linewidth=1.5, alpha=0.8)
    ax.set_title(title)
    ax.set_xlabel("Time Steps")
    ax.set_ylabel("Price (scaled)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"[ML] Plot saved to {save_path}")
    plt.close(fig)


def plot_training_history(
    history_dict: dict,
    save_path: Path | None = None,
) -> None:
    """Plot training and validation loss curves."""
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(history_dict["loss"], label="Training Loss")
    ax.plot(history_dict["val_loss"], label="Validation Loss")
    ax.set_title("Training History")
    ax.set_xlabel("Epoch")
    ax.set_ylabel("Loss (MSE)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"[ML] Training history plot saved to {save_path}")
    plt.close(fig)


def plot_residuals(
    actual: np.ndarray,
    predicted: np.ndarray,
    save_path: Path | None = None,
) -> None:
    """Plot residual distribution."""
    residuals = actual - predicted
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    axes[0].plot(residuals, alpha=0.6)
    axes[0].axhline(y=0, color="r", linestyle="--")
    axes[0].set_title("Residuals Over Time")
    axes[0].set_xlabel("Time Steps")
    axes[0].set_ylabel("Residual")

    axes[1].hist(residuals, bins=50, edgecolor="black", alpha=0.7)
    axes[1].set_title("Residual Distribution")
    axes[1].set_xlabel("Residual")
    axes[1].set_ylabel("Frequency")

    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"[ML] Residual plot saved to {save_path}")
    plt.close(fig)


def plot_zoom(
    actual: np.ndarray,
    predicted: np.ndarray,
    last_n: int = 60,
    title: str = "Zoom: Last 60 Steps",
    save_path: Path | None = None,
) -> None:
    """Zoom-in plot of the last N predictions."""
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(actual[-last_n:], label="Actual", linewidth=1.5, marker="o", markersize=3)
    ax.plot(
        predicted[-last_n:],
        label="Predicted",
        linewidth=1.5,
        alpha=0.8,
        marker="x",
        markersize=3,
    )
    ax.set_title(title)
    ax.set_xlabel("Time Steps")
    ax.set_ylabel("Price (scaled)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info(f"[ML] Zoom plot saved to {save_path}")
    plt.close(fig)

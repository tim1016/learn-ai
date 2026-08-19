"""Catalog-derived indicator warmup policy tests."""

from __future__ import annotations

from app.services.dataset_service import INDICATOR_CONFIGS
from app.services.indicator_warmup_policy import configured_indicator_warmup_bars


def test_configured_warmup_covers_the_largest_catalog_recipe() -> None:
    assert configured_indicator_warmup_bars(INDICATOR_CONFIGS) == 2_500


def test_configured_warmup_uses_typed_integer_metadata_not_parameter_names() -> None:
    catalog = {
        "future-indicator": [
            {"name": "differently_named_window", "type": "int", "max": 650},
            {"name": "scale", "type": "float", "max": 10.0},
        ]
    }

    assert configured_indicator_warmup_bars(catalog) == 3_250

"""Application configuration loaded from environment variables"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Pydantic-v2 settings: ``extra="ignore"`` so a local ``.env`` with
    project-specific extras (test fixtures, optional vendor tokens,
    one-off experiments) does not break ``Settings()`` construction.
    The class-based ``Config`` previously rejected unknown keys with a
    ``ValidationError`` at module import; that broke ``pytest``
    collection any time a developer dropped an experimental key into
    ``.env``. ``extra="ignore"`` is the standard Pydantic-settings
    convention for application config models where the env file is
    operator-edited.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    # Polygon API
    POLYGON_API_KEY: str
    # Polygon's paid plans (Starter / Developer / Advanced / Business) have
    # no per-minute cap, so the throttle is off by default. Only the free
    # Basic tier is 5/min — set this to 5 if you're on Basic. See
    # docs/references/polygon-throttle.md for the full plan table.
    POLYGON_RATE_LIMIT_PER_MIN: int = 0

    # FRED API (for dynamic risk-free rate)
    FRED_API_KEY: str = ""

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # CORS (comma-separated string)
    ALLOWED_ORIGINS: str = "http://backend:8080,http://localhost:5000,http://localhost:4200"

    def get_allowed_origins(self) -> list[str]:
        """Parse ALLOWED_ORIGINS into a list"""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",")]

    # Data sanitization settings
    MAX_NULL_PERCENTAGE: float = 0.1  # 10% max nulls allowed
    REMOVE_DUPLICATES: bool = True
    FILL_METHOD: str = "ffill"  # forward fill for time series

    # .NET backend URL for study persistence
    BACKEND_URL: str = "http://localhost:5000"

    # Rate limiting (optional)
    MAX_REQUESTS_PER_MINUTE: int = 100

    # Data lake (Slice 1a)
    # postgres://user:pass@host:5432/dbname — required when DATA_LAKE_ENABLED is true
    POSTGRES_URL: str = ""
    DATA_LAKE_ENABLED: bool = False


settings = Settings()

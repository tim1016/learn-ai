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

    # Git SHA of the code this container is running, surfaced on /health so an
    # operator can confirm the data plane matches master. The container has no
    # .git mount, so a live `git rev-parse` won't work here — this is sourced
    # from the GIT_COMMIT_SHA env/build-arg ("" if unset). The host daemon
    # (which executes live sessions) computes its own SHA live on /health.
    GIT_COMMIT_SHA: str = ""

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
    # Rebuildable read model over canonical lifecycle/account artifacts.
    # Requires POSTGRES_URL when enabled; files remain canonical when disabled
    # or unavailable.
    LIFECYCLE_PROJECTION_ENABLED: bool = False
    # Data lake writer root (Slice 1b). Container-side path of the RW mount.
    # The writer creates lake/ and staging/ subdirectories under this path.
    # Must be on a single filesystem so POSIX atomic rename(2) is valid.
    LEAN_DATA_WRITE_ROOT: str = "/lean-data-writer"
    # LEAN sidecar launcher (Slice 1c Phase 0 metadata extraction).
    # The launcher is a host process with podman access; the data-plane
    # container calls it via HTTP to extract market-hours-database.json
    # and symbol-properties-database.csv from the pinned LEAN image.
    # When running inside compose on Windows/WSL2, set to
    # http://host.containers.internal:8090. See PythonDataService/CLAUDE.md.
    LEAN_LAUNCHER_URL: str = "http://127.0.0.1:8090"
    LEAN_LAUNCHER_TOKEN: str = ""


settings = Settings()

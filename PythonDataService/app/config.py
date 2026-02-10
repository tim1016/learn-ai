"""Application configuration loaded from environment variables"""
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List, Union


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Polygon API
    POLYGON_API_KEY: str

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # CORS (comma-separated string)
    ALLOWED_ORIGINS: str = "http://backend:8080,http://localhost:5000,http://localhost:4200"

    def get_allowed_origins(self) -> list[str]:
        """Parse ALLOWED_ORIGINS into a list"""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(',')]

    # Data sanitization settings
    MAX_NULL_PERCENTAGE: float = 0.1  # 10% max nulls allowed
    REMOVE_DUPLICATES: bool = True
    FILL_METHOD: str = "ffill"  # forward fill for time series

    # Rate limiting (optional)
    MAX_REQUESTS_PER_MINUTE: int = 100

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()

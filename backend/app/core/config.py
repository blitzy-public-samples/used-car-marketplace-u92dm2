from pydantic import BaseSettings
from typing import List, Optional

class Settings(BaseSettings):
    PROJECT_NAME: str
    API_V1_STR: str
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_STORAGE_BUCKET: str
    STRIPE_API_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    SENTRY_DSN: Optional[str] = None
    ALGORITHM: str = "HS256"
    RATING_MIN: int = 1
    RATING_MAX: int = 5
    RATING_REVIEW_MAX_LENGTH: int = 2000
    RATING_WINDOW_DAYS: int = 14
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
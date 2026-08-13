import json
from pydantic import BaseSettings
from typing import List, Optional

class Settings(BaseSettings):
    PROJECT_NAME: str
    API_V1_STR: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"  # JWT signing algorithm used by token issuance and validation
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_STORAGE_BUCKET: str
    STRIPE_API_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    SENTRY_DSN: Optional[str] = None
    # Origins permitted by the CORS middleware in app/main.py. Declared here
    # because main.py read settings.ALLOWED_ORIGINS while Settings never
    # defined it, raising AttributeError at import and preventing boot.
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    class Config:
        # No env_file is declared, deliberately. Pydantic v1 reads a .env file
        # only through its optional python-dotenv extra, which this project
        # does not install, so a declared env_file could never load a file --
        # it could only make Settings() raise ImportError the moment one
        # existed in the working directory, killing the process instead of
        # configuring it and reinstating the import-time boot failure this
        # module was repaired to remove. Exported environment variables are
        # the only configuration source; see documentation/ONBOARDING.md
        # section 1.5. Re-declare env_file only together with an authorised
        # python-dotenv dependency (open task NT-2).

        @classmethod
        def parse_env_var(cls, field_name: str, raw_val: str):
            # Pydantic v1 JSON-decodes complex fields before validators run, so
            # ALLOWED_ORIGINS="http://a,http://b" would raise SettingsError at
            # import -- the same boot-failure class this fix removes. Accept a
            # comma-separated list as well as a JSON array.
            if field_name == "ALLOWED_ORIGINS":
                value = raw_val.strip()
                if value.startswith("["):
                    return json.loads(value)
                return [
                    origin.strip()
                    for origin in value.split(",")
                    if origin.strip()
                ]
            return cls.json_loads(raw_val)

settings = Settings()
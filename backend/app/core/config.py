import json
from pydantic import BaseSettings, validator
from typing import List, Optional

# Security policy applied to the settings below. Each bound exists because the
# value it guards is read by code that cannot defend itself: the token helper
# in app/api/auth.py treats a zero lifetime as "unset" and silently falls back
# to 15 minutes, jose signs with whatever ALGORITHM names, and the CORS
# middleware in app/main.py runs with allow_credentials=True.
_ALLOWED_JWT_ALGORITHMS = ('HS256', 'HS384', 'HS512')
# 32 characters is the length of the `openssl rand -hex 32` value the setup
# documentation prescribes; anything materially shorter is guessable for an
# HMAC key that signs every access token.
_MIN_SECRET_KEY_LENGTH = 32
# One day. A longer-lived bearer token cannot be withdrawn, because this
# codebase has no revocation mechanism at all.
_MAX_ACCESS_TOKEN_EXPIRE_MINUTES = 24 * 60


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

    @validator('SECRET_KEY')
    def _secret_key_is_long_enough(cls, value: str) -> str:
        # This key is the only thing standing between a caller and a forged
        # token: jose signs with HS256 and app/api/auth.py trusts any token
        # that verifies. A short key makes that signature guessable, so the
        # process refuses to start rather than serving a weak one.
        if len(value.strip()) < _MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                'SECRET_KEY must be at least %d characters; generate one '
                'with `openssl rand -hex 32`' % _MIN_SECRET_KEY_LENGTH
            )
        return value

    @validator('ALGORITHM')
    def _algorithm_is_supported(cls, value: str) -> str:
        # Both token issuance and validation pass this string straight to
        # jose, so an unexpected value here is not a configuration error that
        # surfaces later -- it changes how every token is signed and checked.
        # The allow-list is the symmetric family this codebase holds a shared
        # secret for; an asymmetric algorithm would need a key pair that no
        # setting supplies, and 'none' would disable verification outright.
        normalised = value.strip().upper()
        if normalised not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                'ALGORITHM must be one of %s' % ', '.join(
                    _ALLOWED_JWT_ALGORITHMS
                )
            )
        return normalised

    @validator('ACCESS_TOKEN_EXPIRE_MINUTES')
    def _token_lifetime_is_bounded(cls, value: int) -> int:
        # A zero or negative value is the dangerous case, and it fails
        # silently rather than loudly: app/api/auth.py's create_access_token
        # tests `if expires_delta:`, and timedelta(minutes=0) is falsey, so
        # the caller's configured lifetime would be discarded in favour of the
        # helper's hard-coded 15 minutes. Rejecting it here is what makes the
        # configured lifetime the one that actually governs.
        if value < 1:
            raise ValueError(
                'ACCESS_TOKEN_EXPIRE_MINUTES must be at least 1 minute'
            )
        if value > _MAX_ACCESS_TOKEN_EXPIRE_MINUTES:
            raise ValueError(
                'ACCESS_TOKEN_EXPIRE_MINUTES must not exceed %d minutes; '
                'this codebase has no way to revoke a longer-lived token'
                % _MAX_ACCESS_TOKEN_EXPIRE_MINUTES
            )
        return value

    @validator('ALLOWED_ORIGINS')
    def _origins_are_explicit(cls, value: List[str]) -> List[str]:
        # A wildcard here is not a permissive setting, it is an open door.
        # app/main.py registers CORSMiddleware with allow_credentials=True,
        # and in that configuration starlette does not answer with a literal
        # '*': it echoes back whichever Origin asked, so every site on the
        # internet passes the check and may drive a browser's credentialed
        # requests. Refusing the value at import is deliberate -- a
        # deployment that asks for '*' is told exactly what to do instead,
        # rather than starting up quietly with no origin restriction at all.
        origins = [origin.strip() for origin in value if origin.strip()]
        for origin in origins:
            if '*' in origin:
                raise ValueError(
                    'ALLOWED_ORIGINS may not contain a wildcard (%r): the '
                    'CORS middleware runs with credentials enabled, so every '
                    'origin must be named in full, for example '
                    '"https://app.example.com,https://admin.example.com"'
                    % origin
                )
        return origins

    class Config:
        # A .env file is read only when pydantic's optional python-dotenv
        # extra is installed. It is not part of this project's dependency
        # set, so with the documented install a .env file present in the
        # working directory makes Settings() raise ImportError at import.
        # Exported environment variables are the supported path; see
        # documentation/ONBOARDING.md section 1.5.
        env_file = ".env"
        env_file_encoding = "utf-8"

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
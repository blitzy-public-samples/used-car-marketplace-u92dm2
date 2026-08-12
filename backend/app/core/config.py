from pydantic import BaseSettings, Field, validator
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

    # --- Peer reputation tunables --------------------------------------
    # Each of the four carries a default, so no environment that has not
    # been updated can be broken by their addition, AND each carries a
    # CONSTRAINT, so an environment that sets one wrongly is stopped here
    # rather than silently producing wrong behaviour downstream.
    #
    # That distinction is the whole point of the constraints below. These
    # values are not merely descriptive: RATING_MIN/RATING_MAX are bound
    # into Pydantic field bounds on app/schema/rating.py at class
    # definition time, and RATING_WINDOW_DAYS decides when an
    # unreciprocated rating becomes visible. Unconstrained, a negative
    # RATING_WINDOW_DAYS would make every rating due for publication the
    # instant it was written - collapsing the double-blind reveal that
    # exists to prevent review extortion - and it would do so silently,
    # because the service clamps the value it is given rather than
    # questioning it. A RATING_MIN above RATING_MAX would produce a score
    # field that rejects every possible value, so no rating could ever be
    # submitted. Both are configuration mistakes that must fail loudly at
    # startup, which is exactly what a failing validator here does:
    # `settings = Settings()` is evaluated at import, so an invalid value
    # raises before any request is served.
    #
    # The 1..5 ceiling on the bounds themselves is not arbitrary either -
    # it is the scale the project's own success metric fixes ("4.5/5 star
    # average rating from both buyers and sellers"), mirrored by the Zod
    # schema on the client and by the star control's five options.
    RATING_MIN: int = Field(1, ge=1, le=5)
    RATING_MAX: int = Field(5, ge=1, le=5)
    RATING_REVIEW_MAX_LENGTH: int = Field(2000, ge=1, le=20000)
    RATING_WINDOW_DAYS: int = Field(14, ge=0, le=365)

    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    @validator("RATING_MAX")
    def rating_max_must_not_precede_rating_min(
        cls,
        rating_max: int,
        values: dict,
    ) -> int:
        """Reject a rating scale whose bounds are inverted.

        Declared on ``RATING_MAX`` rather than on ``RATING_MIN`` because
        Pydantic v1 validates fields in declaration order and exposes
        only the already-validated ones in ``values`` - so ``RATING_MIN``
        is available here and ``RATING_MAX`` would not be available the
        other way round.

        ``RATING_MIN`` is absent from ``values`` when it failed its own
        field constraint; in that case this validator adds nothing and
        defers, because the error already being reported is the specific
        one, and raising a second, vaguer error over the top of it would
        only obscure the cause.

        Args:
            rating_max: The candidate upper bound.
            values: Fields validated before this one.

        Returns:
            ``rating_max`` unchanged when the scale is coherent.

        Raises:
            ValueError: The lower bound exceeds the upper bound, which
                would yield a score field no value can satisfy.
        """
        rating_min = values.get("RATING_MIN")
        if rating_min is not None and rating_min > rating_max:
            raise ValueError(
                "RATING_MIN ({0}) must not exceed RATING_MAX ({1}): the "
                "rating scale would admit no valid score".format(
                    rating_min, rating_max
                )
            )
        return rating_max

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

from pydantic import BaseSettings, Field, constr, validator
from typing import Any, List, Optional

# A required setting whose value must actually be present. Declaring one
# of these as a bare ``str`` makes it required in NAME only: pydantic
# accepts the empty string, so an environment that exports the key with
# nothing after the equals sign starts the application with a setting
# that is configured and unusable at the same time. That is the worst of
# the three possible states - a missing key fails loudly at import, a
# real value works, and a blank one boots and then misbehaves somewhere
# far away from the cause.
RequiredSetting = constr(strict=True, min_length=1)

# The JWT signing key, held to a real minimum length rather than merely
# to being present. This is a security boundary, not tidiness: every
# access token is signed with this value and
# ``app/api/auth.py:get_current_user`` resolves whatever ``sub`` a valid
# token carries into a real user document. With an empty or trivially
# short key, a token can be forged OFFLINE by anyone - no knowledge of
# any secret required - and the forged ``sub`` may name a verified user,
# which defeats the verified-rater gate the rating feature enforces.
# Failing at import is the only safe response, because there is no later
# point at which a weak signing key becomes detectable from inside a
# request.
#
# 32 characters is the length ``backend/.env.example`` already publishes
# for this key, and it matches the 256-bit output of HS256, the
# algorithm ``ALGORITHM`` below defaults to; a key shorter than the hash
# it feeds weakens the construction.
SigningSecret = constr(strict=True, min_length=32)

# Every required setting that must not be blank, named once so the field
# declarations and the whitespace validator below cannot drift apart.
BLANK_INTOLERANT_SETTINGS = (
    'PROJECT_NAME',
    'API_V1_STR',
    'SECRET_KEY',
    'GOOGLE_CLOUD_PROJECT',
    'GOOGLE_CLOUD_STORAGE_BUCKET',
    'STRIPE_API_KEY',
    'STRIPE_WEBHOOK_SECRET',
)


class Settings(BaseSettings):
    PROJECT_NAME: RequiredSetting
    API_V1_STR: RequiredSetting
    SECRET_KEY: SigningSecret
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    GOOGLE_CLOUD_PROJECT: RequiredSetting
    GOOGLE_CLOUD_STORAGE_BUCKET: RequiredSetting
    STRIPE_API_KEY: RequiredSetting
    STRIPE_WEBHOOK_SECRET: RequiredSetting
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

    @validator(*BLANK_INTOLERANT_SETTINGS)
    def required_setting_must_not_be_blank(
        cls,
        value: str,
        field: Any,
    ) -> str:
        """Reject a required setting whose value is only whitespace.

        The length constraints on the fields above stop the empty
        string, and this stops the near-miss that a length alone cannot
        see: a value of forty spaces satisfies ``min_length=32`` and is
        no more usable as a signing key than the empty string was, while
        a tab in place of a project id produces a Firestore client
        pointed at nothing.

        The value is checked but deliberately NOT trimmed. A secret is
        compared byte for byte, so silently rewriting one would change
        the key the application signs with relative to the key the
        operator configured - and any other service sharing that secret
        would then disagree about every token. Refusing is honest;
        rewriting is a surprise.

        Args:
            value: The configured value, already length-checked.
            field: The pydantic field being validated, used to name the
                offending setting in the error.

        Returns:
            ``value`` unchanged when it carries something real.

        Raises:
            ValueError: The value is entirely whitespace.
        """
        if not value.strip():
            raise ValueError(
                '{0} must not be blank: the value is only whitespace, '
                'which is configured and unusable at the same '
                'time'.format(field.name)
            )
        return value

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

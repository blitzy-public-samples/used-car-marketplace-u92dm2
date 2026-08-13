"""Application settings.

Every module in this package imports ``settings`` from here at import
time, and ``Settings()`` on the last line is evaluated during that
import, so a field that cannot be satisfied does not fail late - it
prevents the application from being imported at all.

That is why the eight pre-existing required fields are left exactly as
they were found, and why every field added since carries a default: an
environment that has not been updated must keep starting. The only
constraints declared below are the ones that would otherwise let a
deployment boot into a silently wrong state.

Pydantic v1 semantics throughout - ``backend/requirements.txt`` pins
``pydantic==1.10.13`` because ``BaseSettings`` was removed from the
top-level package in v2.
"""
from ipaddress import ip_address
from typing import List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseSettings, Field, validator

# Origins that must never appear in a credentialed allow-list.
#
# ``app/main.py`` pairs ``allow_origins=settings.ALLOWED_ORIGINS`` with
# ``allow_credentials=True``, so ``*`` here would let any site issue
# credentialed cross-origin requests and read the response (CWE-942).
# Starlette also declines to echo a literal ``*`` back once credentials
# are allowed, which makes the mistake quiet rather than loud: the
# deployment looks configured while every browser request is refused.
# ``null`` is the origin a sandboxed iframe or a ``file://`` document
# sends, and anybody can produce one.
FORBIDDEN_ORIGINS = frozenset({'*', 'null'})

# Hostnames that identify the developer's own machine. Cleartext HTTP is
# permitted for these and only these: a session cookie or bearer token
# sent to a non-loopback ``http://`` origin crosses the network in the
# clear, and the browser will happily do it because the allow-list said
# so.
LOOPBACK_HOSTNAMES = frozenset({'localhost', 'ip6-localhost'})


class Settings(BaseSettings):
    """Settings resolved from the environment and from ``.env``.

    The first nine fields are pre-existing and unchanged. The six that
    follow were added for the peer rating feature and for two settings
    the application already read without declaring; all six are
    defaulted, so no existing environment is invalidated by their
    presence.
    """

    PROJECT_NAME: str
    API_V1_STR: str
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    GOOGLE_CLOUD_PROJECT: str
    GOOGLE_CLOUD_STORAGE_BUCKET: str
    STRIPE_API_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    SENTRY_DSN: Optional[str] = None

    # The JWT signing algorithm, read by ``app/api/auth.py`` and
    # ``app/core/security.py`` and declared by neither before now.
    #
    # Constrained to the single reviewed value rather than typed as a
    # bare ``str``. Both consumers pass the symmetric
    # ``settings.SECRET_KEY``, so an asymmetric choice such as ``RS256``
    # has no key pair to use: it was accepted at import and failed only
    # when a token was signed or verified, one request into production.
    # ``none`` is the more serious case - an unsigned token is not a
    # credential at all - and a literal type refuses both at the moment
    # the value is configured. Changing the algorithm is a deliberate
    # code change on both consumers, which is what it should be.
    ALGORITHM: Literal['HS256'] = 'HS256'

    # The inclusive score bounds. Freely tunable and deliberately not
    # pinned to their defaults: they are ordinary configuration, and the
    # server is the authority that enforces them
    # (``app/schema/rating.py`` builds its score field from these two
    # values, so a change here is honoured by validation immediately).
    #
    # They are, however, half of a cross-stack contract. The client
    # mirrors 1..5 in ``frontend/src/schema/rating.ts``, in the five
    # options the star control renders and in the "out of 5" text every
    # rating surface reads out, and a separately built bundle cannot
    # discover a server environment variable. So an operator who changes
    # the scale here must ship the matching client change: otherwise the
    # interface offers a star the server answers with 422. The default
    # 1..5 is the scale the project's own success metric fixes ("4.5/5
    # star average rating from both buyers and sellers").
    RATING_MIN: int = 1
    RATING_MAX: int = 5

    # Maximum characters accepted in the optional written review,
    # measured on the normalised text. Mirrored by the client's live
    # character counter, so the same cross-stack note applies.
    RATING_REVIEW_MAX_LENGTH: int = 2000

    # Days after creation at which an unreciprocated rating publishes.
    #
    # Bounded at one day below rather than zero. Zero is not a shorter
    # window, it is the absence of one: every rating would be due for
    # publication the instant it was written, which collapses the
    # double-blind reveal that exists so neither party can retaliate
    # against the other's score - and it would collapse it silently,
    # because the service treats an elapsed window as normal. If a
    # deployment ever wants immediate publication, that is a distinct
    # policy that needs its own explicit switch, not a boundary value
    # that looks like a tuning choice.
    RATING_WINDOW_DAYS: int = Field(14, ge=1, le=365)

    # Browser origins permitted to call this API with credentials.
    # Validated below, because ``app/main.py`` hands this list straight
    # to CORSMiddleware alongside ``allow_credentials=True``.
    ALLOWED_ORIGINS: List[str] = ['http://localhost:3000']

    @validator('ALLOWED_ORIGINS', each_item=True)
    def origin_must_be_safe_for_credentials(cls, origin: str) -> str:
        """Reject an entry that is unsafe or that no browser can match.

        Args:
            origin: One configured origin.

        Returns:
            ``origin`` unchanged when it is a safe, well-formed origin.

        Raises:
            ValueError: The entry is blank, is a wildcard, carries no
                usable scheme or host, is cleartext HTTP to a
                non-loopback host, or is not a bare origin.
        """
        trimmed = origin.strip()
        if not trimmed or trimmed.lower() in FORBIDDEN_ORIGINS:
            raise ValueError(
                'ALLOWED_ORIGINS entry {0!r} is not usable: this API '
                'allows credentials, so every permitted origin must be '
                'named explicitly and none may be a wildcard'.format(
                    origin
                )
            )
        parts = urlsplit(trimmed)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError(
                'ALLOWED_ORIGINS entry {0!r} must be an absolute origin '
                'such as https://app.example.com'.format(origin)
            )
        # A browser compares the Origin header to these by exact string,
        # so anything beyond scheme, host and port can never match and
        # would silently block the client it was added for.
        if parts.path or parts.query or parts.fragment:
            raise ValueError(
                'ALLOWED_ORIGINS entry {0!r} must be a bare origin - '
                'scheme, host and optional port only, with no trailing '
                'slash, path, query or fragment'.format(origin)
            )
        if parts.scheme == 'http' and not cls._is_loopback(parts.hostname):
            raise ValueError(
                'ALLOWED_ORIGINS entry {0!r} must use https: cleartext '
                'HTTP is permitted only for loopback development '
                'origins, and this list is paired with '
                'allow_credentials=True'.format(origin)
            )
        return origin

    @staticmethod
    def _is_loopback(hostname: str) -> bool:
        """Report whether a hostname names the local machine.

        Args:
            hostname: The host component of an origin, already lowered
                by ``urlsplit``.

        Returns:
            ``True`` for ``localhost``, any ``*.localhost`` name, and
            any loopback IP literal (127.0.0.0/8 or ``::1``).
        """
        if hostname in LOOPBACK_HOSTNAMES or hostname.endswith('.localhost'):
            return True
        try:
            return ip_address(hostname).is_loopback
        except ValueError:
            return False

    @validator('RATING_MAX')
    def rating_max_must_not_precede_rating_min(
        cls,
        rating_max: int,
        values: dict,
    ) -> int:
        """Reject a scale whose bounds are inverted or empty.

        The two bounds are tunable, so this is the check that keeps them
        coherent: a scale with ``RATING_MIN`` above ``RATING_MAX`` admits
        no valid score, and every submission would be answered with 422
        for a reason no caller could act on.

        Declared on ``RATING_MAX`` because Pydantic v1 validates fields
        in declaration order and exposes only the already-validated ones
        in ``values``.

        Args:
            rating_max: The candidate upper bound.
            values: Fields validated before this one.

        Returns:
            ``rating_max`` unchanged when the scale is coherent.

        Raises:
            ValueError: The lower bound exceeds the upper bound.
        """
        rating_min = values.get('RATING_MIN')
        if rating_min is not None and rating_min > rating_max:
            raise ValueError(
                'RATING_MIN ({0}) must not exceed RATING_MAX ({1}): the '
                'rating scale would admit no valid score'.format(
                    rating_min, rating_max
                )
            )
        return rating_max

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

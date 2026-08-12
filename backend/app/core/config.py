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

# Substrings that mark a value as a template rather than a secret.
#
# A length bound alone is not integrity. The template this project ships
# previously published a 48-character example key, which satisfied
# ``min_length=32`` perfectly - so copying ``backend/.env.example`` to
# ``.env`` and changing nothing produced an application that booted
# normally and signed every token with a key printed in a public
# repository. That is strictly worse than a short key: it looks correct,
# so nobody looks again.
#
# Matched case-insensitively as substrings, which catches the padded
# variants a length check invites ("replace-me-please-...-32-chars") as
# well as the value verbatim. Every entry is a word that appears in
# placeholder text and effectively never in output from a cryptographic
# random generator - the chance of any of them landing inside a
# ``secrets.token_urlsafe`` value is on the order of one in a billion, so
# a real key is not going to be refused by accident. "test" is
# deliberately NOT on the list: a signing key that names itself as
# test-only is exactly what a test environment should be using, and the
# suite's own fixture secret says so.
PLACEHOLDER_SECRET_MARKERS = (
    'change',
    'change-me',
    'change_me',
    'changeme',
    'dummy',
    'dummy-secret',
    'example',
    'example-secret',
    'insecure',
    'insert-secret',
    'notasecret',
    'placeholder',
    'replace',
    'replace-with',
    'replace_with',
    'replacethis',
    'sample',
    'todo',
    'your-secret',
    'your_secret',
)

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

# Signing keys that are PUBLIC KNOWLEDGE and must never sign a token.
#
# A length bound alone is not a security control here, and this is the
# case that proves it: ``backend/.env.example`` is committed to this
# repository and publishes a 48-character placeholder for SECRET_KEY.
# It satisfies ``min_length=32`` perfectly, so the documented "copy the
# template, then edit it" workflow produced a running application whose
# signing key is readable by anyone with the repository - and a signing
# key that is public means any token can be forged offline, including one
# whose ``sub`` names a verified user, which defeats the verified-rater
# gate the rating feature enforces at its write boundary.
#
# The sentinel is therefore rejected BY VALUE. The template keeps
# publishing it, because a placeholder that is obviously a placeholder is
# the right thing for a template to carry; what changes is that copying
# it unedited now fails at import with an instruction, instead of booting
# a forgeable service. Compared case-insensitively after trimming, since
# a copied value differing only in case or padding is the same public
# string.
PLACEHOLDER_SECRETS = frozenset({
    'replace-with-a-long-random-secret-at-least-32-chars',
    'replace-with-a-long-random-secret',
    'changeme',
    'change-me',
    'your-secret-key',
    'your-secret-key-here',
    'secret',
    'password',
})


# Origins that must never appear in a credentialed CORS allow-list.
#
# ``app/main.py`` pairs ``allow_origins=settings.ALLOWED_ORIGINS`` with
# ``allow_credentials=True`` and wildcard methods and headers, so an
# origin of ``*`` here would let ANY site issue credentialed
# cross-origin requests and read the response - CWE-942. Starlette
# silently declines to echo a literal ``*`` back when credentials are
# allowed, which makes the mistake worse rather than better: the
# deployment appears configured while every legitimate browser request
# is refused, so the setting is rejected here where the cause is
# visible. ``null`` is listed too: it is the origin a sandboxed iframe or
# a ``file://`` document sends, and allowing it grants credentialed
# access to a page anybody can produce.
FORBIDDEN_ORIGINS = frozenset({'*', 'null'})

# The only schemes an origin may carry. An origin is a scheme, a host and
# an optional port and nothing else, so anything with a path, a query or
# a fragment is not an origin - and a value the browser will never match
# is a silent CORS failure rather than a loud one.
ALLOWED_ORIGIN_SCHEMES = ('http://', 'https://')


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

    # --- Peer reputation settings --------------------------------------
    # Each of the four carries a default, so no environment that has not
    # been updated can be broken by their addition. `settings =
    # Settings()` is evaluated at import, so a required addition would
    # have made the application unimportable everywhere at once.
    #
    # The first three are declared `const=True`, which in pydantic v1
    # means the ONLY value that validates is the default. That is a
    # deliberate reversal of an earlier design in which they were freely
    # tunable, and the reason is that each of the three is half of a
    # CROSS-STACK contract rather than a server preference:
    #
    #   * RATING_MIN/RATING_MAX are mirrored by `RATING_MIN`/`RATING_MAX`
    #     in frontend/src/schema/rating.ts, by the five options the star
    #     control renders, and by the "out of 5" text every rating
    #     surface reads out.
    #   * RATING_REVIEW_MAX_LENGTH is mirrored by `REVIEW_MAX_LENGTH` in
    #     the same module and drives the submission form's live character
    #     counter.
    #
    # The client is a separately built artefact and cannot discover a
    # server environment variable, so genuine tunability here could only
    # ever produce DRIFT: an operator setting RATING_MAX=4 would leave
    # the interface offering a fifth star that the server answers with
    # 422, and lowering the review length would leave the counter
    # promising an allowance the server refuses. Pinning the three makes
    # the two halves of the contract provably equal, and an environment
    # that tries to override one now fails at import with the conflict
    # named instead of shipping a client that disagrees with its server.
    # Changing the scale remains entirely possible - it is a code change
    # on both sides, committed together, which is what a contract change
    # should be.
    #
    # The 1..5 scale itself is the one the project's own success metric
    # fixes ("4.5/5 star average rating from both buyers and sellers").
    #
    # RATING_WINDOW_DAYS is the exception and stays freely tunable: it is
    # read only by the server (app/services/rating.py re-reads it on
    # every call), no client surface mirrors it, and how long an
    # unreciprocated rating stays unpublished is exactly the kind of
    # policy an operator should be able to change without a deployment.
    # It keeps a CONSTRAINT because it is not merely descriptive - a
    # negative window would make every rating due for publication the
    # instant it was written, collapsing the double-blind reveal that
    # exists to prevent review extortion, and it would do so silently
    # because the service clamps the value it is given rather than
    # questioning it.
    RATING_MIN: int = Field(1, const=True)
    RATING_MAX: int = Field(5, const=True)
    RATING_REVIEW_MAX_LENGTH: int = Field(2000, const=True)
    RATING_WINDOW_DAYS: int = Field(14, ge=0, le=365)

    # The four settings above are the ONLY rating settings this feature
    # adds, and the list is closed deliberately rather than by omission.
    #
    # Two more were briefly declared here and have been removed:
    # RATING_SWEEP_SCAN_LIMIT, a ceiling on the scheduled window sweep,
    # and CELERY_BROKER_URL. Neither belongs in the configuration surface.
    # The sweep's ceiling is a property of the query it walks, so it lives
    # beside that query as a constant in app/services/rating.py where the
    # reasoning for its value is visible; exposing it as a setting invited
    # an operator to tune a number whose meaning is only legible next to
    # the code that spends it, and it existed to support a global
    # equality-plus-range sweep that needed a composite index this project
    # does not declare. And a broker URL configures the worker tier, which
    # is aspirational in this build - no task is ever dispatched and no
    # broker is provisioned - so app/tasks/background_jobs.py names its
    # in-memory transport outright instead of reading a setting that would
    # only ever be absent. A setting that is always unset is not
    # configuration; it is a promise the deployment cannot keep.

    # Browser origins permitted to call this API with credentials.
    # Validated below rather than merely typed: see FORBIDDEN_ORIGINS.

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

    @validator('SECRET_KEY')
    def secret_key_must_not_be_a_published_placeholder(
        cls,
        secret_key: str,
    ) -> str:
        """Reject a signing key that is public knowledge.

        The length constraint on the field proves only that a value is
        long. This proves that it is SECRET, which is the property the
        field actually needs, and it closes a real path to forged
        tokens: ``backend/.env.example`` is committed and publishes a
        48-character placeholder, so the documented "copy the template,
        then edit it" workflow could otherwise start an application whose
        signing key is readable by anyone with the repository. From
        there a token is forgeable offline, and because
        ``app/api/auth.py:get_current_user`` resolves whatever ``sub`` a
        valid token carries straight into a user document, the forged
        identity can name a verified user and pass the verified-rater
        gate on ratings.

        Failing at import is the only safe response. There is no later
        point at which a public signing key becomes detectable from
        inside a request - every forged token verifies perfectly.

        Two tests, because one is not enough. The exact-match set
        catches the placeholders this project and its neighbours
        publish; the marker scan catches a placeholder an operator
        lengthened or reworded while leaving it a placeholder, which the
        exact set cannot anticipate. Both compare case-insensitively
        against the trimmed value, since a copy differing only in case
        or padding is the same public string. No generated secret
        contains an instruction to replace itself, so the marker scan
        cannot refuse a real key.

        Args:
            secret_key: The configured value, already length-checked and
                confirmed non-blank.

        Returns:
            ``secret_key`` unchanged when it is not a known placeholder.

        Raises:
            ValueError: The value is a published placeholder, so every
                token signed with it would be forgeable by anyone.
        """
        candidate = secret_key.strip().lower()
        marker = next(
            (
                found for found in PLACEHOLDER_SECRET_MARKERS
                if found in candidate
            ),
            None,
        )
        if candidate in PLACEHOLDER_SECRETS or marker is not None:
            raise ValueError(
                'SECRET_KEY is a published placeholder, not a secret, so '
                'every token signed with it could be forged by anyone '
                'with this repository. Generate a real key and set it '
                'before starting the application: python -c "import '
                'secrets; print(secrets.token_urlsafe(48))"'
            )
        return secret_key

    @validator('ALLOWED_ORIGINS')
    def allowed_origins_must_be_safe_for_credentials(
        cls,
        allowed_origins: List[str],
    ) -> List[str]:
        """Reject a CORS allow-list unsafe for credentialed requests.

        ``app/main.py`` configures ``CORSMiddleware`` with
        ``allow_origins=settings.ALLOWED_ORIGINS`` alongside
        ``allow_credentials=True`` and wildcard methods and headers. A
        wildcard origin in that combination is CWE-942: any site could
        issue credentialed cross-origin requests carrying the user's
        session and read the responses. The middleware block is
        deliberately not changed - the safe posture is enforced on the
        value that reaches it, here, at import, before a single request
        is served.

        Rejecting the wildcard also removes a subtler failure. Starlette
        declines to echo a literal ``*`` back once credentials are
        allowed, so a deployment configured that way LOOKS configured
        while every legitimate browser request is refused; the cause is
        far from the symptom. Refusing at construction reports it where
        it can be fixed.

        Beyond the wildcard, each entry is checked for being an origin at
        all - a scheme, a host, and optionally a port, with no path,
        query or fragment. A browser compares the ``Origin`` header to
        these by exact string, so a trailing slash or an embedded path
        cannot ever match and would silently block the very client it was
        added for.

        Args:
            allowed_origins: The configured origins, already parsed from
                the environment as a JSON array by pydantic.

        Returns:
            ``allowed_origins`` unchanged when every entry is a safe,
            well-formed origin.

        Raises:
            ValueError: The list is empty, or an entry is blank, is a
                wildcard, or is not a bare scheme-host-port origin.
        """
        if not allowed_origins:
            raise ValueError(
                'ALLOWED_ORIGINS must name at least one origin: an '
                'empty list serves no browser client at all'
            )
        for origin in allowed_origins:
            trimmed = origin.strip()
            if not trimmed:
                raise ValueError(
                    'ALLOWED_ORIGINS must not contain a blank entry, '
                    'which no Origin header can ever match'
                )
            if trimmed.lower() in FORBIDDEN_ORIGINS:
                raise ValueError(
                    'ALLOWED_ORIGINS must not contain {0!r}: this API '
                    'allows credentials, so a wildcard origin would let '
                    'any site issue credentialed cross-origin requests '
                    'and read the response. Name every permitted origin '
                    'explicitly.'.format(trimmed)
                )
            if not trimmed.startswith(ALLOWED_ORIGIN_SCHEMES):
                raise ValueError(
                    'ALLOWED_ORIGINS entry {0!r} must start with '
                    'http:// or https://'.format(trimmed)
                )
            remainder = trimmed.split('//', 1)[1]
            if not remainder:
                raise ValueError(
                    'ALLOWED_ORIGINS entry {0!r} names no host'.format(
                        trimmed
                    )
                )
            if any(character in remainder for character in '/?#'):
                raise ValueError(
                    'ALLOWED_ORIGINS entry {0!r} must be a bare origin '
                    '- scheme, host and optional port only, with no '
                    'trailing slash, path, query or fragment, because a '
                    'browser matches the Origin header by exact '
                    'string'.format(trimmed)
                )
        return allowed_origins

    @validator("RATING_MAX")
    def rating_max_must_not_precede_rating_min(
        cls,
        rating_max: int,
        values: dict,
    ) -> int:
        """Reject a rating scale whose bounds are inverted.

        Defence in depth rather than the primary control: both bounds are
        declared ``const=True`` above, so the only pair that validates is
        1..5 and an inversion is already unreachable through
        configuration. This stays because it states the invariant the
        rest of the feature relies on - a scale admitting no valid score
        would make every submission a 422 - and it would catch an
        inversion introduced by editing the defaults themselves, which is
        the one route ``const`` does not close.

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

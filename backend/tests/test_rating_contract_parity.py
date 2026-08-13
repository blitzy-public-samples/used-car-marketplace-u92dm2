"""The rating contract, compared across the two stacks that share it.

WHY THIS MODULE EXISTS
======================
Every other suite in this repository tests one side of the boundary
against its own declarations. ``test_ratings_api.py`` derives its
expectations from ``app/core/config.py`` and ``app/schema/rating.py``;
the frontend suites derive theirs from ``frontend/src/schema/rating.ts``
and ``frontend/src/services/rating.ts``. Both can be entirely green
while the two halves of the contract say different things, because
nothing in either suite ever reads the other side.

That is not a hypothetical. The rating feature is a shared contract in
at least eight places at once - the 1..5 scale, the 2000-character
review limit, the raw ceiling that guards the normaliser, the moderation
reason bound, the document-ID length, two closed enumerations, the five
method/path pairs, and the snake_case wire names each response field
travels under. Change ``RATING_MAX`` to 4 on the server and the client
still offers a fifth star that the server answers with 422. Rename
``vehicle_listing_id`` and the client's mapper reads ``undefined`` and
reports a contract failure to a user who did nothing wrong. Add a sixth
route on the server and nothing tells the client. In each case both test
suites stay green and the product is broken.

This module is the gate that fails instead. It holds the SERVER half
authoritatively - by importing the real models, the real router and the
real service - and reads the CLIENT half out of its source, then asserts
they agree.

HOW THE CLIENT HALF IS READ
===========================
By extracting declarations from the TypeScript source with plain regular
expressions. Deliberately not by running node, not by building the
frontend, and not by importing anything: a pytest run must not depend on
a JavaScript toolchain being installed, and the CI workflow that runs
this suite (``.github/workflows/backend_ci.yml``) sets up Python alone.

Extraction can only ever be as trustworthy as its failure mode, so two
properties are built in deliberately. First, every extractor RAISES when
it finds nothing, rather than returning an empty tuple that would make a
parity assertion pass vacuously. Second, ``TestExtractionSelfCheck``
asserts the extracted values are non-empty and plausible BEFORE any
comparison relies on them - so a refactor that moves a declaration into
a shape the regex no longer matches is reported as a broken gate rather
than silently disarming this whole module.

What is compared, and what is deliberately not:

* Compared, because the contract requires equality - the scale, every
  length bound, the members and ORDER of both enumerations, the field
  sets and their order on every request and response shape, the
  camelCase-to-snake_case mapper keys, and the five routes.
* Compared behaviourally on the server side wherever the server enforces
  a rule with a validator rather than a declaration: the score bounds
  carry no ``ge``/``le`` on their field info, so asserting the numbers
  match is not enough and the module submits values instead.
* NOT compared: wording. The two sides word most messages for their own
  audience, and demanding identical prose would make this module fail on
  a copy edit. The one exception is the aggregate's paired invariants,
  where the two sides already use the same sentence word for word and
  that agreement is worth holding.

DELIBERATE ASYMMETRIES, DOCUMENTED RATHER THAN EQUALISED
========================================================
Two differences between the stacks are correct and are recorded here so
that a future reader does not "fix" one of them into a defect:

* ``RatingCreate`` on the server REFUSES an unrecognised key (with the
  single exception of the ``ratee_id`` claim, which is tolerated so that
  it can be refused with a reason rather than ignored), while
  ``RatingCreateSchema`` on the client STRIPS one. Both are safe, and
  they are safe in the same direction: no spoofed field can reach the
  wire, and none can reach a document. The parity that matters is that
  the three-key body the client sends is exactly the body the server
  accepts, which ``TestPayloadParity`` asserts with the real model.
* ``EligibilityDecisionSchema`` on the client enforces cross-field
  invariants that ``EligibilityDecision`` on the server does not. The
  client is stricter on purpose - it refuses a decision that cannot
  exist rather than rendering one - and the risk that creates is that
  the server might emit a shape the client then refuses. Asserting the
  two models "agree" would not address that risk at all, so
  ``TestEligibilityDecisionConformance`` drives the REAL service through
  every one of its return paths and asserts each decision it actually
  emits satisfies the client's declared rules.

Unittest classes with ``pytest``-visible parameterisation done by
sub-test, matching the conventions of the sibling rating suites. Pydantic
v1 semantics throughout, matching the pin in ``backend/requirements.txt``.
"""
import json
import re
import unittest
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import (
    DEFAULT_BUYER_ID,
    DEFAULT_LISTING_ID,
    DEFAULT_OUTSIDER_ID,
    DEFAULT_SELLER_ID,
    DEFAULT_TRANSACTION_ID,
    TRANSACTIONS_COLLECTION,
    build_user,
    completed_transaction,
    fake_db,
    pending_transaction,
    seed_rating,
    seed_transaction,
    seed_user,
    utc_now,
)

from app.api.ratings import ModerationUpdate, UserRatingsResponse
from app.api.ratings import router as ratings_router
from app.core.config import Settings, settings
from app.main import app
from app.schema.rating import (
    DOCUMENT_ID_MAX_LENGTH,
    MODERATION_REASON_MAX_LENGTH,
    RATEE_ID_CLAIM,
    RATING_ID_ESCAPE_FACTOR,
    RATING_ID_MAX_LENGTH,
    REVIEW_RAW_LENGTH_FACTOR,
    REVIEW_RAW_MAX_LENGTH,
    EligibilityDecision,
    ModeratedRatingView,
    ModerationStatus,
    Rating,
    RatingAggregate,
    RatingCreate,
    RatingDirection,
    RatingView,
)
from app.schema.user import User
from app.services.rating import evaluate_eligibility

# ---------------------------------------------------------------------------
# LOCATING THE CLIENT HALF
# ---------------------------------------------------------------------------
# Walked up from this file rather than assumed, because the backend suite
# is run from at least two working directories in practice - the CI
# workflow uses ``working-directory: backend`` while a developer
# typically runs pytest from the repository root - and a relative path
# would resolve differently between them.
FRONTEND_SOURCE_ROOT = None
for _candidate in Path(__file__).resolve().parents:
    if (_candidate / 'frontend' / 'src' / 'schema' / 'rating.ts').is_file():
        FRONTEND_SOURCE_ROOT = _candidate / 'frontend' / 'src'
        break

if FRONTEND_SOURCE_ROOT is None:
    # A module-level skip rather than a failure, and only for the one
    # situation it can describe honestly: a checkout that contains the
    # backend without the frontend. There is nothing to compare against
    # in that case and no defect to report. The reason names the file
    # that was looked for, so a skip is never mistaken for a pass.
    pytest.skip(
        'Cross-stack parity needs the client half of the contract: '
        'frontend/src/schema/rating.ts was not found above {0}. Every '
        'other rating suite still runs; only the backend-to-frontend '
        'comparison is unavailable in this checkout.'.format(
            Path(__file__).resolve().parent
        ),
        allow_module_level=True,
    )

# The three client modules that DECLARE the contract. Any drift this
# module can detect is a difference between one of these files and the
# server's own models, so they are named once here.
SCHEMA_SOURCE = 'schema/rating.ts'
USER_SCHEMA_SOURCE = 'schema/user.ts'
SERVICE_SOURCE = 'services/rating.ts'

# Client modules that must consume the scale from the schema rather than
# restate it. Parity is only meaningful if each side has ONE source of
# truth: a component carrying its own ``5`` would keep matching this
# module's assertions while disagreeing with the schema it renders.
SCALE_CONSUMERS = (
    'components/StarRatingInput.tsx',
    'components/RatingSubmissionForm.tsx',
    'components/ReputationBadge.tsx',
    'components/RatingList.tsx',
    'utils/validation.ts',
)

# A key or member on its own line: the indentation, then an identifier,
# then an optional TypeScript optional-marker, then the colon. Used with
# the shallowest-indent rule in ``_top_level_keys`` so that nested option
# objects - a Zod ``{ message: … }``, for instance - are excluded.
_MEMBER_LINE = re.compile(r'^(\s+)([A-Za-z_][A-Za-z0-9_]*)\??:')

# Where a produced object literal starts inside a mapper. Slicing here
# is load-bearing rather than cosmetic: these mappers declare their
# parameter on its own line (``  wire: RatingWire``), which is SHALLOWER
# than the literal's keys, so extracting from the whole declaration
# would return the parameter name and compare it against a field set.
_LITERAL_START = re.compile(r'\.parse\(\{|const body: \w+ = \{')

# One axios call in the client: the method, then the path expression,
# which is either the bare ``RATINGS_PATH`` constant or a template
# literal built from it.
_REQUEST_CALL = re.compile(
    r'api\.(get|post|patch)<[^>]*>\(\s*(RATINGS_PATH|`[^`]*`)'
)

# ``${encodeURIComponent(transactionId)}`` - one interpolated path
# segment, whose identifier names the parameter the server declares.
_PATH_PARAMETER = re.compile(r'\$\{encodeURIComponent\((\w+)\)\}')


def read_client_source(relative_path):
    """Return one client module's text.

    Args:
        relative_path: Path under ``frontend/src``, e.g.
            ``'schema/rating.ts'``.

    Returns:
        The file's contents as text.

    Raises:
        AssertionError: The file is absent. Raised rather than skipped:
            the tree was found, so a missing module inside it is a
            genuine defect and not an unavailable comparison.
    """
    path = FRONTEND_SOURCE_ROOT / relative_path
    if not path.is_file():
        raise AssertionError(
            'client module {0} is declared by this parity gate but does '
            'not exist at {1}'.format(relative_path, path)
        )
    return path.read_text(encoding='utf-8')


def _declaration(source, keyword, name):
    """Return the text of one top-level declaration.

    Sliced from the declaration's own line to the next line that starts a
    new top-level declaration, which is what keeps a schema's trailing
    ``export type X = z.infer<…>`` and the doc comment of the next
    declaration out of the slice.

    The ``\\b`` after the name is what makes this safe to call with a
    name that prefixes another: ``toRating`` must not match
    ``toRatingCreateWire``, and ``RatingSchema`` must not match
    ``ModeratedRatingSchema``.

    Args:
        source: Module text.
        keyword: ``'const'`` or ``'interface'``.
        name: Declared identifier.

    Returns:
        The declaration's text.

    Raises:
        AssertionError: No such declaration. An extractor that returned
            nothing here would disarm every assertion built on it.
    """
    opening = re.search(
        r'^(?:export )?{0} {1}\b'.format(keyword, re.escape(name)),
        source,
        re.M,
    )
    if opening is None:
        raise AssertionError(
            'no top-level {0} named {1} in the client source'.format(
                keyword, name
            )
        )
    rest = source[opening.start():]
    # Searched from index 1 so the declaration's own opening line cannot
    # terminate the slice it begins.
    following = re.search(r'^(?:export |const |interface )', rest[1:], re.M)
    return rest[:following.start() + 1] if following else rest


def numeric_constant(source, name):
    """Return the value of a client ``const NAME = <integer>;``.

    Args:
        source: Module text.
        name: Constant identifier, exported or module-local.

    Returns:
        The declared integer.

    Raises:
        AssertionError: The constant is absent, or is not a plain
            integer literal. Both mean the value this gate compares
            against has moved, and neither may pass quietly.
    """
    declared = re.search(
        r'^(?:export )?const {0}\s*=\s*(\d+);'.format(re.escape(name)),
        source,
        re.M,
    )
    if declared is None:
        raise AssertionError(
            'no integer constant named {0} in the client source'.format(name)
        )
    return int(declared.group(1))


def string_constant(source, name):
    """Return the value of a client ``const NAME = '<text>';``.

    Args:
        source: Module text.
        name: Constant identifier.

    Returns:
        The declared string.

    Raises:
        AssertionError: The constant is absent or is not a single-quoted
            literal.
    """
    declared = re.search(
        r"^(?:export )?const {0}\s*=\s*'([^']*)';".format(re.escape(name)),
        source,
        re.M,
    )
    if declared is None:
        raise AssertionError(
            'no string constant named {0} in the client source'.format(name)
        )
    return declared.group(1)


def enum_members(source, name):
    """Return the members of a client ``z.enum([...])`` schema, in order.

    Order is preserved and asserted rather than sorted away, because it
    is what the client's own inferred union and its error message
    (``Expected 'buyer_to_seller' | 'seller_to_buyer'``) present.

    Args:
        source: Module text.
        name: Schema identifier, e.g. ``'RatingDirectionSchema'``.

    Returns:
        The declared members as a tuple of strings.

    Raises:
        AssertionError: The declaration is not a ``z.enum``, or declares
            no members.
    """
    body = _declaration(source, 'const', name)
    bracket = re.search(r'z\.enum\(\[(.*?)\]', body, re.S)
    if bracket is None:
        raise AssertionError(
            '{0} is not declared as a z.enum, so its closed set of '
            'values cannot be compared'.format(name)
        )
    members = tuple(re.findall(r"'([^']+)'", bracket.group(1)))
    if not members:
        raise AssertionError('{0} declares no members'.format(name))
    return members


def _top_level_keys(body):
    """Return the keys declared at the shallowest indentation.

    The shallowest-indent rule is what distinguishes a shape's own keys
    from the keys of anything nested inside it, and it holds for both
    declaration styles the client uses - ``z.object({`` on the
    declaration line, which puts keys at two spaces, and the
    ``z\\n  .object({`` form used where a refinement follows, which puts
    them at four.

    Lines whose first non-space character is ``*`` or ``/`` are skipped
    so that prose inside a doc comment cannot be read as a key.

    Args:
        body: Text of one object literal, interface body or schema
            declaration.

    Returns:
        The keys, in declaration order, without duplicates.

    Raises:
        AssertionError: No key-shaped line was found.
    """
    found = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('*') or stripped.startswith('/'):
            continue
        member = _MEMBER_LINE.match(line)
        if member is not None:
            found.append((len(member.group(1)), member.group(2)))
    if not found:
        raise AssertionError(
            'no declared keys found in:\n{0}'.format(body[:400])
        )
    shallowest = min(indent for indent, _ in found)
    keys, seen = [], set()
    for indent, key in found:
        if indent == shallowest and key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def schema_keys(source, name):
    """Return the keys of a client Zod object schema, in order.

    Args:
        source: Module text.
        name: Schema identifier.

    Returns:
        The declared keys as a tuple. For a schema declared with
        ``.extend({…})`` these are the ADDED keys only, which is what
        makes the base/extension split visible in the assertions.
    """
    return _top_level_keys(_declaration(source, 'const', name))


def interface_members(source, name):
    """Return an interface's members in order, following ``extends``.

    Args:
        source: Module text.
        name: Interface identifier.

    Returns:
        The inherited members followed by the interface's own, in
        declaration order - the order a reader of the wire payload sees.
    """
    body = _declaration(source, 'interface', name)
    members = _top_level_keys(body)
    base = re.search(
        r'interface {0} extends (\w+)'.format(re.escape(name)), body
    )
    if base is not None:
        members = interface_members(source, base.group(1)) + members
    return members


def mapper_keys(source, name):
    """Return the keys one client mapper produces, in order.

    Covers both mapper shapes in the client: an inbound mapper, which
    hands a single literal to ``Schema.parse({…})``, and an outbound
    one, which seeds ``const body: XWire = {…}`` and then conditionally
    assigns an optional field. The conditional assignments are collected
    too, so an optional wire key is not silently missing from the
    comparison.

    Args:
        source: Module text.
        name: Mapper identifier, e.g. ``'toRating'``.

    Returns:
        The produced keys as a tuple.

    Raises:
        AssertionError: The mapper produces no recognisable object
            literal.
    """
    body = _declaration(source, 'const', name)
    literal = _LITERAL_START.search(body)
    if literal is None:
        raise AssertionError(
            '{0} does not build an object literal this gate can read; '
            'it neither calls Schema.parse({{…}}) nor seeds a typed '
            'body'.format(name)
        )
    keys = list(_top_level_keys(body[literal.end():]))
    for conditional in re.findall(r'body\.(\w+)\s*=', body):
        if conditional not in keys:
            keys.append(conditional)
    return tuple(keys)


def to_snake_case(camel):
    """Convert a client field name to its wire form.

    Args:
        camel: A camelCase identifier, e.g. ``'vehicleListingId'``.

    Returns:
        The snake_case equivalent, e.g. ``'vehicle_listing_id'``.
    """
    return re.sub(r'([A-Z])', lambda m: '_' + m.group(1).lower(), camel)


def client_request_routes(source):
    """Return the method and path of every rating request the client makes.

    Each path is reduced to the server's own declared form: the shared
    ``RATINGS_PATH`` prefix is substituted, and every interpolated
    segment becomes ``{parameter_name}`` under the server's naming, so
    the result can be compared with a FastAPI route path directly.

    Args:
        source: Text of the client's rating service module.

    Returns:
        A tuple of ``(METHOD, path)`` pairs in source order.

    Raises:
        AssertionError: No request call was found.
    """
    prefix = string_constant(source, 'RATINGS_PATH')
    routes = []
    for method, expression in _REQUEST_CALL.findall(source):
        if expression == 'RATINGS_PATH':
            path = prefix
        else:
            path = expression.strip('`').replace('${RATINGS_PATH}', prefix)
            path = _PATH_PARAMETER.sub(
                lambda m: '{{{0}}}'.format(to_snake_case(m.group(1))), path
            )
        routes.append((method.upper(), path))
    if not routes:
        raise AssertionError(
            'no rating requests found in the client service module'
        )
    return tuple(routes)


def mounted_path(route):
    """Return the path one router route resolves to on the application.

    Matched by endpoint IDENTITY rather than by path, because the point
    of the lookup is to discover what prefix the route was mounted under
    and a path-based match would have to assume that prefix first.

    Args:
        route: One route from the rating router.

    Returns:
        The resolved path, including the mount prefix.

    Raises:
        AssertionError: The route is not mounted on the application at
            all, which would mean the router was never registered.
    """
    for candidate in app.routes:
        if getattr(candidate, 'endpoint', None) is route.endpoint:
            return candidate.path
    raise AssertionError(
        'the {0} route is declared on the rating router but is not '
        'mounted on the application'.format(route.path or '(root)')
    )


def server_mount_prefix():
    """Return the single prefix every rating route is mounted under.

    Derived from the application rather than read from a literal, so
    that the client's own resource path can be compared against
    something the SERVER decides. Comparing the client's prefix with
    itself - which is what building the expected paths out of the
    client's constant amounts to - would let a renamed resource pass.

    Returns:
        The mount prefix, e.g. ``'/api/ratings'``.

    Raises:
        AssertionError: The routes are not all mounted under one prefix.
    """
    prefixes = set()
    for route in ratings_router.routes:
        resolved = mounted_path(route)
        if route.path:
            assert resolved.endswith(route.path), (
                'route {0} resolves to {1}, which does not end with its '
                'declared path'.format(route.path, resolved)
            )
            prefixes.add(resolved[:len(resolved) - len(route.path)])
        else:
            prefixes.add(resolved)
    if len(prefixes) != 1:
        raise AssertionError(
            'the rating routes are mounted under more than one prefix: '
            '{0}'.format(sorted(prefixes))
        )
    return prefixes.pop()


def refusal_messages(exception):
    """Return one ``ValidationError``'s messages keyed by field.

    Args:
        exception: A pydantic ``ValidationError``.

    Returns:
        A dict mapping the last element of each error location to its
        message.
    """
    return {
        error['loc'][-1]: error['msg'] for error in exception.errors()
    }


# Read once at import. These files do not change while the suite runs,
# and reading them per test would trade a real cost for no isolation
# benefit: nothing here mutates them.
CLIENT_SCHEMA = read_client_source(SCHEMA_SOURCE)
CLIENT_USER_SCHEMA = read_client_source(USER_SCHEMA_SOURCE)
CLIENT_SERVICE = read_client_source(SERVICE_SOURCE)


class TestExtractionSelfCheck(unittest.TestCase):
    """The reader itself, before anything is compared with it.

    Every assertion in this module is only as good as the extraction it
    rests on, and the dangerous failure is not a crash but an empty or
    partial result that makes a comparison trivially true. These tests
    exist so that a client refactor which moves a declaration out of the
    shape the regexes recognise is reported HERE, as a broken gate,
    instead of quietly disarming the eleven classes below.
    """

    def test_every_declaration_this_gate_reads_was_found(self):
        """Each extractor returns a non-empty result."""
        extracted = {
            'RATING_MIN': numeric_constant(CLIENT_SCHEMA, 'RATING_MIN'),
            'RATING_MAX': numeric_constant(CLIENT_SCHEMA, 'RATING_MAX'),
            'REVIEW_MAX_LENGTH': numeric_constant(
                CLIENT_SCHEMA, 'REVIEW_MAX_LENGTH'
            ),
            'directions': enum_members(
                CLIENT_SCHEMA, 'RatingDirectionSchema'
            ),
            'statuses': enum_members(CLIENT_SCHEMA, 'ModerationStatusSchema'),
            'RatingSchema': schema_keys(CLIENT_SCHEMA, 'RatingSchema'),
            'RatingWire': interface_members(CLIENT_SERVICE, 'RatingWire'),
            'toRating': mapper_keys(CLIENT_SERVICE, 'toRating'),
            'routes': client_request_routes(CLIENT_SERVICE),
        }
        for name, value in extracted.items():
            with self.subTest(declaration=name):
                self.assertTrue(
                    value,
                    '{0} extracted as empty, which would make every '
                    'parity assertion built on it pass without '
                    'comparing anything'.format(name),
                )

    def test_a_missing_declaration_is_reported_not_ignored(self):
        """An absent declaration raises rather than returning nothing.

        The property that makes this gate trustworthy. Asserted against
        names that genuinely do not exist, so it holds for the real
        extractors rather than for a stubbed one.
        """
        with self.assertRaises(AssertionError):
            numeric_constant(CLIENT_SCHEMA, 'RATING_MIDPOINT')
        with self.assertRaises(AssertionError):
            schema_keys(CLIENT_SCHEMA, 'RatingRevisionSchema')
        with self.assertRaises(AssertionError):
            interface_members(CLIENT_SERVICE, 'RatingRevisionWire')
        with self.assertRaises(AssertionError):
            mapper_keys(CLIENT_SERVICE, 'toRatingRevision')
        with self.assertRaises(AssertionError):
            read_client_source('schema/rating-revision.ts')

    def test_a_name_that_prefixes_another_is_not_confused_with_it(self):
        """``toRating`` is not ``toRatingCreateWire``.

        The word boundary in ``_declaration`` is what prevents a mapper
        from being compared against a different mapper's key set, which
        would be a passing assertion about the wrong thing.
        """
        self.assertEqual(
            mapper_keys(CLIENT_SERVICE, 'toRating')[0],
            'id',
            'toRating should map a rating, whose first field is its id',
        )
        self.assertEqual(
            mapper_keys(CLIENT_SERVICE, 'toRatingCreateWire')[0],
            'transaction_id',
            'toRatingCreateWire should build a request body',
        )
        self.assertEqual(
            schema_keys(CLIENT_SCHEMA, 'ModeratedRatingSchema'),
            ('moderationStatus', 'moderationReason'),
            'ModeratedRatingSchema extends RatingSchema, so only its '
            'added keys should be read from its own declaration',
        )

    def test_camel_case_names_convert_to_the_wire_form(self):
        """The one transformation this module performs is correct."""
        for camel, wire in (
            ('id', 'id'),
            ('score', 'score'),
            ('rateeId', 'ratee_id'),
            ('isPublished', 'is_published'),
            ('alreadyRated', 'already_rated'),
            ('vehicleListingId', 'vehicle_listing_id'),
            ('moderationStatus', 'moderation_status'),
        ):
            with self.subTest(camel=camel):
                self.assertEqual(to_snake_case(camel), wire)


class TestRatingScaleParity(unittest.TestCase):
    """The 1..5 scale, on both sides and in the one place it is fixed.

    The scale is the most widely mirrored figure in the feature: the
    server bounds a submitted score by it, the client bounds the same
    value, the star control renders one option per point, and every
    reputation surface reads "out of 5". A silent disagreement here
    offers a user a score the server refuses.
    """

    def test_the_scale_matches_on_both_sides(self):
        """The client's bounds are the server's settings."""
        self.assertEqual(
            numeric_constant(CLIENT_SCHEMA, 'RATING_MIN'),
            settings.RATING_MIN,
        )
        self.assertEqual(
            numeric_constant(CLIENT_SCHEMA, 'RATING_MAX'),
            settings.RATING_MAX,
        )

    def test_the_scale_is_the_one_the_project_fixed(self):
        """1..5, the scale the success metric names.

        Pinned as literals as well as compared, because two sides
        agreeing on a wrong number is still wrong: the project's own KPI
        is "4.5/5 average rating from both buyers and sellers", and the
        client's five-option control cannot render any other scale
        without a code change on both sides.
        """
        self.assertEqual(settings.RATING_MIN, 1)
        self.assertEqual(settings.RATING_MAX, 5)

    def test_the_scale_is_tunable_but_never_incoherent(self):
        """The three bounds are DEFAULTED tunables, not immutable ones.

        This is the reason the parity assertions above exist at all. An
        earlier revision declared the three ``const=True``, which turned
        an operator's existing override into an import-time failure -
        and because ``settings = Settings()`` runs while the module is
        imported and every module imports it, that was a total outage
        rather than a warning. The plan specifies defaulted tunables, so
        each of the three is defaulted and overridable, and drift is
        prevented by the discipline this gate enforces rather than by
        refusing to start.

        What the server DOES refuse is an incoherent scale: the client
        is a separately built artefact and cannot discover a server
        environment variable, so a scale whose floor sits above its
        ceiling would leave the interface offering points the server
        answers with 422 for every one of them.
        """
        for name in ('RATING_MIN', 'RATING_MAX', 'RATING_REVIEW_MAX_LENGTH'):
            with self.subTest(setting=name):
                field = Settings.__fields__[name]
                self.assertNotEqual(
                    field.field_info.const,
                    True,
                    '{0} is a defaulted tunable; declaring it const '
                    'refuses an environment that already sets it'.format(
                        name
                    ),
                )
                self.assertFalse(
                    field.required,
                    '{0} must carry a default so no deployment has to '
                    'set it'.format(name),
                )
        with self.assertRaises(ValidationError):
            Settings(RATING_MIN=settings.RATING_MAX + 1)

    def test_the_server_enforces_the_scale_it_declares(self):
        """The bounds are applied, not merely stated.

        Asserted by submitting values because the server's score bounds
        live in a validator rather than on the field's declaration -
        ``Rating.score`` carries no ``ge``/``le`` - so comparing numbers
        alone would not prove the scale is enforced at all.
        """
        for score in (settings.RATING_MIN, settings.RATING_MAX):
            with self.subTest(score=score, expected='accepted'):
                self.assertEqual(
                    RatingCreate(
                        transaction_id=DEFAULT_TRANSACTION_ID, score=score
                    ).score,
                    score,
                )
        for score in (settings.RATING_MIN - 1, settings.RATING_MAX + 1):
            with self.subTest(score=score, expected='refused'):
                with self.assertRaises(ValidationError) as refusal:
                    RatingCreate(
                        transaction_id=DEFAULT_TRANSACTION_ID, score=score
                    )
                self.assertIn('score', refusal_messages(refusal.exception))

    def test_the_client_derives_the_scale_from_one_declaration(self):
        """Every client surface reads the scale from the schema.

        Parity is only meaningful if each side has a single source of
        truth. A component carrying its own ``5`` would keep satisfying
        the comparison above while contradicting the schema it renders,
        so the import is asserted rather than assumed.
        """
        for module in SCALE_CONSUMERS:
            with self.subTest(module=module):
                source = read_client_source(module)
                self.assertRegex(
                    source,
                    r"import \{[^}]*(RATING_MIN|RATING_MAX|"
                    r"REVIEW_MAX_LENGTH)[^}]*\} from '\.\./schema/rating'",
                    '{0} should take the scale from the schema rather '
                    'than restate it'.format(module),
                )


class TestReviewLimitParity(unittest.TestCase):
    """Two different limits on one field, mirrored in the right order.

    The review carries a SEMANTIC limit - the figure a character counter
    shows and a user is promised - and a RAW ceiling several times larger
    that exists only so an unbounded string never reaches the
    normaliser. Both sides must agree on both numbers, and on which one
    is measured against which text: the raw ceiling applies to the value
    as received, the semantic limit to the normalised value that is
    stored.
    """

    def test_the_semantic_limit_matches(self):
        """The counter's promise equals the server's rule."""
        self.assertEqual(
            numeric_constant(CLIENT_SCHEMA, 'REVIEW_MAX_LENGTH'),
            settings.RATING_REVIEW_MAX_LENGTH,
        )
        self.assertEqual(settings.RATING_REVIEW_MAX_LENGTH, 2000)

    def test_the_raw_ceiling_matches_and_is_derived_the_same_way(self):
        """Both sides compute the ceiling from the same factor.

        The derivation is asserted as well as the value. Two sides
        agreeing on 8000 today while one of them hard-codes it would
        drift the moment the semantic limit changed, which is exactly
        the failure this module exists to prevent.
        """
        self.assertEqual(
            numeric_constant(CLIENT_SCHEMA, 'REVIEW_RAW_LENGTH_FACTOR'),
            REVIEW_RAW_LENGTH_FACTOR,
        )
        self.assertEqual(
            REVIEW_RAW_MAX_LENGTH,
            REVIEW_RAW_LENGTH_FACTOR * settings.RATING_REVIEW_MAX_LENGTH,
        )
        derived = re.search(
            r'export const REVIEW_RAW_MAX_LENGTH\s*=\s*([^;]+);',
            CLIENT_SCHEMA,
        )
        self.assertIsNotNone(
            derived, 'the client should declare a raw review ceiling'
        )
        self.assertEqual(
            ' '.join(derived.group(1).split()),
            'REVIEW_RAW_LENGTH_FACTOR * REVIEW_MAX_LENGTH',
            'the client should derive its raw ceiling from the same '
            'factor and semantic limit the server does',
        )

    def test_the_server_applies_the_semantic_limit_to_stored_text(self):
        """The boundary, from the side that owns it."""
        limit = settings.RATING_REVIEW_MAX_LENGTH
        accepted = RatingCreate(
            transaction_id=DEFAULT_TRANSACTION_ID,
            score=4,
            review='y' * limit,
        )
        self.assertEqual(len(accepted.review), limit)
        with self.assertRaises(ValidationError) as refusal:
            RatingCreate(
                transaction_id=DEFAULT_TRANSACTION_ID,
                score=4,
                review='y' * (limit + 1),
            )
        self.assertEqual(
            refusal_messages(refusal.exception)['review'],
            'Review must be at most {0} characters'.format(limit),
        )

    def test_the_server_applies_the_raw_ceiling_before_normalising(self):
        """A value over the raw ceiling is refused as raw text.

        The distinction matters: the ceiling is a denial-of-service
        guard, so it must reject before any normalisation work happens,
        and its message must not claim the semantic limit was exceeded.
        """
        with self.assertRaises(ValidationError) as refusal:
            RatingCreate(
                transaction_id=DEFAULT_TRANSACTION_ID,
                score=4,
                review='y' * (REVIEW_RAW_MAX_LENGTH + 1),
            )
        self.assertIn(
            str(REVIEW_RAW_MAX_LENGTH),
            refusal_messages(refusal.exception)['review'],
        )

    def test_the_client_measures_the_limit_the_way_the_server_does(self):
        """Code points after composition, not UTF-16 code units.

        The server measures ``len(value)`` on NFC-composed text, which
        counts code points. Zod's own ``.max()`` counts UTF-16 code
        units, so a client using it would refuse a 1200-emoji review the
        server accepts. The client therefore has to express both bounds
        as refinements over its own length function, and that choice -
        not merely the number - is the thing that has to hold.
        """
        review_field = _declaration(CLIENT_SCHEMA, 'const', 'RatingSchema')
        self.assertIn(
            'boundedText(', review_field,
            'the client should bound a review through its code-point '
            'measure rather than through .max()',
        )
        self.assertIn(
            'REVIEW_MAX_LENGTH', review_field,
            'the client should bound a review by the shared limit',
        )
        measure = _declaration(CLIENT_SCHEMA, 'const', 'textLength')
        self.assertIn(
            "normalize('NFC')", measure,
            'the client should compose before measuring, as the server '
            'does',
        )


class TestBoundedIdentifierParity(unittest.TestCase):
    """The remaining shared numbers: identifiers and moderator prose."""

    def test_the_transaction_reference_bound_matches(self):
        """The client bounds a submitted reference as the server does.

        A client bound larger than the server's would let a user submit
        a rating that fails with 422 for a reason no interface
        explained; a smaller one would make a transaction whose ID is
        long unratable from this client at all.

        The client holds the number ONCE, as
        ``DOCUMENT_ID_MAX_LENGTH``, and derives the transaction bound
        from it - the two are the same bound because a transaction
        reference IS a document ID, and the client also reads a seller
        ID off a listing against the same constant. So the literal is
        compared against the server, and the alias is asserted to be an
        alias rather than a second number that could drift from it.
        """
        self.assertEqual(
            numeric_constant(CLIENT_SCHEMA, 'DOCUMENT_ID_MAX_LENGTH'),
            DOCUMENT_ID_MAX_LENGTH,
        )
        alias = re.search(
            r'^const TRANSACTION_ID_MAX_LENGTH\s*=\s*(.+);',
            CLIENT_SCHEMA,
            re.M,
        )
        self.assertIsNotNone(
            alias,
            'the client should declare a transaction reference bound',
        )
        self.assertEqual(
            alias.group(1).strip(),
            'DOCUMENT_ID_MAX_LENGTH',
            'the transaction bound should be the shared document-ID '
            'constant rather than a second literal to keep in step',
        )

    def test_the_rating_key_leaves_room_for_both_halves(self):
        """A rating's key is two escaped document IDs and a separator.

        Not mirrored on the client - it never composes the key - but
        asserted here because it is the reason the shared identifier
        bound is 128 rather than Firestore's own limit. The escape
        factor is part of the arithmetic: the separator is legal inside
        a document ID, so each half is percent-escaped to keep the
        composition injective and one character can become three.
        """
        self.assertEqual(
            RATING_ID_MAX_LENGTH,
            2 * RATING_ID_ESCAPE_FACTOR * DOCUMENT_ID_MAX_LENGTH + 1,
        )

    def test_the_moderation_reason_bound_matches(self):
        """A moderator's recorded reason is bounded identically.

        The value travels on a RESPONSE, so a client bound below the
        server's would report a contract failure to the administrator
        who had just written the reason.
        """
        self.assertEqual(
            numeric_constant(CLIENT_SCHEMA, 'MODERATION_REASON_MAX_LENGTH'),
            MODERATION_REASON_MAX_LENGTH,
        )
        self.assertEqual(MODERATION_REASON_MAX_LENGTH, 500)

    def test_the_server_enforces_the_identifier_grammar_it_shares(self):
        """The path-safety rule the client mirrors is real.

        The client refuses ``/``, control characters, the single and
        double period and the reserved ``__name__`` form before
        submitting. That is only worth mirroring if the server enforces
        the same grammar, so it is exercised here rather than trusted.
        """
        for reference in ('a/b', '.', '..', '__name__', 'a\x00b', 'a\x7fb'):
            with self.subTest(transaction_id=reference):
                with self.assertRaises(ValidationError):
                    RatingCreate(transaction_id=reference, score=4)
        for reference in ('a.b', 'a' * DOCUMENT_ID_MAX_LENGTH):
            with self.subTest(transaction_id=reference[:16] + '…'):
                self.assertEqual(
                    RatingCreate(
                        transaction_id=reference, score=4
                    ).transaction_id,
                    reference,
                )
        with self.assertRaises(ValidationError):
            RatingCreate(
                transaction_id='a' * (DOCUMENT_ID_MAX_LENGTH + 1), score=4
            )

    def test_the_client_mirrors_the_grammar_rather_than_approximating_it(self):
        """The client's guard encodes the same four prohibitions.

        Textual, because the rule is a regular expression on the server
        and a hand-written predicate on the client - the control range
        cannot be written into a JavaScript character class without
        tripping ``no-control-regex`` - so the two forms cannot be
        compared as patterns from this process. What can be asserted is
        that the client declares every prohibition the server's pattern
        encodes, and the direction of any residual difference: a client
        WIDER than the server costs a remote 422 on a value no
        legitimate caller sends, while a narrower one blocks a
        legitimate rating with no server involvement at all.
        """
        guard = (
            _declaration(CLIENT_SCHEMA, 'const', 'RESERVED_DOCUMENT_IDS')
            + _declaration(
                CLIENT_SCHEMA, 'const', 'RESERVED_DOCUMENT_ID_NAMESPACE'
            )
            + _declaration(CLIENT_SCHEMA, 'const', 'isPathSafeDocumentId')
        )
        for fragment, description in (
            (r'/^\.\.?$/', 'the single and double period'),
            (r'/^__.*__$/', "Firestore's reserved __name__ namespace"),
            ('0x2f', 'the path separator'),
            ('0x1f', 'the C0 control characters'),
            ('0x7f', 'DEL'),
            ('length === 0', 'the empty identifier'),
        ):
            with self.subTest(rule=description):
                self.assertIn(
                    fragment,
                    guard,
                    'the client should refuse {0}, as the server '
                    'does'.format(description),
                )


class TestEnumParity(unittest.TestCase):
    """The two closed sets, member for member and in order.

    An enumeration is the one kind of contract where a superset is as
    bad as a subset. A server value the client does not know refuses a
    valid payload; a client value the server does not know is a request
    that can only ever 422.
    """

    def test_the_rating_directions_match(self):
        """Both sides know exactly two directions, in one order."""
        self.assertEqual(
            enum_members(CLIENT_SCHEMA, 'RatingDirectionSchema'),
            tuple(member.value for member in RatingDirection),
        )
        self.assertEqual(
            tuple(member.value for member in RatingDirection),
            ('buyer_to_seller', 'seller_to_buyer'),
        )

    def test_the_moderation_states_match(self):
        """Both sides know exactly three states, in one order."""
        self.assertEqual(
            enum_members(CLIENT_SCHEMA, 'ModerationStatusSchema'),
            tuple(member.value for member in ModerationStatus),
        )
        self.assertEqual(
            tuple(member.value for member in ModerationStatus),
            ('pending', 'approved', 'rejected'),
        )

    def test_neither_enumeration_is_open(self):
        """A value outside the set is refused by the server.

        The client's ``z.enum`` refuses one by construction; this is the
        other half, so that "closed" is a property of the contract
        rather than of one implementation.
        """
        with self.assertRaises(ValidationError):
            ModerationUpdate(moderation_status='hidden')
        with self.assertRaises(ValidationError):
            EligibilityDecision(
                eligible=False,
                reason='refused',
                ratee_id=None,
                direction='sideways',
                already_rated=False,
            )


class TestFieldSetParity(unittest.TestCase):
    """Every shape's field set, across all three declarations of it.

    Each shape is declared three times: as a pydantic model on the
    server, as a TypeScript wire interface on the client, and as a Zod
    schema the client parses into. All three are compared, in ORDER,
    because a renamed or dropped field is the failure this feature is
    most exposed to - the client reads ``undefined`` and reports a
    contract error to a user who did nothing wrong.
    """

    def assert_shape(self, model, wire_interface, client_schema):
        """Assert one shape agrees across the stacks.

        Args:
            model: The server's pydantic model.
            wire_interface: Name of the client's wire interface.
            client_schema: Name of the client's Zod schema.
        """
        server_fields = tuple(model.__fields__)
        wire = interface_members(CLIENT_SERVICE, wire_interface)
        self.assertEqual(
            wire,
            server_fields,
            '{0} on the wire should carry exactly the fields {1} '
            'declares, in the same order'.format(
                wire_interface, model.__name__
            ),
        )
        parsed = schema_keys(CLIENT_SCHEMA, client_schema)
        self.assertEqual(
            tuple(to_snake_case(key) for key in parsed),
            server_fields,
            '{0} should parse exactly the fields {1} declares'.format(
                client_schema, model.__name__
            ),
        )

    def test_a_rating_response_matches(self):
        """The public projection of a rating: eleven fields."""
        self.assert_shape(RatingView, 'RatingWire', 'RatingSchema')

    def test_a_moderated_rating_response_matches(self):
        """The admin projection: the same eleven plus two."""
        wire = interface_members(CLIENT_SERVICE, 'ModeratedRatingWire')
        self.assertEqual(wire, tuple(ModeratedRatingView.__fields__))
        added = schema_keys(CLIENT_SCHEMA, 'ModeratedRatingSchema')
        self.assertEqual(
            tuple(to_snake_case(key) for key in added),
            tuple(
                field
                for field in ModeratedRatingView.__fields__
                if field not in RatingView.__fields__
            ),
        )

    def test_the_submission_request_matches(self):
        """Three keys, and the same three on both sides.

        The narrowest and most important shape in the feature: it is the
        only one a client composes, and every field it does NOT carry -
        ``ratee_id``, ``direction``, ``rater_id`` - is one the server
        derives instead of trusting.
        """
        self.assert_shape(RatingCreate, 'RatingCreateWire',
                          'RatingCreateSchema')
        self.assertEqual(
            tuple(RatingCreate.__fields__),
            ('transaction_id', 'score', 'review'),
        )
        for derived in ('ratee_id', 'direction', 'rater_id', 'is_published'):
            with self.subTest(field=derived):
                self.assertNotIn(derived, RatingCreate.__fields__)
                self.assertNotIn(
                    derived,
                    interface_members(CLIENT_SERVICE, 'RatingCreateWire'),
                )

    def test_the_aggregate_matches(self):
        """Two fields, whose pairing is a rule in its own right."""
        self.assert_shape(
            RatingAggregate, 'RatingAggregateWire', 'RatingAggregateSchema'
        )

    def test_the_eligibility_decision_matches(self):
        """Five fields, the interface's whole input for its gate."""
        self.assert_shape(
            EligibilityDecision,
            'EligibilityDecisionWire',
            'EligibilityDecisionSchema',
        )

    def test_the_user_ratings_envelope_matches(self):
        """Two fields, established from one server snapshot."""
        self.assert_shape(
            UserRatingsResponse,
            'UserRatingsResponseWire',
            'UserRatingsResponseSchema',
        )

    def test_the_moderation_request_matches(self):
        """A state and the policy reason that justifies it.

        No Zod schema mirrors this shape - the moderation surface is
        API-only in this release - so the comparison is between the
        server's model and the client's wire interface alone.
        """
        self.assertEqual(
            interface_members(CLIENT_SERVICE, 'ModerationWire'),
            tuple(ModerationUpdate.__fields__),
        )
        self.assertEqual(
            tuple(ModerationUpdate.__fields__),
            ('moderation_status', 'moderation_reason'),
        )

    def test_the_user_shape_carries_the_ratings_fields_on_both_sides(self):
        """The three fields this feature added to an existing entity.

        ``is_verified`` is the R1 gate and the other two are the
        denormalised aggregate F010-3 reads, so a client that did not
        mirror them could neither explain a refusal nor render a
        reputation.
        """
        client_fields = tuple(
            to_snake_case(key)
            for key in schema_keys(CLIENT_USER_SCHEMA, 'UserSchema')
        )
        self.assertEqual(client_fields, tuple(User.__fields__))
        for field in ('is_verified', 'rating_average', 'rating_count'):
            with self.subTest(field=field):
                self.assertIn(field, client_fields)


class TestMapperKeyParity(unittest.TestCase):
    """The re-casing layer, key by key.

    The client's mappers are the only place the two naming conventions
    meet. A mapper that forgot a field would leave it ``undefined`` on a
    domain object whose schema then reports it missing; one that invented
    a key would send the server a field it refuses. Both are caught by
    comparing the mapper's produced keys - read from its own object
    literal - with the server's field list, in order.
    """

    def assert_maps(self, mapper, model):
        """Assert one inbound mapper covers a model exactly.

        Args:
            mapper: Name of the client mapper.
            model: The server model it adapts.
        """
        self.assertEqual(
            tuple(
                to_snake_case(key)
                for key in mapper_keys(CLIENT_SERVICE, mapper)
            ),
            tuple(model.__fields__),
            '{0} should map every field of {1} and no others'.format(
                mapper, model.__name__
            ),
        )

    def test_the_inbound_mappers_cover_their_models(self):
        """Every response field is read, under its own name."""
        for mapper, model in (
            ('toRating', RatingView),
            ('toModeratedRating', ModeratedRatingView),
            ('toRatingAggregate', RatingAggregate),
            ('toEligibilityDecision', EligibilityDecision),
        ):
            with self.subTest(mapper=mapper):
                self.assert_maps(mapper, model)

    def test_the_envelope_mapper_covers_its_two_halves(self):
        """``items`` and ``aggregate``, neither dropped."""
        self.assert_maps('toUserRatingsResponse', UserRatingsResponse)

    def test_the_outbound_mappers_build_exactly_what_is_accepted(self):
        """A request body carries only fields the server declares.

        Including the optional ones: ``review`` and
        ``moderation_reason`` are assigned conditionally rather than in
        the literal, and a comparison that missed them would not notice
        an optional field being renamed.
        """
        for mapper, model in (
            ('toRatingCreateWire', RatingCreate),
            ('toModerationWire', ModerationUpdate),
        ):
            with self.subTest(mapper=mapper):
                self.assertEqual(
                    mapper_keys(CLIENT_SERVICE, mapper),
                    tuple(model.__fields__),
                )

    def test_no_mapper_carries_operational_state_into_a_public_view(self):
        """The public rating mapper reads no moderation field.

        Sentiment-neutral moderation is preserved partly by there being
        no mechanism to violate it: the mapper used by the three read
        endpoints has no moderation key at all, so a server change that
        started sending one could not leak it into a public projection.
        """
        public = mapper_keys(CLIENT_SERVICE, 'toRating')
        for withheld in ('moderationStatus', 'moderationReason'):
            with self.subTest(field=withheld):
                self.assertNotIn(withheld, public)
        self.assertNotIn('moderation_status', RatingView.__fields__)
        self.assertNotIn('moderation_reason', RatingView.__fields__)


class TestRouteParity(unittest.TestCase):
    """Six methods and six paths, on both sides of the call.

    The client cannot discover a route, so every one of these is written
    twice. This is also the only place the prefix is checked end to end:
    all three of the older routers in this codebase declare their own
    resource segment as well as receiving one at mount time and resolve
    at doubled paths such as ``/api/listings/listings``, which is
    precisely the defect the rating router was written to avoid.
    """

    #: The API segment of every route, fixed by the project's own REST
    #: convention (``/api/<resource>``) and carried by the client inside
    #: its configured base URL rather than in its request paths. Pinned
    #: as a literal here because it is the one part of the path that
    #: neither side derives from the other.
    API_SEGMENT = '/api'

    def resource_segment(self):
        """Return the resource path the SERVER mounts, without ``/api``.

        Returns:
            The segment a client's request path must begin with, e.g.
            ``'/ratings'``.
        """
        prefix = server_mount_prefix()
        self.assertTrue(
            prefix.startswith(self.API_SEGMENT),
            'the rating routes should be mounted under {0}, following '
            'the /api/<resource> convention; found {1}'.format(
                self.API_SEGMENT, prefix
            ),
        )
        return prefix[len(self.API_SEGMENT):]

    def test_the_client_addresses_the_resource_the_server_mounts(self):
        """The client's resource constant is the server's mount point.

        Asserted against a value read off the application, not off the
        client: an expectation built from the client's own constant would
        cancel out and a renamed resource would pass.
        """
        self.assertEqual(
            string_constant(CLIENT_SERVICE, 'RATINGS_PATH'),
            self.resource_segment(),
        )
        self.assertEqual(server_mount_prefix(), '/api/ratings')

    def test_the_client_calls_the_routes_the_server_declares(self):
        """Method and path agree, request by request.

        Compared as sorted sets rather than in source order: FastAPI
        matches routes in declaration order, but the order the client
        happens to declare its request functions in carries no meaning, and
        holding it would make this gate fail on a reordering that
        changed nothing.
        """
        segment = self.resource_segment()
        declared = sorted(
            (sorted(route.methods)[0], segment + route.path)
            for route in ratings_router.routes
        )
        self.assertEqual(
            sorted(client_request_routes(CLIENT_SERVICE)),
            declared,
            'each client request should address a declared route, with '
            'the resource segment the server mounts and the server\'s '
            'own parameter names',
        )

    def test_all_six_routes_exist_and_no_seventh_does(self):
        """The published surface is exactly six endpoints.

        Six rather than five: the aggregate-only read
        (``/user/{user_id}/aggregate``) is a separate route because a
        reputation badge needs the summary and nothing else, and serving
        it from the full read made a badge cost a paginated scan of the
        ratee's history. Both sides declare it, which is what this
        counts.
        """
        self.assertEqual(len(ratings_router.routes), 6)
        self.assertEqual(len(client_request_routes(CLIENT_SERVICE)), 6)

    def test_the_routes_resolve_under_one_api_prefix(self):
        """Mounted paths are not doubled.

        The client's own paths are relative to a configured base URL
        that carries the ``/api`` half, so what has to hold here is that
        the server mounts the resource once: ``/api/ratings`` rather
        than ``/api/ratings/ratings``.
        """
        segment = self.resource_segment()
        resolved = sorted(
            mounted_path(route) for route in ratings_router.routes
        )
        self.assertEqual(
            resolved,
            [
                '/api/ratings',
                '/api/ratings/eligibility/{transaction_id}',
                '/api/ratings/transaction/{transaction_id}',
                '/api/ratings/user/{user_id}',
                '/api/ratings/user/{user_id}/aggregate',
                '/api/ratings/{rating_id}/moderation',
            ],
        )
        for path in resolved:
            with self.subTest(path=path):
                self.assertNotIn(
                    segment + segment,
                    path,
                    'the rating router must not inherit the double '
                    'prefix the older routers carry',
                )

    def test_a_created_rating_is_reported_as_created(self):
        """The submission endpoint answers 201.

        Mirrored in the client's own documentation of the call and, more
        importantly, in what a caller may treat as success: a client
        checking for 200 would treat every successful submission as a
        failure.
        """
        created = [
            route
            for route in ratings_router.routes
            if 'POST' in route.methods
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].status_code, 201)


class TestPayloadParity(unittest.TestCase):
    """What is actually serialised, against what the client declares.

    The strongest form of parity available in this process: the field
    sets above are declarations, while these tests serialise a real
    model with pydantic and compare the resulting keys, and hand the
    client's own body shape to the real request model.
    """

    def rating_view(self, **overrides):
        """Build one public rating projection.

        Args:
            **overrides: Any field to replace.

        Returns:
            A ``RatingView``.
        """
        moment = utc_now()
        fields = {
            'id': '{0}_{1}'.format(DEFAULT_TRANSACTION_ID, DEFAULT_BUYER_ID),
            'transaction_id': DEFAULT_TRANSACTION_ID,
            'vehicle_listing_id': DEFAULT_LISTING_ID,
            'rater_id': DEFAULT_BUYER_ID,
            'ratee_id': DEFAULT_SELLER_ID,
            'direction': RatingDirection.BUYER_TO_SELLER.value,
            'score': 4,
            'review': None,
            'is_published': True,
            'created_at': moment,
            'updated_at': moment,
        }
        fields.update(overrides)
        return RatingView(**fields)

    def test_a_serialised_rating_carries_the_declared_wire_keys(self):
        """The bytes on the wire match the client's interface."""
        payload = json.loads(self.rating_view().json())
        self.assertEqual(
            tuple(payload),
            interface_members(CLIENT_SERVICE, 'RatingWire'),
        )

    def test_an_absent_value_is_sent_as_null_rather_than_omitted(self):
        """Every key is present on every response.

        The client's mappers deliberately do NOT coalesce with
        ``?? null``, so that a truncated or reshaped payload is reported
        as a named contract failure instead of being rendered as a
        confident "this rating carries no review". That design is only
        correct while the server always sends the key, which is what
        this asserts - on the model AND on the routes, since a
        ``response_model_exclude_none`` anywhere would silently break
        it.
        """
        payload = json.loads(self.rating_view(review=None).json())
        self.assertIn('review', payload)
        self.assertIsNone(payload['review'])
        empty = json.loads(RatingAggregate(average=None, count=0).json())
        self.assertEqual(empty, {'average': None, 'count': 0})
        for route in ratings_router.routes:
            with self.subTest(path=route.path):
                self.assertFalse(route.response_model_exclude_none)
                self.assertFalse(route.response_model_exclude_unset)

    def test_a_serialised_decision_carries_every_field_the_form_reads(self):
        """An ineligible decision still names all five fields."""
        payload = json.loads(
            EligibilityDecision(
                eligible=False,
                reason='Only verified users can submit ratings',
                ratee_id=None,
                direction=None,
                already_rated=False,
            ).json()
        )
        self.assertEqual(
            tuple(payload),
            interface_members(CLIENT_SERVICE, 'EligibilityDecisionWire'),
        )

    def test_a_serialised_envelope_carries_both_halves(self):
        """``items`` and ``aggregate`` travel together."""
        payload = json.loads(
            UserRatingsResponse(
                items=[self.rating_view()],
                aggregate=RatingAggregate(average=4.0, count=1),
            ).json()
        )
        self.assertEqual(
            tuple(payload),
            interface_members(CLIENT_SERVICE, 'UserRatingsResponseWire'),
        )
        self.assertEqual(
            tuple(payload['items'][0]),
            interface_members(CLIENT_SERVICE, 'RatingWire'),
        )
        self.assertEqual(
            tuple(payload['aggregate']),
            interface_members(CLIENT_SERVICE, 'RatingAggregateWire'),
        )

    def test_the_body_the_client_builds_is_the_body_the_server_accepts(self):
        """The client's outbound shape, handed to the real model.

        Built from the client's own declared wire members rather than
        from a hand-written dict, so this test follows a rename in
        either direction instead of hiding it.
        """
        members = interface_members(CLIENT_SERVICE, 'RatingCreateWire')
        values = {
            'transaction_id': DEFAULT_TRANSACTION_ID,
            'score': 4,
            'review': 'Straightforward sale, clear paperwork.',
        }
        body = {member: values[member] for member in members}
        submission = RatingCreate(**body)
        self.assertEqual(submission.transaction_id, DEFAULT_TRANSACTION_ID)
        self.assertEqual(submission.score, 4)
        self.assertEqual(submission.review, values['review'])

    def test_the_body_is_accepted_without_its_optional_field(self):
        """A submission with no review omits the key entirely.

        The client's builder assigns ``review`` only when one was
        written, so the server has to accept a two-key body. An explicit
        null and an absent key are different requests and this is the
        one the client sends.
        """
        submission = RatingCreate(
            **{'transaction_id': DEFAULT_TRANSACTION_ID, 'score': 5}
        )
        self.assertIsNone(submission.review)

    def test_the_moderation_body_the_client_builds_is_accepted(self):
        """Both of the client's moderation bodies parse.

        The one-key form for a state that displays the review, and the
        two-key form for a rejection, which the server requires a reason
        for.
        """
        update = ModerationUpdate(
            **{
                'moderation_status': ModerationStatus.REJECTED.value,
                'moderation_reason': 'contains a phone number',
            }
        )
        self.assertIs(update.moderation_status, ModerationStatus.REJECTED)
        self.assertEqual(update.moderation_reason, 'contains a phone number')
        approved = ModerationUpdate(
            **{'moderation_status': ModerationStatus.APPROVED.value}
        )
        self.assertIs(approved.moderation_status, ModerationStatus.APPROVED)
        self.assertIsNone(approved.moderation_reason)

    def test_a_spoofed_field_cannot_reach_the_server_from_either_side(self):
        """The two stacks refuse a forged claim differently, and both refuse.

        The client STRIPS an unrecognised key, so it can never be sent;
        the server REFUSES one, except for the counterparty claim it
        tolerates specifically so that a mismatch can be reported rather
        than ignored. Recorded here as a deliberate asymmetry, with both
        halves asserted, because "make them the same" would be the wrong
        fix in either direction.
        """
        with self.assertRaises(ValidationError) as refusal:
            RatingCreate(
                transaction_id=DEFAULT_TRANSACTION_ID,
                score=4,
                direction=RatingDirection.SELLER_TO_BUYER.value,
            )
        self.assertIn(
            'direction', str(refusal.exception)
        )
        tolerated = RatingCreate(
            **{
                'transaction_id': DEFAULT_TRANSACTION_ID,
                'score': 4,
                RATEE_ID_CLAIM: DEFAULT_SELLER_ID,
            }
        )
        self.assertEqual(
            getattr(tolerated, RATEE_ID_CLAIM), DEFAULT_SELLER_ID
        )
        client_schema = _declaration(
            CLIENT_SCHEMA, 'const', 'RatingCreateSchema'
        )
        self.assertNotIn(
            'rateeId', client_schema,
            'the client should not declare a counterparty key at all, '
            'so a forged one is stripped before any request is built',
        )


class TestAggregateInvariantParity(unittest.TestCase):
    """The rule that spans two fields, held identically on both sides.

    ``rating_average`` and ``rating_count`` are meaningless apart: a
    count with no average, or an average with no count, is a payload the
    interface would render as a real reputation. Both sides refuse both
    combinations, and - unusually - both use the same sentence to say so,
    which is worth holding because the client shows that sentence to a
    developer reading a contract failure the server produced.
    """

    def test_a_count_without_an_average_is_refused_by_the_server(self):
        """The first impossible pairing."""
        with self.assertRaises(ValidationError) as refusal:
            RatingAggregate(average=None, count=3)
        self.assertEqual(
            refusal_messages(refusal.exception)['count'],
            'A positive rating count requires an average',
        )

    def test_an_average_without_a_count_is_refused_by_the_server(self):
        """The second impossible pairing."""
        with self.assertRaises(ValidationError) as refusal:
            RatingAggregate(average=4.0, count=0)
        self.assertEqual(
            refusal_messages(refusal.exception)['count'],
            'An average requires a positive rating count',
        )

    def test_the_client_refuses_the_same_two_pairings_in_the_same_words(self):
        """The client declares both rules, worded identically.

        Textual because the rule is a pydantic validator on one side and
        a Zod ``superRefine`` on the other; the sentences are what can be
        compared, and they are the sentences a reader sees.
        """
        declared = _declaration(
            CLIENT_SCHEMA, 'const', 'RatingAggregateSchema'
        )
        self.assertIn('superRefine', declared)
        for sentence in (
            'A positive rating count requires an average',
            'An average requires a positive rating count',
        ):
            with self.subTest(rule=sentence):
                self.assertIn(sentence, declared)

    def test_both_sides_bound_the_average_to_the_shared_scale(self):
        """An average outside 1..5 is not a reputation.

        The numbers are compared rather than the wording, which differs
        by design: the server says "Average must be between 1 and 5"
        while the client writes a message per bound.
        """
        for average in (
            settings.RATING_MIN - 0.5, settings.RATING_MAX + 0.5
        ):
            with self.subTest(average=average):
                with self.assertRaises(ValidationError):
                    RatingAggregate(average=average, count=1)
        for average in (float(settings.RATING_MIN),
                        float(settings.RATING_MAX)):
            with self.subTest(average=average):
                self.assertEqual(
                    RatingAggregate(average=average, count=1).average,
                    average,
                )
        client = _declaration(CLIENT_SCHEMA, 'const', 'RatingAggregateSchema')
        self.assertIn('.min(RATING_MIN', client)
        self.assertIn('.max(RATING_MAX', client)

    def test_the_empty_reputation_is_the_same_pair_on_both_sides(self):
        """No ratings yet is ``null`` and ``0``, never ``0`` and ``0``.

        A zero average would render as an earned one-star reputation for
        somebody who has simply never been rated, so the pair that means
        "unrated" has to be identical on both sides.
        """
        empty = RatingAggregate(average=None, count=0)
        self.assertIsNone(empty.average)
        self.assertEqual(empty.count, 0)
        unrated = build_user(rating_average=None, rating_count=0)
        self.assertIsNone(unrated.rating_average)
        self.assertEqual(unrated.rating_count, 0)
        self.assertIn(
            'nullable()',
            _declaration(CLIENT_SCHEMA, 'const', 'RatingAggregateSchema'),
        )


class TestEligibilityDecisionConformance(unittest.TestCase):
    """Every decision the server emits, against the client's own rules.

    The client is deliberately STRICTER here than the server: its
    ``EligibilityDecisionSchema`` refuses a decision that cannot exist -
    eligible without a named counterparty, eligible while already rated,
    ineligible with no stated reason - because the submission form
    cannot recover from a malformed decision and would disable its
    control with no explanation.

    Comparing the two models would say nothing about the risk that
    creates, which is that the server emits a shape the client refuses.
    So these tests drive the REAL service through every one of its return
    paths and assert the decision it produces satisfies the client's
    rules. The rules are stated once, as the client states them, and
    every path is checked against all of them.
    """

    def setUp(self):
        """Seed the eligible cast: two verified parties and their sale."""
        super().setUp()
        self.buyer = seed_user(role='buyer')
        self.seller = seed_user(
            user_id=DEFAULT_SELLER_ID, role='seller'
        )
        self.transaction = seed_transaction(completed_transaction())

    def assert_client_would_accept(self, decision, path):
        """Assert one decision satisfies the client's cross-field rules.

        Args:
            decision: The ``EligibilityDecision`` the service returned.
            path: Human-readable name of the branch that produced it,
                used to name a failure.
        """
        if decision.eligible:
            self.assertIsNotNone(
                decision.ratee_id,
                '{0}: an eligible decision must name the counterparty, '
                'or the client refuses the payload'.format(path),
            )
            self.assertIsNotNone(
                decision.direction,
                '{0}: an eligible decision must carry the '
                'direction'.format(path),
            )
            self.assertFalse(
                decision.already_rated,
                '{0}: a caller who has already rated cannot be '
                'eligible'.format(path),
            )
        else:
            self.assertIsNotNone(
                decision.reason,
                '{0}: an ineligible decision must explain why, or the '
                'client disables its control with no explanation'.format(
                    path
                ),
            )
            self.assertTrue(
                decision.reason.strip(),
                '{0}: the reason must be a sentence a person can '
                'read'.format(path),
            )
        if decision.direction is not None:
            self.assertIn(
                decision.direction,
                enum_members(CLIENT_SCHEMA, 'RatingDirectionSchema'),
                '{0}: the direction must be one the client knows'.format(
                    path
                ),
            )

    def corrupt_the_seeded_transaction(self):
        """Remove the listing reference every rating denormalises.

        The one branch that cannot be reached by an argument: the
        request is well formed and the caller authorized, but the STORED
        record is missing a field ``Transaction`` declares. The service
        raises for it on the write path and REPORTS it here, which is
        the return site this arranges.
        """
        stored = dict(
            fake_db.document_body(
                TRANSACTIONS_COLLECTION, DEFAULT_TRANSACTION_ID
            )
        )
        stored.pop('vehicle_listing_id', None)
        fake_db.seed(TRANSACTIONS_COLLECTION, DEFAULT_TRANSACTION_ID, stored)

    def decisions_from_every_return_path(self):
        """Return one decision from each branch, with its branch name.

        Each case re-seeds only the document it needs, so the decision
        it produces is attributable to the branch under test rather than
        to some other condition the arrangement also happened to fail.

        Returns:
            A list of ``(branch, EligibilityDecision)`` pairs.
        """
        outsider = build_user(user_id=DEFAULT_OUTSIDER_ID, role='buyer')
        unverified = build_user(is_verified=False)
        collected = [
            (
                'eligible',
                evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.buyer),
            ),
            (
                'unverified caller',
                evaluate_eligibility(DEFAULT_TRANSACTION_ID, unverified),
            ),
            (
                'reference that cannot name a document',
                evaluate_eligibility('not/a/document', self.buyer),
            ),
            (
                'unknown transaction',
                evaluate_eligibility('test-transaction-999999', self.buyer),
            ),
            (
                'caller is not a party',
                evaluate_eligibility(DEFAULT_TRANSACTION_ID, outsider),
            ),
        ]

        # Ordered so that each re-seed only ever moves the arrangement
        # FORWARD, which is why no reset is needed between branches: a
        # non-completed transaction and a corrupt record are both refused
        # by a guard that runs before the duplicate probe, so the rating
        # seeded for the last case cannot influence either of them.
        seed_transaction(pending_transaction())
        collected.append((
            'transaction not completed',
            evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.buyer),
        ))

        seed_transaction(completed_transaction())
        self.corrupt_the_seeded_transaction()
        collected.append((
            'transaction record missing its listing',
            evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.buyer),
        ))

        seed_transaction(completed_transaction())
        seed_rating()
        collected.append((
            'already rated',
            evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.buyer),
        ))
        return collected

    def test_every_decision_the_service_emits_satisfies_the_client(self):
        """All eight reachable decisions, against the client's contract.

        The gate M10 asks for, in the only direction that carries risk.
        If the service ever starts reporting an eligible decision with no
        counterparty, or a refusal with no reason, the client's schema
        refuses the payload and the submission form is left with nothing
        to render - and no existing suite would have noticed, because the
        server's own model permits both shapes.
        """
        decisions = self.decisions_from_every_return_path()
        self.assertEqual(
            len(decisions),
            8,
            'every return path of evaluate_eligibility should be '
            'represented, so a new one is noticed here',
        )
        eligible = [
            branch for branch, decision in decisions if decision.eligible
        ]
        self.assertEqual(
            eligible,
            ['eligible'],
            'exactly one arranged branch should report eligibility',
        )
        for branch, decision in decisions:
            with self.subTest(branch=branch):
                self.assert_client_would_accept(decision, branch)

    def test_the_clients_declared_invariants_are_the_ones_asserted_here(self):
        """The client's rules, extracted, against the four encoded above.

        ``assert_client_would_accept`` restates the client's rules in
        Python because a Zod ``superRefine`` cannot be executed from this
        process. Restating a rule is only safe while it stays in step
        with the original, so the client's four refusal messages are
        extracted and pinned: adding a fifth rule on the client fails
        HERE, which is what forces the conformance assertions to be
        brought up to date rather than silently covering three of four.
        """
        declared = re.findall(
            r"message:\s*'([^']*)'",
            _declaration(CLIENT_SCHEMA, 'const', 'EligibilityDecisionSchema'),
        )
        self.assertEqual(
            declared,
            [
                'An eligible decision must name the counterparty being '
                'rated',
                'An eligible decision must carry the rating direction',
                'A caller who has already rated this transaction cannot '
                'be eligible',
                'An ineligible decision must explain why',
            ],
        )

    def test_the_eligible_decision_carries_a_direction_the_client_knows(self):
        """The one shape the form acts on positively."""
        decision = evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.buyer)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.ratee_id, DEFAULT_SELLER_ID)
        self.assertEqual(
            decision.direction, RatingDirection.BUYER_TO_SELLER.value
        )
        self.assertIn(
            decision.direction,
            enum_members(CLIENT_SCHEMA, 'RatingDirectionSchema'),
        )

    def test_the_reverse_direction_is_also_one_the_client_knows(self):
        """R0: the seller's decision travels the other way."""
        decision = evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.seller)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.ratee_id, DEFAULT_BUYER_ID)
        self.assertEqual(
            decision.direction, RatingDirection.SELLER_TO_BUYER.value
        )
        self.assertIn(
            decision.direction,
            enum_members(CLIENT_SCHEMA, 'RatingDirectionSchema'),
        )

    def test_a_serialised_decision_is_what_the_client_parses(self):
        """The emitted payload, key for key.

        Closes the loop: the shape is the client's declared wire
        interface, and the values within it satisfy the client's
        cross-field rules.
        """
        decision = evaluate_eligibility(DEFAULT_TRANSACTION_ID, self.buyer)
        payload = json.loads(decision.json())
        self.assertEqual(
            tuple(payload),
            interface_members(CLIENT_SERVICE, 'EligibilityDecisionWire'),
        )
        self.assert_client_would_accept(decision, 'serialised eligible')


class TestRatingDocumentParity(unittest.TestCase):
    """The stored shape, against the projection the client receives.

    The persisted rating carries two fields the public projection does
    not, and that difference is the whole of "moderation before
    display". Asserted here so that a field added to the document does
    not silently reach a public response, and so that the projection
    cannot quietly lose one the client depends on.
    """

    def test_the_public_projection_withholds_only_moderation_state(self):
        """Eleven of the document's thirteen fields are public."""
        withheld = tuple(
            field
            for field in Rating.__fields__
            if field not in RatingView.__fields__
        )
        self.assertEqual(withheld, ('moderation_status', 'moderation_reason'))

    def test_every_public_field_is_declared_by_the_client(self):
        """No stored field reaches the client unnamed."""
        wire = interface_members(CLIENT_SERVICE, 'RatingWire')
        for field in RatingView.__fields__:
            with self.subTest(field=field):
                self.assertIn(field, wire)

    def test_the_admin_projection_adds_exactly_the_withheld_pair(self):
        """The moderation response is the document, in full."""
        self.assertEqual(
            tuple(ModeratedRatingView.__fields__),
            tuple(RatingView.__fields__)
            + ('moderation_status', 'moderation_reason'),
        )
        self.assertEqual(
            set(ModeratedRatingView.__fields__),
            set(Rating.__fields__),
        )


if __name__ == '__main__':
    unittest.main()

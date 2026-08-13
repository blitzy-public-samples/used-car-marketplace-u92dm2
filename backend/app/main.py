from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from app.api.auth import auth_router
# Every router is imported UNGUARDED, and that is the contract of this
# module: the composition root either assembles the whole API or it fails
# where it broke.
#
# An earlier revision wrapped the three marketplace imports in
# ``except Exception`` and substituted an empty ``APIRouter()`` so that a
# module which could not import cost only its own surface. That is worse
# than the failure it hid. The application came up reporting success while
# ``/api/listings``, ``/api/transactions`` and ``/api/messages`` served
# nothing at all: every request to a real endpoint answered 404, no
# readiness signal was false, and the only trace was one warning in a log
# nobody reads until a customer complains. Silently trading three delivered
# domains for a clean startup is not degradation, it is data loss with a
# green light on it. Failing at import is the correct response, because it
# is the only one an operator cannot miss.
#
# So the substitution is gone, and the two defects that made it look
# necessary are fixed at their source instead: ``listings``, ``messages``
# and ``transactions`` now import the ``User`` they annotate with (Python
# evaluates annotations at function-definition time, so the missing name
# was a NameError raised during import), and ``app/services/payment.py``
# configures stripe the way stripe 7.x is configured rather than importing
# a ``Stripe`` class the library does not export. Both were one-line
# repairs that change no path, no signature and no response.
#
# The three modules expose their router object as ``router`` rather than
# ``<domain>_router``, so each is bound through an alias.
from app.api.listings import router as listings_router
from app.api.transactions import router as transactions_router
from app.api.messages import router as messages_router
from app.api.ratings import RATINGS_PREFIX
from app.api.ratings import router as ratings_router
from app.api.ratings import validation_failure_handler
from app.core.config import settings
from app.db.firestore import initialize_db

# No initializer is imported from ``app.services.ai_vision`` or
# ``app.services.document_processing``, and none is guarded for either.
# Neither module defines one - they construct their Google Cloud clients at
# module import instead - so an ``except ImportError`` here would always
# take its fallback branch and bind an awaitable no-op to a name that never
# existed, keeping an invalid startup contract alive while reporting
# success. Removing the imports outright is both narrower and safer than
# any guard: nothing is absorbed, so a genuine fault in either module - a
# missing credential, an unresolvable dependency, a syntax error - still
# propagates from wherever it is really imported instead of being
# reinterpreted here as a disabled subsystem. The lifespan handler below
# therefore awaits exactly what this application initializes.


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Run what this application initializes, and nothing else.

    A ``lifespan`` context manager rather than the ``@app.on_event``
    hooks this used to declare. Those are deprecated by the framework -
    the pinned FastAPI release warns on each one at import - and the
    replacement is not merely a rename: startup and shutdown are one
    scope here, so what is set up and what is torn down are written
    beside each other instead of in two functions that can drift.

    Everything before the ``yield`` happens before the application
    serves a request; everything after it happens once it has stopped.

    Args:
        application: The application being started. Unused: this
            application initializes a module-level datastore client
            rather than anything attached to the app object, and binding
            it to the app's state would invent a second way to reach the
            same client. It is in the signature because the framework
            passes it.

    Yields:
        Control to the framework, for the lifetime of the application.
    """
    # The one initializer this application actually defines.
    # ``app.services.ai_vision`` and ``app.services.document_processing``
    # expose none - they construct their Google Cloud clients at module
    # import instead - so the two awaits that used to be here named
    # functions that never existed. Binding those names to no-op
    # coroutines would have kept an invalid startup contract alive while
    # reporting success; the contract is removed instead, so what starts
    # up is exactly what this application initializes.
    await initialize_db()
    yield
    # Nothing to release, stated rather than implied. The previous
    # shutdown hook was an empty body under comments about closing
    # database connections and freeing AI model resources, neither of
    # which this application owns: the Firestore client is a module-level
    # singleton in ``app/db/firestore.py`` shared with the task module,
    # so closing it here would tear down a collaborator this scope does
    # not own, and no model is loaded in-process. A future resource that
    # DOES belong to the application's lifetime is released here.


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)

app.include_router(auth_router, prefix='/api/auth', tags=['Authentication'])
app.include_router(listings_router, prefix='/api/listings', tags=['Listings'])
app.include_router(
    transactions_router,
    prefix='/api/transactions',
    tags=['Transactions'],
)
app.include_router(messages_router, prefix='/api/messages', tags=['Messages'])
# The prefix comes from the router's own module, so the mount and the
# scope of the validation handler registered below cannot disagree about
# which paths this feature serves.
app.include_router(ratings_router, prefix=RATINGS_PREFIX, tags=['Ratings'])

# One error envelope for the ratings API, including the 422 the framework
# raises for itself.
#
# An exception handler is registered per APPLICATION, which is why this
# line is here and not in the router module. Without it a ratings request
# refused by validation answers `{"detail": [{...}]}` while every other
# refusal from the same API answers `{"detail": "<sentence>"}` - two
# incompatible bodies under one status code, only one of which can be
# declared in the OpenAPI document. The handler renders validation
# failures into the declared envelope, summarising the field problems
# into the sentence and keeping them verbatim beside it.
#
# It is scoped to the ratings paths by the handler itself: every other
# path is delegated to the framework's own handler, unchanged. The
# listings, transactions and messages routers publish 422 as
# `HTTPValidationError` and are not part of this feature, so reshaping
# their bodies while leaving their generated schema in place would
# recreate, for them, exactly the mismatch this closes.
app.add_exception_handler(
    RequestValidationError,
    validation_failure_handler,
)

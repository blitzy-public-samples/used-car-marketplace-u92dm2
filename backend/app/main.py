from fastapi import FastAPI
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
from app.api.ratings import router as ratings_router
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
# reinterpreted here as a disabled subsystem. The startup hook below
# therefore awaits exactly what this application initializes.

app = FastAPI()

@app.on_event('startup')
async def startup_event():
    # Awaits the one initializer this application actually defines.
    # ``app.services.ai_vision`` and ``app.services.document_processing``
    # expose no initializer - they construct their Google Cloud clients
    # at module import instead - so the two awaits that used to be here
    # named functions that never existed. Binding those names to no-op
    # coroutines would have kept an invalid startup contract alive while
    # reporting success; the contract is removed instead, so what starts
    # up is exactly what this application initializes.
    await initialize_db()

@app.on_event('shutdown')
async def shutdown_event():
    # Close database connections
    # Release AI model resources
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)

app.include_router(auth_router, prefix='/api/auth', tags=['Authentication'])
app.include_router(listings_router, prefix='/api/listings', tags=['Listings'])
app.include_router(transactions_router, prefix='/api/transactions', tags=['Transactions'])
app.include_router(messages_router, prefix='/api/messages', tags=['Messages'])
app.include_router(ratings_router, prefix='/api/ratings', tags=['Ratings'])

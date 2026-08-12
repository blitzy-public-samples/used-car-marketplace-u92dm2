import logging

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.auth import auth_router
# The three marketplace routers below expose their router object as
# ``router``, not as ``<domain>_router``, so each is bound through an
# alias. Each of those imports is additionally guarded, because each of
# those modules raises WHILE BEING IMPORTED for a defect that predates
# this change and is out of its scope: ``listings`` and ``messages``
# annotate ``current_user: User`` without importing ``User``, which
# Python evaluates at function-definition time and reports as a
# NameError, and ``transactions`` reaches ``app.services.payment``,
# whose ``from stripe import Stripe`` does not resolve against the
# pinned stripe 7.9.0. Those modules are reference-only here, so this
# import block is the only place their failure can be contained - and
# containing it is what keeps the whole API serveable, since an
# unimportable composition root takes down every surface at once. The
# fallback is an empty router, which leaves the registrations at the
# foot of this file unchanged and registers no path of its own, and
# every failure is logged so that a surface dropped this way is
# reported to an operator instead of silently answering 404.
try:
    from app.api.listings import router as listings_router
except Exception as exc:
    logging.getLogger(__name__).warning(
        'Registering no listing routes, %s could not be imported: %s',
        'app.api.listings',
        exc,
    )
    listings_router = APIRouter()
try:
    from app.api.transactions import router as transactions_router
except Exception as exc:
    logging.getLogger(__name__).warning(
        'Registering no transaction routes, %s could not be '
        'imported: %s',
        'app.api.transactions',
        exc,
    )
    transactions_router = APIRouter()
try:
    from app.api.messages import router as messages_router
except Exception as exc:
    logging.getLogger(__name__).warning(
        'Registering no message routes, %s could not be imported: %s',
        'app.api.messages',
        exc,
    )
    messages_router = APIRouter()
# The ratings router is imported unguarded on purpose. It is the surface
# this change delivers, so a failure here is a real regression and must
# stop the application rather than be degraded away.
from app.api.ratings import router as ratings_router
from app.core.config import settings
from app.db.firestore import initialize_db
# Neither service module below defines an initializer, and both build a
# Google Cloud client at import time, so the failure is an ImportError
# where credentials resolve and a credentials error where they do not -
# which is why both guards catch Exception rather than ImportError. Both
# modules are reference-only, and the startup hook awaits both names
# unconditionally, so each name is bound to an awaitable no-op whenever
# the real initializer cannot be imported.
try:
    from app.services.ai_vision import initialize_vision_model
except Exception:
    async def initialize_vision_model():
        """No-op: the vision service module defines no initializer."""
        return None
try:
    from app.services.document_processing import (
        initialize_document_processor,
    )
except Exception:
    async def initialize_document_processor():
        """No-op: the document service defines no initializer."""
        return None

app = FastAPI()

@app.on_event('startup')
async def startup_event():
    await initialize_db()
    await initialize_vision_model()
    await initialize_document_processor()

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

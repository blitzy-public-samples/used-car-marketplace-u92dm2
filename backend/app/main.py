from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
# Each api module exports its APIRouter as `router`; alias on import so the
# names used below stay unchanged. Importing `auth_router` etc. directly raised
# ImportError: cannot import name 'auth_router' on every start.
from app.api.auth import router as auth_router
from app.api.listings import router as listings_router
from app.api.transactions import router as transactions_router
from app.api.messages import router as messages_router
from app.core.config import settings

logger = logging.getLogger(__name__)

app = FastAPI()

@app.on_event('startup')
async def startup_event():
    # HUMAN DECISION RECORDED: initialize_db / initialize_vision_model /
    # initialize_document_processor were imported and awaited here but are
    # defined nowhere in the repository. The Firestore client
    # (app/db/firestore.py) and the Cloud Vision client
    # (app/services/ai_vision.py) are already constructed at module import, and
    # app/services/document_processing.py needs no client, so there is no
    # remaining work for an initializer. The awaits are therefore removed
    # rather than stubbed. Reinstate real initializers here if lazy or
    # health-checked startup is wanted.
    logger.info('startup complete: firestore and vision clients initialised at import')

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
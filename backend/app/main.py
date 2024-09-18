from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.auth import auth_router
from app.api.listings import listings_router
from app.api.transactions import transactions_router
from app.api.messages import messages_router
from app.core.config import settings
from app.db.firestore import initialize_db
from app.services.ai_vision import initialize_vision_model
from app.services.document_processing import initialize_document_processor

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
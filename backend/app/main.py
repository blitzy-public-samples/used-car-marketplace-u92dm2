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

# Every attribute a LogRecord carries by construction, read from the running
# interpreter so that a version which adds one -- 3.12 added taskName -- needs
# no edit here. Anything on a record outside this set arrived through `extra`
# and is request context worth printing.
_LOG_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord('', 0, '', 0, '', None, None))
) | {'asctime', 'message'}
# Every module of this application names its logger with
# logging.getLogger(__name__), so they all sit under this one namespace.
_APP_LOGGER_NAME = 'app'


def _escape_log_context(value):
    """Flatten one context value onto a single line.

    A context field can carry text a request influenced, and a newline inside
    a log line is a forged log line, so control characters are escaped rather
    than emitted.
    """
    text = str(value)
    for character, replacement in (
        ('\\', '\\\\'), ('\n', '\\n'), ('\r', '\\r'), ('\t', '\\t'),
    ):
        text = text.replace(character, replacement)
    return text


class ContextFormatter(logging.Formatter):
    """Formatter that also renders whatever was passed through ``extra``.

    A format string can only render a field it names, and raises KeyError on a
    record that lacks it -- so correlation_id, attached by the listing handler
    and absent from every other record, cannot be rendered that way.
    Appending the fields a record carries beyond the standard set renders
    `extra` without coupling the format string to any one key, which is what
    makes logger.exception('photo analysis failed', extra={...}) traceable
    back to the request that produced it.
    """

    def formatMessage(self, record):
        # formatMessage rather than format, so the context joins the message
        # line instead of trailing a traceback, where it would read as part of
        # the exception.
        formatted = super().formatMessage(record)
        context = ' '.join(
            '%s=%s' % (key, _escape_log_context(value))
            for key, value in sorted(vars(record).items())
            if key not in _LOG_RECORD_FIELDS
        )
        if context:
            return '%s [%s]' % (formatted, context)
        return formatted


def _configure_application_logging():
    """Give this application's own loggers a level, a handler and a format.

    Uvicorn configures only its `uvicorn*` loggers, so every `app.*` logger
    inherited root's WARNING with no handler attached: the startup record
    below was created and then discarded, and a photo-analysis failure
    reached logging.lastResort -- printed without its correlation id and
    without even a level name. Configuring the namespace once, here in the
    composition root, is what makes both observable.

    A namespace an operator has already configured is left exactly as it is.
    """
    app_logger = logging.getLogger(_APP_LOGGER_NAME)
    if app_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(ContextFormatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s'
    ))
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    # Handled here, so neither a root handler nor logging.lastResort can print
    # these records a second time.
    app_logger.propagate = False


_configure_application_logging()

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
    logger.info(
        'startup complete: firestore and vision clients initialised at import'
    )

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
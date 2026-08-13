from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging
# Each api module exports its APIRouter as `router`; alias on import so the
# names used below stay unchanged. Importing `auth_router` etc. directly raised
# ImportError: cannot import name 'auth_router' on every start.
from app.api.auth import router as auth_router
from app.api.listings import router as listings_router
from app.api.transactions import router as transactions_router
from app.api.messages import router as messages_router
from app.core.config import settings

# Uvicorn configures only its own `uvicorn*` loggers, so without this line the
# `app.*` loggers inherit root's WARNING with no handler: the startup record
# below is created and then discarded, and a guarded photo-analysis failure
# reaches logging.lastResort without even a level name. basicConfig is a no-op
# when the root logger already has a handler, so an operator who configures
# logging before importing this module keeps their own configuration.
logging.basicConfig(level=logging.INFO)

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

# --- Response headers this application owns --------------------------------
# The application set no response header of its own, so four things were true
# at once: nothing told a browser not to sniff a content type, nothing refused
# framing, nothing bounded the referrer, and -- the one that matters most here
# -- nothing marked a token-bearing response unstorable, so a shared cache or
# a browser was free to keep the register, login and /me payloads. The rest of
# the transport hardening (TLS, HSTS at the edge, a host allow-list) belongs to
# whatever terminates TLS in front of this service, which is not in this
# repository; these are the headers the application can set correctly on its
# own wherever it is deployed.
_SECURITY_HEADERS = (
    (b'x-content-type-options', b'nosniff'),
    (b'x-frame-options', b'DENY'),
    (b'referrer-policy', b'no-referrer'),
    (b'strict-transport-security', b'max-age=31536000; includeSubDomains'),
)

# A JSON API needs no origin of its own for anything, so the policy denies
# every source by default. The two documentation surfaces are exempt on
# purpose: FastAPI 0.95.2 serves Swagger UI and ReDoc from a CDN with an inline
# bootstrap script, so a default-src of 'none' would blank the only browser UI
# this application actually serves -- the cure would be worse than the defect.
_CONTENT_SECURITY_POLICY = (
    b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
)
_DOCUMENTATION_PATHS = frozenset((
    '/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json',
))
_NO_STORE_PREFIX = f'{settings.API_V1_STR}/auth'


def _extra_response_headers(path, authenticated):
    """Return the header pairs this application adds to one response."""
    headers = list(_SECURITY_HEADERS)
    if path not in _DOCUMENTATION_PATHS:
        headers.append((b'content-security-policy', _CONTENT_SECURITY_POLICY))
    # A response is unstorable when it was requested with a credential, or
    # when it is an authentication response that carries a freshly minted
    # token in its body whether or not a credential was presented.
    if authenticated or path.startswith(_NO_STORE_PREFIX):
        headers.append((b'cache-control', b'no-store'))
    return headers


class SecurityHeadersMiddleware:
    """Attach the headers above to every HTTP response.

    A plain ASGI middleware rather than a BaseHTTPMiddleware subclass: it only
    needs to see the response-start message, so there is no reason to buffer a
    request or a response body to do it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        authenticated = any(
            name == b'authorization' for name, _ in scope.get('headers') or ()
        )

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                headers = message.setdefault('headers', [])
                present = {name.lower() for name, _ in headers}
                for name, value in _extra_response_headers(
                        path, authenticated):
                    # A handler that has already set one of these -- a future
                    # route with its own caching rules, say -- keeps its value.
                    if name not in present:
                        headers.append((name, value))
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Answer an unhandled failure with a CORS-decorated JSON 500."""
    # Starlette's ServerErrorMiddleware is the outermost layer of the stack, so
    # the 500 it produces is created after CORSMiddleware has been left behind
    # and carried none of its headers. A browser then discarded the response
    # for want of an allow-origin header and the page could only report
    # "Failed to fetch" -- byte-identical to what it shows when this service is
    # not running at all, so neither a user nor client code could tell a bug
    # from an outage, and nothing counting status codes saw either. This
    # handler runs in that same outermost position, which is why it applies
    # both header sets itself rather than relying on the middleware above.
    # ServerErrorMiddleware re-raises after calling it, so Uvicorn still logs
    # the traceback: nothing is swallowed, only answered.
    logger.error(
        'unhandled exception on %s %s; answering 500',
        request.method, request.url.path,
    )
    headers = {
        name.decode(): value.decode()
        for name, value in _extra_response_headers(
            request.url.path,
            'authorization' in request.headers,
        )
    }
    headers['cache-control'] = 'no-store'
    origin = request.headers.get('origin')
    allowed = settings.ALLOWED_ORIGINS
    if origin and (origin in allowed or '*' in allowed):
        # Echo the origin rather than '*', because the CORS middleware runs
        # with allow_credentials=True and a wildcard is invalid with
        # credentials; Vary keeps a cache from serving one origin's copy to
        # another.
        headers['access-control-allow-origin'] = origin
        headers['access-control-allow-credentials'] = 'true'
        headers['vary'] = 'Origin'
    return JSONResponse(
        status_code=500,
        content={'detail': 'Internal server error'},
        headers=headers,
    )


app.include_router(auth_router, prefix='/api/auth', tags=['Authentication'])
app.include_router(listings_router, prefix='/api/listings', tags=['Listings'])
app.include_router(transactions_router, prefix='/api/transactions', tags=['Transactions'])
app.include_router(messages_router, prefix='/api/messages', tags=['Messages'])
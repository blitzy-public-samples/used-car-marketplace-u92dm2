from fastapi import (
    APIRouter, Depends, HTTPException, Request, Response, status,
)
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional
import hashlib
import logging
import re
import secrets
import threading
import time
from pydantic import BaseModel, validator
# Raised by DocumentReference.create when the document already exists, which
# is what makes the email marker below an atomic uniqueness constraint rather
# than another query that a concurrent request can slip past.
from google.api_core.exceptions import AlreadyExists
from app.core.config import settings
from app.db.firestore import db
from app.schema.user import User

# tokenUrl must name the route that actually mints tokens. The literal 'token'
# matched no route, so the OpenAPI security scheme advertised an endpoint that
# does not exist and the Swagger authorize flow could never complete.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f'{settings.API_V1_STR}/auth/login'
)
# This module held a complete set of authentication helpers and no router at
# all, so main.py had nothing to mount and no route matched any of the four
# auth paths. None was ever seen answering 404 -- the import failure in
# main.py came first, and the SPA cannot execute either -- but 404 is what
# they would have returned the moment either side could run.
router = APIRouter()
pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
logger = logging.getLogger(__name__)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def authenticate_user(email: str, password: str) -> User:
    user_doc = db.collection('users').where('email', '==', email).limit(1).get()
    if not user_doc:
        return False
    user = User.from_dict(user_doc[0].to_dict())
    if not verify_password(password, user.hashed_password):
        return False
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            logger.warning("JWT missing 'sub' claim")
            raise credentials_exception
    except JWTError:
        logger.warning("JWT validation failed")
        raise credentials_exception
    user_doc = db.collection('users').document(user_id).get()
    if not user_doc.exists:
        logger.warning("Authenticated subject not found: %s", user_id)
        raise credentials_exception
    return User.from_dict(user_doc.to_dict())

class Token(BaseModel):
    access_token: str
    token_type: str


# --- Policy for the two public routes -------------------------------------
# Registration is the only unauthenticated write in this API and sign-in is
# the only unauthenticated read of a credential, so both need bounds that the
# helpers above do not provide.
#
# The roles a caller may give itself. 'admin' is excluded deliberately and is
# the whole point of the list: get_current_user reads the stored role back on
# every request and delete_listing (app/api/listings.py) lets an admin delete
# any seller's listing, so a role string accepted verbatim here would let one
# anonymous request grant itself that privilege. Administrators are provisioned
# out of band -- see documentation/ONBOARDING.md section 2.2.
_SELF_SERVICE_ROLES = ('buyer', 'seller')
# The longest address SMTP carries (RFC 5321), so nothing longer can be real.
_MAX_EMAIL_LENGTH = 254
_MAX_NAME_LENGTH = 100
_MIN_PASSWORD_LENGTH = 8
# bcrypt hashes only the first 72 bytes and passlib discards the rest in
# silence, so a longer password promises strength it does not have and any two
# sharing a 72-byte prefix are interchangeable. Refusing is the honest answer;
# truncating quietly is the one option to avoid.
_MAX_PASSWORD_BYTES = 72
# Deliberately conservative: an address must have a local part, an '@', and a
# dotted domain with no empty label. pydantic's EmailStr would need
# email-validator, which is not in this project's dependency set.
_EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')
# One document per registered address, keyed by a digest of the address so the
# id is a legal Firestore id of fixed length and carries no readable address.
_EMAIL_INDEX_COLLECTION = 'user_emails'
# Fixed windows, counted in this process only. A deployment running several
# workers multiplies these ceilings, so a global limit still needs a shared
# store or a gateway policy -- documented as a next task, not implied here.
_RATE_LIMIT_WINDOW_SECONDS = 60
_MAX_LOGINS_PER_WINDOW = 10
_MAX_REGISTRATIONS_PER_WINDOW = 5
_rate_lock = threading.Lock()
_rate_counters = {}
# Built on first use rather than at import, so the module costs nothing to
# import; see _absorb_unknown_account_cost for what it is for.
_unknown_account_hash = None


def _canonical_email(value: str) -> str:
    # One address must have one representation, or the guarantees around it
    # dissolve: ' A@B.com ' and 'a@b.com' would occupy two user documents
    # while authenticate_user's exact-match query resolves sign-in to
    # whichever of them Firestore returns first.
    return value.strip().lower()


def _email_key(email: str) -> str:
    return hashlib.sha256(_canonical_email(email).encode('utf-8')).hexdigest()


def _validated_email(value: str) -> str:
    # Shared by both request models so that the address a caller registers and
    # the address it later signs in with are shaped and normalised identically.
    email = _canonical_email(value)
    if len(email) > _MAX_EMAIL_LENGTH:
        raise ValueError(
            'email must not exceed %d characters' % _MAX_EMAIL_LENGTH
        )
    if not _EMAIL_PATTERN.match(email):
        raise ValueError('email is not a valid address')
    return email


def _peer(request: Request) -> str:
    # X-Forwarded-For is ignored on purpose. Nothing in this repository
    # terminates TLS in front of the application or strips that header, so
    # honouring it would let any caller reset its own counter by inventing an
    # address. Put a proxy in front and this is the value to revisit.
    client = request.client
    return client.host if client is not None else 'unknown'


def _enforce_attempt_limit(bucket: str, subject: str, ceiling: int) -> None:
    """Count one attempt in the current window and refuse past the ceiling.

    Unthrottled, these two routes are both a credential-stuffing surface and
    a CPU amplifier: every sign-in attempt costs one bcrypt verification, and
    registration costs a hash plus two writes. The refusal carries no hint
    about whether the account exists.
    """
    now = time.time()
    window = int(now // _RATE_LIMIT_WINDOW_SECONDS)
    key = (bucket, subject)
    with _rate_lock:
        # Windows older than the previous one can never be consulted again, so
        # dropping them keeps this map bounded by live traffic rather than by
        # the number of addresses ever submitted.
        for stale in [
            recorded for recorded, (seen, _) in _rate_counters.items()
            if seen < window - 1
        ]:
            del _rate_counters[stale]
        seen_window, count = _rate_counters.get(key, (window, 0))
        count = count + 1 if seen_window == window else 1
        _rate_counters[key] = (window, count)
    if count > ceiling:
        # The bucket is logged, never the subject: an email address or a peer
        # address in a log line is personal data the operator did not ask for.
        logger.warning('attempt limit reached', extra={'bucket': bucket})
        raise HTTPException(
            status_code=429,
            detail='Too many attempts. Please try again shortly.',
            headers={
                'Retry-After': str(max(1, int(
                    (window + 1) * _RATE_LIMIT_WINDOW_SECONDS - now
                ))),
            },
        )


def _clear_attempt_limit(bucket: str, subject: str) -> None:
    # A caller who proves it holds the credential is not the caller these
    # counters are for, so its own window is released.
    with _rate_lock:
        _rate_counters.pop((bucket, subject), None)


def _absorb_unknown_account_cost(email: str, password: str) -> None:
    """Spend one bcrypt verification when no account holds this address.

    authenticate_user returns as soon as its query comes back empty, so an
    unknown address answers measurably faster than a known one with the wrong
    password -- which turns a uniform 401 into an account oracle anyway.
    Verifying against a throwaway hash costs the same work the known-address
    path already paid, and the result is discarded.

    Existence is read from the marker document rather than by querying the
    users collection, because a document read by id is the cheaper of the two
    and needs no index. An account registered before that marker existed has
    none, so a failed attempt against it spends one verification more than it
    needs to -- noise on the slow side, never a shortcut on the fast one.
    """
    global _unknown_account_hash
    claimed = (
        db.collection(_EMAIL_INDEX_COLLECTION)
        .document(_email_key(email))
        .get()
    )
    if claimed.exists:
        return
    if _unknown_account_hash is None:
        _unknown_account_hash = get_password_hash(secrets.token_urlsafe(32))
    verify_password(password, _unknown_account_hash)


def _no_store(response: Response) -> None:
    # Token and profile responses must not be written to a shared or on-disk
    # cache: 'no-store' is the only directive that keeps a bearer token out of
    # one. Transport and browser policy headers (HSTS, CSP, frame and
    # content-type options, referrer policy) are not set here because this
    # application is not the TLS terminator -- see the ownership note in
    # documentation/ONBOARDING.md section 1.11.
    response.headers['Cache-Control'] = 'no-store'


class RegisterRequest(BaseModel):
    # The field set comes from the User schema, not from a client: the SPA
    # ships no register function, and every User field except the password
    # hash is required, so persisting a partial document would fail
    # validation on the next authenticated request instead of here.
    email: str
    password: str
    first_name: str
    last_name: str
    role: str

    # Validators rather than checks in the handler, so a bad body is a 422
    # before the duplicate lookup, before bcrypt and before Firestore -- and
    # so the value that reaches the duplicate check is the same normalised
    # value that gets stored.
    @validator('email')
    def _email_is_an_address(cls, value: str) -> str:
        return _validated_email(value)

    @validator('password')
    def _password_is_usable(cls, value: str) -> str:
        if len(value) < _MIN_PASSWORD_LENGTH:
            raise ValueError(
                'password must be at least %d characters'
                % _MIN_PASSWORD_LENGTH
            )
        if len(value.encode('utf-8')) > _MAX_PASSWORD_BYTES:
            raise ValueError(
                'password must not exceed %d bytes when UTF-8 encoded, '
                'because bcrypt ignores anything beyond that'
                % _MAX_PASSWORD_BYTES
            )
        return value

    @validator('first_name', 'last_name')
    def _name_is_present_and_bounded(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError('name must not be blank')
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError(
                'name must not exceed %d characters' % _MAX_NAME_LENGTH
            )
        return name

    @validator('role')
    def _role_is_self_service(cls, value: str) -> str:
        # Case is normalised because every authorization check in this codebase
        # compares the stored string exactly: 'Seller' stored as written would
        # then fail the seller gate on the very next request.
        role = value.strip().lower()
        if role not in _SELF_SERVICE_ROLES:
            # Logged so that an attempt to self-elevate is visible, with no
            # caller-supplied text in the record.
            logger.warning('registration refused: role is not self-service')
            raise ValueError(
                'role must be one of %s; administrators are provisioned out '
                'of band' % ', '.join(_SELF_SERVICE_ROLES)
            )
        return role


class LoginRequest(BaseModel):
    # The SPA posts a JSON body {email, password}, so login binds a request
    # model rather than OAuth2PasswordRequestForm: a form-bound route would
    # reject every call the client makes, and would newly require
    # python-multipart. Accepted consequence: Swagger's "Authorize" password
    # flow, which submits a form, cannot complete against this route.
    email: str
    password: str

    @validator('email')
    def _email_is_canonical(cls, value: str) -> str:
        # Canonicalised, but deliberately not shape-checked: a sign-in attempt
        # for an address that could never be registered must answer with the
        # same 401 as any other failed attempt, not with a 422 that tells the
        # caller its guess was rejected before the credential was even read.
        return _canonical_email(value)


def _public_user(user: User) -> dict:
    # Project the public fields explicitly. No route in this codebase
    # declares a response_model and User still carries hashed_password, so
    # returning the model itself would serialise the bcrypt hash back to
    # the client.
    return {
        'id': user.id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'role': user.role,
        'created_at': user.created_at,
        'updated_at': user.updated_at,
    }


def _issue_token(user: User) -> str:
    # Supply the configured lifetime explicitly.
    # ACCESS_TOKEN_EXPIRE_MINUTES is a required setting that no module read,
    # and create_access_token's 15-minute fallback governs any caller that
    # omits expires_delta -- so a deployment asking for 60 minutes would have
    # been handed 15 by the first token-issuing path written without this
    # argument. No such path existed to be caught doing it: there was no
    # route that minted a token at all. That fallback is reachable through a
    # falsey delta too, which is why app/core/config.py refuses a lifetime
    # below one minute rather than letting a zero here inherit fifteen. The
    # subject is the user id because get_current_user resolves 'sub' as a
    # Firestore document id.
    return create_access_token(
        {'sub': user.id},
        expires_delta=timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        ),
    )


@router.post('/register', status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, response: Response):
    """Create a user account and return a token with the public profile."""
    _enforce_attempt_limit(
        'register', _peer(request), _MAX_REGISTRATIONS_PER_WINDOW
    )
    # Firestore enforces no unique index on email and authenticate_user's
    # limit(1) would silently pick one of any duplicates, so a collision
    # is rejected here, before anything is written. This query is the fast
    # path and it also covers any document written before the marker
    # collection below existed; the marker is what makes the guarantee hold
    # against two simultaneous requests, which a query never can.
    existing = (
        db.collection('users')
        .where('email', '==', payload.email)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Email is already registered",
        )
    # Reserve the document reference first so its id can be stored inside the
    # document itself: authenticate_user and get_current_user both hydrate
    # through User.from_dict, which requires every field but the hash.
    # Reserving allocates an id client-side and writes nothing.
    user_ref = db.collection('users').document()
    now = datetime.utcnow()
    user_data = {
        'id': user_ref.id,
        'email': payload.email,
        'first_name': payload.first_name,
        'last_name': payload.last_name,
        'role': payload.role,
        'created_at': now,
        'updated_at': now,
        'hashed_password': get_password_hash(payload.password),
    }
    # Validate the document and mint the token before the irreversible write.
    # Both steps can fail -- from_dict on a value the schema rejects,
    # _issue_token on an ALGORITHM the signer does not support -- and doing
    # them afterwards would commit a real account that its owner was never
    # told about and could not create again, because the retry answers 409.
    user = User.from_dict(user_data)
    token = _issue_token(user)
    # Claim the address atomically before writing the account. create() is a
    # conditional write -- it fails if the document is already there -- so two
    # requests racing for one address cannot both get past this line, which a
    # query-then-write pair demonstrably can.
    marker_ref = (
        db.collection(_EMAIL_INDEX_COLLECTION)
        .document(_email_key(payload.email))
    )
    try:
        marker_ref.create({'user_id': user_ref.id, 'created_at': now})
    except AlreadyExists:
        raise HTTPException(
            status_code=409,
            detail="Email is already registered",
        )
    try:
        user_ref.set(user_data)
    except Exception:
        # Release the claim, or a failed write would leave the address
        # permanently unregisterable with no account behind it.
        marker_ref.delete()
        raise
    _no_store(response)
    # access_token and token_type honour the OAuth2 convention the Token
    # model above describes; token and user are the keys the SPA actually
    # reads, and it throws "Invalid response from server" when a top-level
    # token is absent.
    return {
        'access_token': token,
        'token_type': 'bearer',
        'token': token,
        'user': _public_user(user),
    }


@router.post('/login')
def login(payload: LoginRequest, request: Request, response: Response):
    """Exchange an email and password for an access token."""
    # Two counters, because they answer different attacks: the peer address
    # caps one client working through many accounts, and the address caps many
    # clients working on one account.
    peer = _peer(request)
    _enforce_attempt_limit('login-peer', peer, _MAX_LOGINS_PER_WINDOW)
    _enforce_attempt_limit('login-account', payload.email,
                           _MAX_LOGINS_PER_WINDOW)
    user = authenticate_user(payload.email, payload.password)
    # authenticate_user is annotated -> User but returns False on both of
    # its failure branches, so falsiness is what must be tested here: "if
    # user is None" never fires on False, so a failed login would fall
    # straight into the success path below and raise AttributeError: 'bool'
    # object has no attribute 'id'. That is a 500 in place of a 401 -- no
    # token is minted, but the caller learns nothing and the log fills with
    # tracebacks instead of rejections.
    if not user:
        _absorb_unknown_account_cost(payload.email, payload.password)
        # One message for an unknown email and for a wrong password, so the
        # response cannot be used to enumerate accounts, and the same header
        # get_current_user already returns on a 401.
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    _clear_attempt_limit('login-peer', peer)
    _clear_attempt_limit('login-account', payload.email)
    token = _issue_token(user)
    _no_store(response)
    # The same four keys register returns, for the same client contract.
    return {
        'access_token': token,
        'token_type': 'bearer',
        'token': token,
        'user': _public_user(user),
    }


@router.post('/logout')
def logout(response: Response,
           current_user: User = Depends(get_current_user)):
    """Acknowledge a sign-out; the token stays valid until it expires."""
    # Acknowledgement only. Tokens carry just 'sub' and 'exp' and this
    # codebase holds no denylist, revocation list or server-side session,
    # so nothing here can shorten a token's life. The route exists because
    # the SPA posts to it and clears its stored token whatever the outcome,
    # and until this route existed nothing matched that post -- a 404 the
    # client would have met on every sign-out, once either side could run.
    _no_store(response)
    return {"detail": "Logged out"}


@router.get('/me')
def read_current_user(response: Response,
                      current_user: User = Depends(get_current_user)):
    """Return the authenticated user's public profile."""
    # Nested under 'user' because the SPA reads response.data.user; a flat
    # body would hand it undefined.
    _no_store(response)
    return {'user': _public_user(current_user)}

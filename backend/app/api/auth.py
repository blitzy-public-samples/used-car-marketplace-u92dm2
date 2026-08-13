from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import logging
import re
import threading
import time
from pydantic import BaseModel, validator
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


# --- Input policy for the two public routes -------------------------------
# Registration is the only unauthenticated write in this API and it used to
# check nothing beyond field presence and type, so it stored whatever it was
# sent: a role of 'admin', an address with no domain, a one-character
# password, a blank name, markup in a name. The constants and helpers below
# are what those bodies are now measured against. They live on
# RegisterRequest's validators rather than in the handler so that a bad body
# is a 422 before the duplicate lookup, before bcrypt and before Firestore,
# and so the handler receives one normalised value it can trust.

# The roles a caller may give itself. 'admin' is deliberately absent:
# get_current_user re-reads the role from the user document on every request
# and delete_listing treats it as an override, so accepting a client-supplied
# 'admin' handed any anonymous caller destructive control over every listing
# in the system. Granting the role deliberately is a separate, authenticated
# path this codebase does not have yet (see documentation/ONBOARDING.md).
_SELF_SERVICE_ROLES = ('buyer', 'seller')

# 254 characters is the longest address SMTP carries. pydantic's EmailStr
# would be the obvious shape check, but it requires the email-validator
# package, which is not part of this project's pinned dependency set, so the
# shape is checked here by hand rather than by adding a dependency.
_MAX_EMAIL_LENGTH = 254
_EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$')

# bcrypt hashes only the first 72 bytes of a password and passlib discards the
# rest without a word, so a longer password promised strength it did not have
# and any two sharing a 72-byte prefix were interchangeable. Above passlib's
# own 4096-byte ceiling it was worse still: PasswordSizeError escaped as a
# 500 in text/plain that a browser then discarded for want of CORS headers.
# Refusing is the honest answer where truncating quietly is the one option to
# avoid. Both bounds are measured on the UTF-8 encoding, because bytes are
# what bcrypt counts -- a 72-character password of multi-byte characters
# would still be truncated.
_MIN_PASSWORD_BYTES = 8
_MAX_PASSWORD_BYTES = 72

_MAX_NAME_LENGTH = 100
# A name carrying markup is refused rather than escaped: this API declares no
# response_model and performs no output encoding of its own, and both listing
# reads are unauthenticated, so a stored '<img src=x onerror=...>' was a
# payload waiting for the first client that renders a name as HTML. Control
# characters go with it. Apostrophes and hyphens are left alone, because real
# names carry them.
_NAME_FORBIDDEN = re.compile(r'[<>\x00-\x1f\x7f]')


def _canonical_email(value: str) -> str:
    """Trim and lower-case an address so that one address is one account."""
    # Every authorization check and the duplicate-registration query compare
    # the stored string exactly, so without this ' A@b.com ' and 'a@b.com'
    # were two accounts, and signing in with any spelling other than the one
    # registered answered 401. Registration and login canonicalise
    # identically; changing one without the other would lock accounts out.
    return value.strip().lower()


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

    @validator('email')
    def _validate_email(cls, value: str) -> str:
        email = _canonical_email(value)
        if not email:
            raise ValueError('email is required')
        if len(email) > _MAX_EMAIL_LENGTH:
            raise ValueError(
                'email must be at most %d characters' % _MAX_EMAIL_LENGTH
            )
        if not _EMAIL_PATTERN.match(email):
            raise ValueError(
                'email must be a valid address, for example name@example.com'
            )
        return email

    @validator('password')
    def _validate_password(cls, value: str) -> str:
        size = len(value.encode('utf-8'))
        if size < _MIN_PASSWORD_BYTES:
            raise ValueError(
                'password must be at least %d bytes long'
                % _MIN_PASSWORD_BYTES
            )
        if size > _MAX_PASSWORD_BYTES:
            raise ValueError(
                'password must be at most %d bytes long, which is all bcrypt '
                'hashes' % _MAX_PASSWORD_BYTES
            )
        return value

    @validator('first_name', 'last_name')
    def _validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError('name is required')
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError(
                'name must be at most %d characters' % _MAX_NAME_LENGTH
            )
        if _NAME_FORBIDDEN.search(name):
            raise ValueError(
                'name must not contain markup or control characters'
            )
        return name

    @validator('role')
    def _validate_role(cls, value: str) -> str:
        role = value.strip().lower()
        if role not in _SELF_SERVICE_ROLES:
            # Worth a log line: a caller asking for a role it may not give
            # itself is the privilege escalation this check exists to stop.
            # The value is truncated because it is caller-supplied.
            logger.warning(
                'registration refused: %r is not a self-service role',
                value[:32],
            )
            raise ValueError(
                'role must be one of: %s' % ', '.join(_SELF_SERVICE_ROLES)
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


def _utc_isoformat(value: datetime) -> str:
    # Serialise a timestamp one way, whichever route is answering. register
    # projects the value it just built from datetime.utcnow(), which is naive,
    # while login and /me project the same field after a Firestore round trip,
    # which returns it tz-aware -- so one user's created_at went out as
    # '...T11:08:02.692334' from register and '...T11:08:02.692334+00:00' from
    # /me. That is not only inconsistent: a client parsing the offset-less form
    # with new Date() reads it as local time, so the same instant moved by the
    # reader's timezone. Naive values are UTC by this codebase's convention
    # (datetime.utcnow()), so they are labelled rather than converted.
    #
    # This normalises the wire form only. The clock is untouched --
    # datetime.utcnow() still writes the documents and still sets token expiry
    # -- because migrating the codebase to timezone-aware datetimes is a
    # coordinated change tracked separately, not something to do here.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


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
        'created_at': _utc_isoformat(user.created_at),
        'updated_at': _utc_isoformat(user.updated_at),
    }


def _issue_token(user: User) -> str:
    # Supply the configured lifetime explicitly.
    # ACCESS_TOKEN_EXPIRE_MINUTES is a required setting that no module read,
    # and create_access_token's 15-minute fallback governs any caller that
    # omits expires_delta -- so a deployment asking for 60 minutes would have
    # been handed 15 by the first token-issuing path written without this
    # argument. No such path existed to be caught doing it: there was no
    # route that minted a token at all. The subject is the user id because
    # get_current_user resolves 'sub' as a Firestore document id.
    return create_access_token(
        {'sub': user.id},
        expires_delta=timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        ),
    )


# --- Attempt ceilings for the two public routes ---------------------------
# Neither public route counted anything, so a caller could send as many
# attempts as it liked: fifteen consecutive failed sign-ins answered fifteen
# 401s and no 429, which made credential stuffing cost only bandwidth and made
# registration usable both to fill the users collection and -- because the 409
# distinguishes a registered address from an unregistered one -- to enumerate
# it. The ceilings below are deliberately modest and deliberately honest about
# what they are:
#
#   * The counters live in this process. With N workers the effective ceiling
#     is N times the configured one, and a restart forgets every window. A
#     real bound needs a shared store or a gateway policy, and neither may be
#     added here without authorizing a new dependency, so this is the ceiling
#     that can be had without one -- not the ceiling this service should ship
#     with behind a load balancer.
#   * The peer is taken from the connection, never from X-Forwarded-For.
#     Nothing in this repository terminates TLS or strips that header, so
#     honouring it would let a caller reset its own counter by inventing one.
#     Behind a trusted proxy every request collapses onto the proxy's address
#     instead, which is why the trusted-proxy depth has to become
#     configuration before that header can be believed.
#   * The check runs BEFORE the deliberate bcrypt-equalising work below, so
#     the expensive path sits behind the ceiling rather than in front of it.
_LOGIN_FAILURE_LIMIT = 10
_LOGIN_FAILURE_WINDOW_SECONDS = 60
# Registration is bounded more loosely than sign-in because a burst of genuine
# sign-ups from one address is ordinary where a burst of failed sign-ins is
# not, and because every malformed body is already refused by validation
# before it reaches the handler and so never consumes a slot.
_REGISTER_LIMIT = 20
_REGISTER_WINDOW_SECONDS = 60


class _FixedWindowThrottle:
    """Count timestamped attempts per key inside a rolling window.

    Deliberately small: a dict of monotonic timestamps behind one lock, with
    expired entries pruned on the way past. It is exact for a single process
    and claims nothing beyond that.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._attempts: Dict[str, List[float]] = {}
        # Starlette runs these synchronous handlers in a threadpool, so two
        # requests really can be inside this object at the same time.
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> List[float]:
        recent = [
            at for at in self._attempts.get(key, ())
            if now - at < self._window
        ]
        if recent:
            self._attempts[key] = recent
        else:
            self._attempts.pop(key, None)
        return recent

    def check(self, key: str, label: str) -> None:
        """Raise 429 when `key` has already filled its window."""
        now = time.monotonic()
        with self._lock:
            recent = self._prune(key, now)
            if len(recent) < self._limit:
                return
            retry_after = max(1, int(self._window - (now - min(recent))) + 1)
        # The bucket that filled is logged, never the address that filled it:
        # an address in a log line is the account enumeration this ceiling
        # exists to make expensive.
        logger.warning(
            'attempt ceiling reached for %s; refusing for %d seconds',
            label, retry_after,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = self._prune(key, now)
            recent.append(now)
            self._attempts[key] = recent

    def clear(self, key: str) -> None:
        """Forget a key's attempts, so a real sign-in undoes its own typos."""
        with self._lock:
            self._attempts.pop(key, None)

    def reset(self) -> None:
        """Forget every key. For operators and for verification harnesses."""
        with self._lock:
            self._attempts.clear()


_LOGIN_THROTTLE = _FixedWindowThrottle(
    _LOGIN_FAILURE_LIMIT, _LOGIN_FAILURE_WINDOW_SECONDS
)
_REGISTER_THROTTLE = _FixedWindowThrottle(
    _REGISTER_LIMIT, _REGISTER_WINDOW_SECONDS
)

# A failed sign-in is held to this floor. authenticate_user returns as soon as
# its query comes back empty, so an unknown address answered in ~5 ms while a
# known address with a wrong password paid a ~300 ms bcrypt verification: a
# 60-fold difference that told an unauthenticated caller which addresses have
# accounts, however identical the two responses looked. Padding every failure
# to one floor removes the difference without touching authenticate_user,
# whose body is fixed. The floor sits above the measured worst-case
# verification cost with room to spare; if a deployment's bcrypt cost ever
# approaches it the padding stops hiding anything, so the overrun is logged
# rather than left to be guessed at.
_LOGIN_MIN_FAILURE_SECONDS = 0.75


def _peer_key(request: Request) -> str:
    """Identify the caller by its connection, not by a header it controls."""
    client = request.client
    return client.host if client and client.host else 'unknown-peer'


def _is_presentable_credential(email: str, password: str) -> bool:
    """Report whether a credential could match any stored one at all."""
    # A blank credential used to authenticate: an account had been registered
    # with an empty address and an empty password -- registration bounded
    # neither -- and login queried for exactly that and minted a full
    # 60-minute token. An over-long password was a different failure with the
    # same cause: passlib refuses anything above 4096 bytes, so the request
    # answered 500 rather than 401. Registration now bounds both, so neither
    # shape can match a stored credential, and both are refused here without
    # touching the datastore or bcrypt.
    if not email or not password:
        return False
    if len(email) > _MAX_EMAIL_LENGTH:
        return False
    if len(password.encode('utf-8')) > _MAX_PASSWORD_BYTES:
        return False
    return True


def _refuse_credential(started: float, peer: str, email: str) -> None:
    """Count the failure, equalise its cost, and raise the one rejection."""
    _LOGIN_THROTTLE.record('peer:' + peer)
    if email:
        _LOGIN_THROTTLE.record('account:' + email)
    elapsed = time.monotonic() - started
    if elapsed < _LOGIN_MIN_FAILURE_SECONDS:
        # A sync handler runs in Starlette's threadpool, so this waits on a
        # worker thread and not on the event loop.
        time.sleep(_LOGIN_MIN_FAILURE_SECONDS - elapsed)
    else:
        # The real work already outran the floor, so this rejection is no
        # longer indistinguishable from a cheaper one. Say so instead of
        # quietly leaking the difference: the floor needs raising.
        logger.warning(
            'sign-in rejection took %.3fs, above the %.3fs floor: timing '
            'parity is no longer guaranteed',
            elapsed, _LOGIN_MIN_FAILURE_SECONDS,
        )
    # One message for a blank credential, an unknown address and a wrong
    # password alike, carrying the same header get_current_user returns on a
    # 401, so nothing in the response can be used to enumerate accounts.
    raise HTTPException(
        status_code=401,
        detail="Incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post('/register', status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request):
    """Create a user account and return a token with the public profile."""
    # Bound how many accounts one caller may create in a window. Only bodies
    # that survive validation reach this point, so a malformed request costs
    # an attacker a slot it never gets to use.
    peer = _peer_key(request)
    _REGISTER_THROTTLE.check('peer:' + peer, 'registrations from peer ' + peer)
    _REGISTER_THROTTLE.record('peer:' + peer)
    # Firestore enforces no unique index on email and authenticate_user's
    # limit(1) would silently pick one of any duplicates, so a collision is
    # rejected here, before anything is written. A query cannot make that
    # guarantee against two requests arriving at once -- for that the write
    # itself has to be conditional -- which is recorded as an open question
    # rather than answered here.
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
    user_ref.set(user_data)
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
def login(payload: LoginRequest, request: Request):
    """Exchange an email and password for an access token."""
    # Every failure path below is held to one floor, measured from here, so
    # that an unknown address and a wrong password take the same time.
    started = time.monotonic()
    peer = _peer_key(request)
    # LoginRequest deliberately carries no field constraints of its own: a 422
    # on a sign-in attempt names a field of the request, and so would
    # distinguish a malformed credential from a rejected one -- exactly what
    # the 401 below is careful not to reveal. The address is canonicalised the
    # same way registration canonicalises it, or an account registered as
    # 'A@b.com' could never sign in, and a credential that cannot match any
    # stored one is refused with that same 401.
    email = _canonical_email(payload.email)
    # The ceiling is checked before any of the work below, including the
    # padding, so a caller that has filled its window is refused cheaply.
    _LOGIN_THROTTLE.check('peer:' + peer, 'sign-ins from peer ' + peer)
    if email:
        _LOGIN_THROTTLE.check('account:' + email, 'sign-ins for one address')
    if not _is_presentable_credential(email, payload.password):
        _refuse_credential(started, peer, email)
    user = authenticate_user(email, payload.password)
    # authenticate_user is annotated -> User but returns False on both of
    # its failure branches, so falsiness is what must be tested here: "if
    # user is None" never fires on False, so a failed login would fall
    # straight into the success path below and raise AttributeError: 'bool'
    # object has no attribute 'id'. That is a 500 in place of a 401 -- no
    # token is minted, but the caller learns nothing and the log fills with
    # tracebacks instead of rejections.
    if not user:
        _refuse_credential(started, peer, email)
    # A real sign-in undoes its own typos: the caller has proved it holds the
    # credential, so neither bucket should still be counting against it.
    _LOGIN_THROTTLE.clear('peer:' + peer)
    _LOGIN_THROTTLE.clear('account:' + email)
    token = _issue_token(user)
    # The same four keys register returns, for the same client contract.
    return {
        'access_token': token,
        'token_type': 'bearer',
        'token': token,
        'user': _public_user(user),
    }


@router.post('/logout')
def logout(current_user: User = Depends(get_current_user)):
    """Acknowledge a sign-out; the token stays valid until it expires."""
    # Acknowledgement only. Tokens carry just 'sub' and 'exp' and this
    # codebase holds no denylist, revocation list or server-side session,
    # so nothing here can shorten a token's life. The route exists because
    # the SPA posts to it and clears its stored token whatever the outcome,
    # and until this route existed nothing matched that post -- a 404 the
    # client would have met on every sign-out, once either side could run.
    return {"detail": "Logged out"}


@router.get('/me')
def read_current_user(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's public profile."""
    # Nested under 'user' because the SPA reads response.data.user; a flat
    # body would hand it undefined.
    return {'user': _public_user(current_user)}

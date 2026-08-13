from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional
import logging
from pydantic import BaseModel
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


class LoginRequest(BaseModel):
    # The SPA posts a JSON body {email, password}, so login binds a request
    # model rather than OAuth2PasswordRequestForm: a form-bound route would
    # reject every call the client makes, and would newly require
    # python-multipart. Accepted consequence: Swagger's "Authorize" password
    # flow, which submits a form, cannot complete against this route.
    email: str
    password: str


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
    # route that minted a token at all. The subject is the user id because
    # get_current_user resolves 'sub' as a Firestore document id.
    return create_access_token(
        {'sub': user.id},
        expires_delta=timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        ),
    )


@router.post('/register', status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest):
    """Create a user account and return a token with the public profile."""
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
def login(payload: LoginRequest):
    """Exchange an email and password for an access token."""
    user = authenticate_user(payload.email, payload.password)
    # authenticate_user is annotated -> User but returns False on both of
    # its failure branches, so falsiness is what must be tested here: "if
    # user is None" never fires on False, so a failed login would fall
    # straight into the success path below and raise AttributeError: 'bool'
    # object has no attribute 'id'. That is a 500 in place of a 401 -- no
    # token is minted, but the caller learns nothing and the log fills with
    # tracebacks instead of rejections.
    if not user:
        # One message for an unknown email and for a wrong password, so the
        # response cannot be used to enumerate accounts, and the same header
        # get_current_user already returns on a 401.
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
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

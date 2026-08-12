from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, ValidationError
from app.core.config import settings
from app.db.firestore import db
from app.schema.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')
pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')

# Router that ``app/main.py`` mounts at ``/api/auth``. It declares no
# routes on purpose: registration and login are F-001 work and are out
# of scope here, so mounting it contributes no paths. Defining it is
# still required, because ``app/main.py`` does
# ``from app.api.auth import auth_router`` - without this name that
# import raises and the whole application fails to start.
auth_router = APIRouter()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def authenticate_user(email: str, password: str) -> User:
    user_doc = db.collection('users').where('email', '==', email).limit(1).get()
    if not user_doc:
        return False
    # ``.where(...).limit(1).get()`` returns a LIST of snapshots, so the
    # match is ``user_doc[0]``. Pydantic v1 offers no ``from_dict``, and
    # the required ``User.id`` lives on the document reference rather
    # than in the body, so it is injected before construction.
    snapshot = user_doc[0]
    user_data = snapshot.to_dict() or {}
    user_data['id'] = snapshot.id
    # The stored hash is read from the RAW document and removed from the
    # dict before the model is built: ``User`` is the response shape
    # every router annotates, so a credential must never become one of
    # its attributes.
    hashed_password = user_data.pop('hashed_password', None)
    if not hashed_password:
        # An account with no stored hash cannot authenticate. Returning
        # here also keeps ``None`` out of passlib, which raises rather
        # than reporting a failed verification when handed one.
        return False
    if not verify_password(password, hashed_password):
        return False
    return User(**user_data)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

# HUMAN ASSISTANCE NEEDED
# This function might need additional error handling and security checks
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
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    # This read is load-bearing and runs on every request by design.
    # The token carries only ``sub`` and ``exp``, so authorization state
    # such as ``is_verified`` is resolved from the datastore each time -
    # revoking it takes effect on the caller's very next request, with
    # no token rotation. Never cache or memoise this lookup.
    user_doc = db.collection('users').document(user_id).get()
    if not user_doc.exists:
        raise credentials_exception
    # Pydantic v1 offers no ``from_dict``. ``User.id`` is required but
    # absent from the body - the user document is KEYED by the JWT
    # ``sub`` - so the id comes from the snapshot reference, which is
    # authoritative in a way a client-supplied claim is not.
    user_data = user_doc.to_dict() or {}
    user_data['id'] = user_doc.id
    try:
        return User(**user_data)
    except ValidationError:
        # A stored document that cannot satisfy the model means this
        # caller's identity cannot be validated, so answer with the
        # same 401 as a missing document instead of a 500 that would
        # expose the stored shape. Narrowed to ValidationError on
        # purpose: any other failure still surfaces.
        raise credentials_exception

class Token(BaseModel):
    access_token: str
    token_type: str

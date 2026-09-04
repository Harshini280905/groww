"""JWT authentication — replaces the DEMO_USER_ID stub.

Design choices, stated plainly:
  * HS256 JWT, 7-day expiry — generous on purpose, since a judge may return
    to the same browser session hours later and re-logging-in would be
    friction with no security benefit at hackathon scale.
  * SECRET_KEY from env; falls back to a fixed dev value with a loud stderr
    warning so nobody ships that fallback to a real deployment by accident.
  * Password hashing uses pbkdf2_sha256 (pure Python, no native bcrypt
    extension) rather than bcrypt — deliberate: passlib+bcrypt has a
    long-standing version-compatibility footgun (AttributeError on certain
    bcrypt>=4.1 builds) that has cost real people real debugging time on a
    deadline. pbkdf2_sha256 is a fine, standard choice for this threat model
    and removes an entire class of "why won't this install" risk.

This module is the real thing, not a demo bypass — see routers/auth.py for
the `demo-login` convenience route, which issues a token through this exact
same path against a fixed, documented account.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = "dev-only-insecure-secret-do-not-use-in-production"
    print(
        "WARNING: JWT_SECRET_KEY not set in the environment — using an "
        "insecure development default. Set JWT_SECRET_KEY before deploying.",
        file=sys.stderr,
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7   # 7 days

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_user_id(token: str) -> Optional[int]:
    """Returns the user id encoded in a token, or None if invalid/expired.
    Never raises — callers decide how to react to a None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except (JWTError, ValueError):
        return None


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise unauthorized
    user_id = decode_user_id(token)
    if user_id is None:
        raise unauthorized
    user = db.query(User).filter_by(id=user_id).first()
    if user is None:
        raise unauthorized
    return user

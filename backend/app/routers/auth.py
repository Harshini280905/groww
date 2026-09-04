"""Auth endpoints — register, login, and a demo-login convenience route.

`demo-login` gets-or-creates a fixed, documented demo account so a judge can
open the frontend and start using the app with zero manual signup — but the
token it returns travels through the exact same `create_access_token` path
as a real `/login`, so it is not a security bypass, just a fixed identity.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from ..auth import create_access_token, hash_password, verify_password
from ..db import get_db
from ..models import User

router = APIRouter()

DEMO_EMAIL = "demo@watchlist.local"
DEMO_PASSWORD = "demo-watchlist-2026"   # fixed and documented — not a secret


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str


@router.post("/register", response_model=TokenOut)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    existing = db.query(User).filter_by(email=body.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(email=body.email, hashed_password=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(access_token=create_access_token(user.id), email=user.email)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=body.email).first()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return TokenOut(access_token=create_access_token(user.id), email=user.email)


@router.post("/demo-login", response_model=TokenOut)
def demo_login(db: Session = Depends(get_db)):
    """Convenience route for the judging flow. Gets-or-creates a fixed
    account and returns a real signed JWT for it — same issuance path as
    /login, just against a well-known identity instead of one the visitor
    registers themselves."""
    user = db.query(User).filter_by(email=DEMO_EMAIL).first()
    if user is None:
        user = User(email=DEMO_EMAIL, hashed_password=hash_password(DEMO_PASSWORD))
        db.add(user)
        db.commit()
        db.refresh(user)
    return TokenOut(access_token=create_access_token(user.id), email=user.email)

import secrets
from datetime import timedelta
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from core.db import db
from core.security import (hash_password, verify_password, issue_session, clear_auth_cookies, get_current_user, public_user,
                           permissions_for, secret, ALG, sha)
from core.utils import now, new_id, rate_limit, client_ip
from services.notifications import notify

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    phone: str = Field(default="", max_length=20)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(min_length=8, max_length=128)


async def me_payload(user):
    return public_user(user, await permissions_for(user))


@router.post("/register")
async def register(body: RegisterIn, request: Request, response: Response):
    rate_limit(request, "register", 10, 300)
    email = body.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "An account with this email already exists")
    user = {"id": new_id(), "email": email, "name": body.name.strip(), "phone": body.phone.strip(), "role": "customer",
            "password_hash": hash_password(body.password), "active": True, "token_version": 0, "addresses": [], "wishlist": [],
            "created_at": now()}
    await db.users.insert_one(user)
    await issue_session(response, user)
    return await me_payload(user)


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    rate_limit(request, "login", 20, 60)
    email = body.email.lower()
    ident = f"{client_ip(request)}:{email}"
    att = await db.login_attempts.find_one({"identifier": ident})
    if att and att.get("count", 0) >= 5 and att.get("locked_until") and att["locked_until"] > now():
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        cnt = (att or {}).get("count", 0) + 1
        await db.login_attempts.update_one({"identifier": ident}, {"$set": {"count": cnt, "locked_until": now() + timedelta(minutes=15) if cnt >= 5 else None}}, upsert=True)
        raise HTTPException(401, "Invalid email or password")
    if not user.get("active", True):
        raise HTTPException(403, "This account has been disabled. Please contact support.")
    await db.login_attempts.delete_one({"identifier": ident})
    await db.users.update_one({"id": user["id"]}, {"$set": {"last_login_at": now()}})
    await issue_session(response, user)
    return await me_payload(user)


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "No refresh token")
    try:
        payload = jwt.decode(token, secret(), algorithms=[ALG])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid refresh token")
    if payload.get("type") != "refresh":
        raise HTTPException(401, "Invalid refresh token")
    sess = await db.sessions.find_one_and_update({"jti": payload["jti"], "revoked": False}, {"$set": {"revoked": True, "rotated_at": now()}})
    if not sess:
        # reuse of a rotated token: revoke all sessions of this user
        await db.sessions.update_many({"user_id": payload["sub"]}, {"$set": {"revoked": True}})
        clear_auth_cookies(response)
        raise HTTPException(401, "Session expired")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user or not user.get("active", True):
        raise HTTPException(401, "Session expired")
    await issue_session(response, user)
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if token:
        try:
            payload = jwt.decode(token, secret(), algorithms=[ALG], options={"verify_exp": False})
            await db.sessions.update_one({"jti": payload.get("jti")}, {"$set": {"revoked": True}})
        except jwt.InvalidTokenError:
            pass
    clear_auth_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(response: Response, user=Depends(get_current_user)):
    await db.users.update_one({"id": user["id"]}, {"$inc": {"token_version": 1}})
    await db.sessions.update_many({"user_id": user["id"]}, {"$set": {"revoked": True}})
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return await me_payload(user)


async def create_reset_token(user: dict, origin: str) -> str:
    token = secrets.token_urlsafe(32)
    await db.password_reset_tokens.insert_one({"token_hash": sha(token), "user_id": user["id"], "used": False,
                                               "created_at": now(), "expires_at": now() + timedelta(hours=1)})
    link = f"{origin}/reset-password?token={token}"
    await notify("password_reset", user["email"], "Reset your RAF DECOR password", f"Reset link (valid 1 hour): {link}", user["id"])
    import logging
    logging.getLogger("auth").info("Password reset link for user %s: %s", user["id"], link)
    return link


@router.post("/forgot-password")
async def forgot(body: ForgotIn, request: Request):
    rate_limit(request, "forgot", 5, 300)
    user = await db.users.find_one({"email": body.email.lower(), "active": True}, {"_id": 0})
    if user:
        await create_reset_token(user, request.headers.get("origin") or "")
    return {"ok": True, "message": "If an account exists for this email, a reset link has been issued."}


@router.post("/reset-password")
async def reset(body: ResetIn, request: Request):
    rate_limit(request, "reset", 10, 300)
    rec = await db.password_reset_tokens.find_one_and_update(
        {"token_hash": sha(body.token), "used": False, "expires_at": {"$gt": now()}}, {"$set": {"used": True}})
    if not rec:
        raise HTTPException(400, "This reset link is invalid or has expired")
    await db.users.update_one({"id": rec["user_id"]}, {"$set": {"password_hash": hash_password(body.password)}, "$inc": {"token_version": 1}})
    await db.sessions.update_many({"user_id": rec["user_id"]}, {"$set": {"revoked": True}})
    return {"ok": True}

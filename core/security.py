import os
import hashlib
import secrets
from datetime import timedelta
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response
from core.db import db
from core.utils import now, new_id

ALG = "HS256"
ACCESS_MIN = 15
REFRESH_DAYS = 7
STAFF_ROLES = {"admin", "product_manager", "order_manager", "support", "marketing", "reporting"}

ALL_PERMISSIONS = [
    "products.read", "products.write", "orders.read", "orders.write", "orders.finance",
    "customers.read", "customers.write", "inventory.write", "marketing.write", "content.write",
    "inquiries.read", "inquiries.write", "analytics.read", "users.manage", "audit.read", "migration.run",
]

DEFAULT_ROLES = {
    "admin": ("Administrator", ALL_PERMISSIONS),
    "product_manager": ("Product manager", ["products.read", "products.write", "inventory.write", "analytics.read"]),
    "order_manager": ("Order manager", ["orders.read", "orders.write", "orders.finance", "customers.read", "inventory.write", "products.read"]),
    "support": ("Customer support", ["inquiries.read", "inquiries.write", "orders.read", "customers.read", "products.read"]),
    "marketing": ("Marketing manager", ["marketing.write", "content.write", "products.read", "analytics.read"]),
    "reporting": ("Read-only reporting", ["analytics.read", "orders.read", "products.read", "customers.read", "audit.read"]),
}


def secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access(user: dict) -> str:
    payload = {"sub": user["id"], "tv": user.get("token_version", 0), "type": "access",
               "exp": now() + timedelta(minutes=ACCESS_MIN)}
    return jwt.encode(payload, secret(), algorithm=ALG)


async def create_refresh(user: dict) -> str:
    jti = secrets.token_urlsafe(24)
    await db.sessions.insert_one({"jti": jti, "user_id": user["id"], "revoked": False,
                                  "created_at": now(), "expires_at": now() + timedelta(days=REFRESH_DAYS)})
    return jwt.encode({"sub": user["id"], "jti": jti, "type": "refresh",
                       "exp": now() + timedelta(days=REFRESH_DAYS)}, secret(), algorithm=ALG)


def set_auth_cookies(response: Response, access: str, refresh: str):
    response.set_cookie("access_token", access, httponly=True, secure=True, samesite="lax", max_age=ACCESS_MIN * 60, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=True, samesite="lax", max_age=REFRESH_DAYS * 86400, path="/api/v1/auth")


def clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/", secure=True, samesite="lax")
    response.delete_cookie("refresh_token", path="/api/v1/auth", secure=True, samesite="lax")


async def issue_session(response: Response, user: dict):
    set_auth_cookies(response, create_access(user), await create_refresh(user))


def public_user(u: dict, perms=None) -> dict:
    out = {k: u.get(k) for k in ("id", "email", "name", "phone", "role", "created_at")}
    out["is_staff"] = u.get("role") in STAFF_ROLES
    if perms is not None:
        out["permissions"] = perms
    return out


async def _user_from_request(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        h = request.headers.get("Authorization", "")
        token = h[7:] if h.startswith("Bearer ") else None
    if not token:
        return None
    try:
        payload = jwt.decode(token, secret(), algorithms=[ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session")
    if payload.get("type") != "access":
        raise HTTPException(401, "Invalid session")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user or not user.get("active", True) or user.get("token_version", 0) != payload.get("tv"):
        raise HTTPException(401, "Session revoked")
    return user


async def get_current_user(request: Request) -> dict:
    user = await _user_from_request(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


async def optional_user(request: Request):
    try:
        return await _user_from_request(request)
    except HTTPException:
        return None


async def permissions_for(user: dict) -> list:
    if user.get("role") not in STAFF_ROLES:
        return []
    role = await db.roles.find_one({"key": user["role"]}, {"_id": 0})
    return role["permissions"] if role else []


def require_perm(perm: str):
    async def dep(user: dict = Depends(get_current_user)):
        if perm not in await permissions_for(user):
            raise HTTPException(403, "You do not have permission for this action")
        return user
    return dep


def require_staff():
    async def dep(user: dict = Depends(get_current_user)):
        if user.get("role") not in STAFF_ROLES:
            raise HTTPException(403, "Staff access required")
        return user
    return dep


async def seed_roles_and_admin():
    for key, (name, perms) in DEFAULT_ROLES.items():
        await db.roles.update_one({"key": key}, {"$setOnInsert": {"key": key, "name": name, "permissions": perms, "system": True}}, upsert=True)
    await db.roles.update_one({"key": "admin"}, {"$set": {"permissions": ALL_PERMISSIONS}})
    email = os.environ["ADMIN_EMAIL"].lower()
    pw = os.environ["ADMIN_PASSWORD"]
    existing = await db.users.find_one({"email": email})
    if not existing:
        await db.users.insert_one({"id": new_id(), "email": email, "password_hash": hash_password(pw), "name": "RAF DECOR Admin",
                                   "role": "admin", "active": True, "token_version": 0, "addresses": [], "wishlist": [], "created_at": now()})
    elif not verify_password(pw, existing["password_hash"]):
        await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_password(pw)}})

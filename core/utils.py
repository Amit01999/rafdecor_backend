import html
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import HTTPException, Request
from core.db import db


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def clean(doc):
    if doc:
        doc.pop("_id", None)
    return doc


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.UNICODE).strip()
    return re.sub(r"[\s_-]+", "-", s)[:90] or new_id()[:8]


def html_to_text(h: str) -> str:
    """Convert WordPress/WooCommerce HTML into readable plain text, preserving line breaks
    and list items instead of collapsing everything to a single line."""
    if not h:
        return ""
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", h, flags=re.S | re.I)
    h = re.sub(r"<li[^>]*>", "• ", h, flags=re.I)
    h = re.sub(r"<(br\s*/?|/p|/div|/h[1-6]|/li|/tr)\s*>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    lines, out = [ln.strip() for ln in h.splitlines()], []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def safe_regex(q: str):
    return {"$regex": re.escape(q.strip()[:80]), "$options": "i"}


def page_params(page: int, limit: int, max_limit: int = 100):
    page = max(1, page)
    limit = min(max(1, limit), max_limit)
    return (page - 1) * limit, limit


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


_hits = defaultdict(list)


def rate_limit(request: Request, bucket: str, limit: int, window: int = 60):
    key = f"{bucket}:{client_ip(request)}"
    t = time.time()
    _hits[key] = [x for x in _hits[key] if t - x < window]
    if len(_hits[key]) >= limit:
        raise HTTPException(429, "Too many requests. Please wait a moment and try again.")
    _hits[key].append(t)


async def audit(actor: dict, action: str, entity: str, entity_id: str, changes=None):
    await db.audit_logs.insert_one({
        "id": new_id(), "actor_id": actor.get("id"), "actor_email": actor.get("email"),
        "action": action, "entity": entity, "entity_id": entity_id,
        "changes": changes or {}, "created_at": now(),
    })


def diff(old: dict, new: dict, keys) -> dict:
    return {k: {"from": old.get(k), "to": new.get(k)} for k in keys if k in new and old.get(k) != new.get(k)}

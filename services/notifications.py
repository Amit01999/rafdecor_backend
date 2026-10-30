"""Notification channel abstraction. No email/SMS provider is configured, so messages are recorded with delivered=False and logged."""
import logging
from core.db import db
from core.utils import now, new_id

log = logging.getLogger("notifications")


def channel_configured() -> bool:
    return False


async def notify(kind: str, to: str, subject: str, body: str, ref=None) -> dict:
    doc = {"id": new_id(), "kind": kind, "to": to, "subject": subject, "body": body, "reference": ref,
           "channel": "log", "delivered": False, "created_at": now()}
    await db.notifications.insert_one(doc)
    log.info("[notification:%s] to=%s subject=%s (not delivered: no provider configured)", kind, "***" if to else None, subject)
    doc.pop("_id", None)
    return doc

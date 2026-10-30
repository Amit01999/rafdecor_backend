"""Payment provider abstraction. Checkout depends only on PaymentProvider; adding SSLCommerz = enabling SSLCommerzProvider."""
import os
from abc import ABC, abstractmethod
from fastapi import HTTPException
from core.db import db
from core.utils import now, new_id


class PaymentProvider(ABC):
    key: str
    label: str
    online: bool = False

    def configured(self) -> bool:
        return True

    @abstractmethod
    async def initiate(self, order: dict, settings: dict) -> dict:
        """Return initial payment state + customer instructions (or a redirect URL for online gateways)."""

    async def handle_callback(self, payload: dict) -> dict:
        raise HTTPException(400, "This payment method has no callbacks")

    async def verify(self, order: dict, reference: str) -> bool:
        return False


class CashOnDelivery(PaymentProvider):
    key, label = "cod", "Cash on delivery"

    async def initiate(self, order, settings):
        return {"status": "unpaid", "instructions": settings.get("payments", {}).get("cod_instructions", "Pay with cash upon delivery.")}


class BankTransfer(PaymentProvider):
    key, label = "bank_transfer", "Direct bank transfer"

    async def initiate(self, order, settings):
        cfg = settings.get("payments", {})
        return {"status": "awaiting_payment", "instructions": cfg.get("bank_instructions", ""), "bank_details": cfg.get("bank_details", "")}


class SSLCommerzProvider(PaymentProvider):
    """Integration point for SSLCommerz (not active until merchant credentials are configured and tested).
    Flow to implement: initiate -> POST {SSLCZ_API}/gwprocess/v4/api.php with store_id/passwd, tran_id=order_number,
    success/fail/cancel/ipn URLs pointing to /api/v1/payments/sslcommerz/{success|fail|cancel|ipn}; redirect user to GatewayPageURL.
    IPN/success: server-side validate via {SSLCZ_API}/validator/api/validationserverAPI.php?val_id=...; check amount/currency/tran_id;
    record payment_transactions with unique (provider, reference=val_id) for idempotency; only then mark order paid."""
    key, label, online = "sslcommerz", "SSLCommerz (cards, bKash, Nagad)", True

    def configured(self):
        return bool(os.environ.get("SSLCZ_STORE_ID") and os.environ.get("SSLCZ_STORE_PASSWORD"))

    async def initiate(self, order, settings):
        raise HTTPException(503, "SSLCommerz is not configured yet")

    async def handle_callback(self, payload):
        raise HTTPException(503, "SSLCommerz is not configured yet")


PROVIDERS = {p.key: p for p in (CashOnDelivery(), BankTransfer(), SSLCommerzProvider())}


def enabled_methods(settings: dict) -> list:
    flags = settings.get("payments", {}).get("enabled", {"cod": True, "bank_transfer": True})
    return [{"key": k, "label": p.label, "online": p.online} for k, p in PROVIDERS.items() if flags.get(k) and p.configured()]


def get_provider(key: str, settings: dict) -> PaymentProvider:
    if key not in [m["key"] for m in enabled_methods(settings)]:
        raise HTTPException(400, "Selected payment method is not available")
    return PROVIDERS[key]


async def record_transaction(order: dict, provider: str, kind: str, amount: float, reference=None, actor=None, note=None):
    doc = {"id": new_id(), "order_id": order["id"], "order_number": order["order_number"], "provider": provider, "type": kind,
           "amount": amount, "reference": reference or None, "note": note, "actor_email": (actor or {}).get("email"), "created_at": now()}
    if not doc["reference"]:
        doc.pop("reference")
    await db.payment_transactions.insert_one(doc)
    doc.pop("_id", None)
    return doc


def payment_status(total: float, paid: float, refunded: float, base: str) -> str:
    if refunded > 0 and refunded >= paid and paid > 0:
        return "refunded"
    if refunded > 0:
        return "partially_refunded"
    if paid >= total and total > 0:
        return "paid"
    if paid > 0:
        return "partially_paid"
    return base

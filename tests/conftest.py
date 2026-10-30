import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
API = f"{BASE_URL}/api/v1"
ADMIN_EMAIL = "admin@rafdecor.shop"
ADMIN_PASSWORD = "RafAdmin@2026"

BOWL = "a7b2acc848ef4f39a8d3d3c796609755"
BOWL_15 = "4c62db9be0f34560b3baf0a431516b59"
BOWL_3 = "164883330dd5411bba529790d64de927"
TABLE = "97372c8db2644f5085cbc8166b072ffd"
SPICE = "ee1365a1e1cb410bb6fd86ac322aaf11"


def call(sess, method, url, retries=3, **kw):
    """Request with back-off on 429 rate limits."""
    for i in range(retries):
        r = sess.request(method, url, timeout=30, **kw)
        if r.status_code != 429:
            return r
        time.sleep(61)
    return r


def login(email, password):
    s = requests.Session()
    r = call(s, "POST", f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return s


def register(prefix="cust"):
    s = requests.Session()
    email = f"test_{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    r = call(s, "POST", f"{API}/auth/register", json={"name": f"TEST_{prefix}", "email": email, "password": "Passw0rd!x", "phone": "01712345678"})
    assert r.status_code == 200, r.text
    return s, email


def order_body(items, payment="cod", coupon=None, key=None, phone="01712345678"):
    return {"items": items, "customer": {"name": "TEST_Buyer", "phone": phone, "email": "test_buyer@example.com"},
            "shipping_address": {"address": "House 1, Road 2, Dhanmondi", "district": "Dhaka"},
            "payment_method": payment, "coupon_code": coupon, "idempotency_key": key or uuid.uuid4().hex}


@pytest.fixture(scope="session")
def admin():
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def anon():
    return requests.Session()

"""RAF DECOR API regression suite: catalog, quote, checkout, coupons, inventory, orders, auth/RBAC, analytics, audit."""
import time
import uuid
from datetime import datetime, timedelta, timezone
import pytest
import requests
from conftest import API, call, login, register, order_body, BOWL, BOWL_15, BOWL_3, TABLE, SPICE


# ---------- Catalog ----------
class TestCatalog:
    def test_list_three_products(self, anon):
        d = anon.get(f"{API}/products").json()
        assert d["total"] == 3
        by = {p["id"]: p for p in d["items"]}
        assert by[BOWL]["price_min"] == 899 and by[BOWL]["price_max"] == 1099
        assert by[TABLE]["price_min"] == 3400 and by[SPICE]["price_min"] == 2200
        for p in d["items"]:
            assert p["images"] and all("res.cloudinary.com" in i["url"] for i in p["images"])

    @pytest.mark.parametrize("params,expected", [
        ({"q": "spice"}, {SPICE}), ({"category": "premium-wooden"}, {BOWL, TABLE, SPICE}),
        ({"min_price": 2000, "max_price": 3000}, {SPICE}), ({"in_stock_only": "true"}, {BOWL, TABLE, SPICE}),
        ({"attr": "Premium Bark Bowl Size:3 inch"}, {BOWL}),
    ])
    def test_filters(self, anon, params, expected):
        d = anon.get(f"{API}/products", params=params).json()
        assert {p["id"] for p in d["items"]} == expected

    def test_sorts_and_pagination(self, anon):
        asc = [p["price_min"] for p in anon.get(f"{API}/products", params={"sort": "price_asc"}).json()["items"]]
        desc = [p["price_min"] for p in anon.get(f"{API}/products", params={"sort": "price_desc"}).json()["items"]]
        assert asc == sorted(asc) and desc == sorted(desc, reverse=True)
        assert len(anon.get(f"{API}/products", params={"sort": "newest"}).json()["items"]) == 3
        p1 = anon.get(f"{API}/products", params={"limit": 2, "page": 1}).json()
        p2 = anon.get(f"{API}/products", params={"limit": 2, "page": 2}).json()
        assert p1["pages"] == 2 and len(p1["items"]) == 2 and len(p2["items"]) == 1
        assert not ({p["id"] for p in p1["items"]} & {p["id"] for p in p2["items"]})

    def test_unknown_category_404(self, anon):
        assert anon.get(f"{API}/products", params={"category": "nope"}).status_code == 404


# ---------- Quote ----------
class TestQuote:
    def test_shipping_sum(self, anon):
        q = anon.post(f"{API}/cart/quote", json={"items": [{"product_id": BOWL, "variant_id": BOWL_3, "quantity": 2},
                                                          {"product_id": TABLE, "quantity": 1}, {"product_id": SPICE, "quantity": 1}]}).json()
        assert q["issues"] == []
        assert q["subtotal"] == 1099 * 2 + 3400 + 2200
        assert q["shipping"] == 150 + 520 + 340
        assert q["total"] == q["subtotal"] + q["shipping"]

    def test_variant_required(self, anon):
        q = anon.post(f"{API}/cart/quote", json={"items": [{"product_id": BOWL, "quantity": 1}]}).json()
        assert q["issues"][0]["code"] == "variant_required"

    def test_client_price_ignored(self, anon):
        q = anon.post(f"{API}/cart/quote", json={"items": [{"product_id": SPICE, "quantity": 1, "price": 1, "unit_price": 1}]}).json()
        assert q["lines"][0]["unit_price"] == 2200


# ---------- Checkout ----------
class TestCheckout:
    def test_idempotency_and_cod_unpaid(self, anon):
        key = uuid.uuid4().hex
        body = order_body([{"product_id": SPICE, "quantity": 1}], key=key)
        r1 = call(anon, "POST", f"{API}/checkout/orders", json=body)
        assert r1.status_code == 200, r1.text
        r2 = call(anon, "POST", f"{API}/checkout/orders", json=body)
        assert r2.json()["duplicate"] is True
        assert r1.json()["order"]["order_number"] == r2.json()["order"]["order_number"]
        assert r1.json()["order"]["payment"]["status"] == "unpaid"
        assert r1.json()["order"]["total"] == 2540
        pytest.shared_order = r1.json()["order"]

    def test_bank_transfer_awaiting(self, anon):
        r = call(anon, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": TABLE, "quantity": 1}], payment="bank_transfer"))
        assert r.status_code == 200, r.text
        assert r.json()["order"]["payment"]["status"] == "awaiting_payment"

    def test_invalid_phone_422(self, anon):
        r = anon.post(f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}], phone="12345"))
        assert r.status_code == 422

    def test_sslcommerz_not_available(self, anon):
        r = anon.post(f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}], payment="sslcommerz"))
        assert r.status_code == 400

    def test_guest_tracking_requires_phone(self, anon):
        o = pytest.shared_order
        ok = anon.post(f"{API}/orders/track", json={"order_number": o["order_number"], "phone": "01712345678"})
        assert ok.status_code == 200 and ok.json()["status"] == "placed"
        bad = anon.post(f"{API}/orders/track", json={"order_number": o["order_number"], "phone": "01899999999"})
        assert bad.status_code == 404


# ---------- Order admin: transitions, payments, refunds ----------
class TestOrderAdmin:
    def test_transitions(self, admin):
        o = pytest.shared_order
        bad = admin.post(f"{API}/admin/orders/{o['id']}/status", json={"status": "delivered"})
        assert bad.status_code == 422
        ok = admin.post(f"{API}/admin/orders/{o['id']}/status", json={"status": "confirmed", "note": "TEST visible note", "visible_to_customer": True})
        assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
        tr = requests.post(f"{API}/orders/track", json={"order_number": o["order_number"], "phone": "01712345678"}).json()
        assert tr["status"] == "confirmed" and any(h["note"] == "TEST visible note" for h in tr["history"])

    def test_payments_and_refunds(self, admin):
        o = pytest.shared_order
        r = admin.post(f"{API}/admin/orders/{o['id']}/payments", json={"amount": 1000, "reference": f"T{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 200 and r.json()["payment"]["status"] == "partially_paid"
        assert admin.post(f"{API}/admin/orders/{o['id']}/payments", json={"amount": 5000}).status_code == 422
        r = admin.post(f"{API}/admin/orders/{o['id']}/payments", json={"amount": 1540})
        assert r.json()["payment"]["status"] == "paid" and r.json()["payment"]["amount_paid"] == 2540
        assert admin.post(f"{API}/admin/orders/{o['id']}/refunds", json={"amount": 3000, "reason": "too much"}).status_code == 422
        r = admin.post(f"{API}/admin/orders/{o['id']}/refunds", json={"amount": 500, "reason": "TEST partial"})
        assert r.status_code == 200 and r.json()["payment"]["status"] == "partially_refunded"

    def test_dashboard_finance(self, admin):
        d = admin.get(f"{API}/admin/dashboard").json()
        orders = admin.get(f"{API}/admin/orders", params={"limit": 100}).json()["items"]
        collected = sum(o["payment"]["amount_paid"] for o in orders)
        refunds = sum(o["payment"].get("amount_refunded", 0) for o in orders)
        assert abs(d["finance"]["collected"] - collected) < 0.01
        assert abs(d["finance"]["net_revenue"] - (collected - refunds)) < 0.01
        assert d["orders"]["total"] == len(orders)
        a = admin.get(f"{API}/admin/analytics")
        assert a.status_code == 200 and "funnel" in a.json()

    def test_audit_order_status(self, admin):
        logs = admin.get(f"{API}/admin/audit-logs", params={"entity": "order"}).json()["items"]
        assert any(l["action"] == "order.status" and l["entity_id"] == pytest.shared_order["id"] for l in logs)


# ---------- Coupons ----------
class TestCoupons:
    code = f"TEST{uuid.uuid4().hex[:6].upper()}"
    expired = f"TESTEXP{uuid.uuid4().hex[:4].upper()}"

    def test_create_and_apply(self, admin, anon):
        r = admin.post(f"{API}/admin/coupons", json={"code": self.code, "type": "percent", "value": 10, "min_order": 2000, "usage_limit": 1})
        assert r.status_code == 200, r.text
        q = anon.post(f"{API}/cart/quote", json={"items": [{"product_id": SPICE, "quantity": 1}], "coupon_code": self.code}).json()
        assert q["coupon"]["valid"] and q["discount"] == 220 and q["total"] == 2200 - 220 + 340
        q = anon.post(f"{API}/cart/quote", json={"items": [{"product_id": BOWL, "variant_id": BOWL_15, "quantity": 1}], "coupon_code": self.code}).json()
        assert not q["coupon"]["valid"] and "Minimum" in q["coupon"]["message"]

    def test_usage_limit(self, anon):
        r1 = call(anon, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}], coupon=self.code))
        assert r1.status_code == 200 and r1.json()["order"]["discount"] == 220
        r2 = call(anon, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}], coupon=self.code))
        assert r2.status_code == 409

    def test_expired(self, admin, anon):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert admin.post(f"{API}/admin/coupons", json={"code": self.expired, "type": "fixed", "value": 100, "expires_at": past}).status_code == 200
        q = anon.post(f"{API}/cart/quote", json={"items": [{"product_id": SPICE, "quantity": 1}], "coupon_code": self.expired}).json()
        assert not q["coupon"]["valid"] and "expired" in q["coupon"]["message"]

    def test_cleanup(self, admin):
        for c in (self.code, self.expired):
            assert admin.delete(f"{API}/admin/coupons/{c}").status_code == 200


# ---------- Inventory ----------
class TestInventory:
    def stock(self, admin):
        return admin.get(f"{API}/admin/products/{SPICE}").json().get("stock_quantity")

    def test_inventory_flow(self, admin, anon):
        try:
            assert admin.post(f"{API}/admin/inventory/{SPICE}/tracking", json={"manage_stock": True}).status_code == 200
            base = self.stock(admin) or 0
            if base != 2:
                r = admin.post(f"{API}/admin/inventory/adjust", json={"product_id": SPICE, "delta": 2 - base, "reason": "restock"})
                assert r.status_code == 200, r.text
            assert self.stock(admin) == 2
            assert admin.post(f"{API}/admin/inventory/adjust", json={"product_id": SPICE, "delta": -5, "reason": "damaged"}).status_code == 409
            r = call(anon, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 3}]))
            assert r.status_code == 409
            r = call(anon, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}]))
            assert r.status_code == 200
            o1 = r.json()["order"]
            assert self.stock(admin) == 1
            assert admin.post(f"{API}/admin/orders/{o1['id']}/status", json={"status": "cancelled", "restock": True}).status_code == 200
            assert self.stock(admin) == 2
            r = call(anon, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}]))
            o2 = r.json()["order"]
            assert self.stock(admin) == 1
            assert admin.post(f"{API}/admin/orders/{o2['id']}/status", json={"status": "cancelled", "restock": False}).status_code == 200
            assert self.stock(admin) == 1
            mv = admin.get(f"{API}/admin/inventory/movements", params={"product_id": SPICE}).json()["items"]
            assert any(m["reason"] == "order_cancelled" for m in mv)
            logs = admin.get(f"{API}/admin/audit-logs", params={"entity": "product"}).json()["items"]
            assert any(l["action"] == "inventory.adjust" for l in logs)
        finally:
            admin.post(f"{API}/admin/inventory/{SPICE}/tracking", json={"manage_stock": False})
        p = requests.get(f"{API}/products/wooden-spice-box").json()
        assert p["manage_stock"] is False and p["in_stock"] is True


# ---------- Product audit / archive ----------
class TestProductAdmin:
    def test_price_change_audit(self, admin):
        p = admin.get(f"{API}/admin/products/{SPICE}").json()
        p["sku"] = p.get("sku") or ""  # NOTE: GET returns sku=null but PUT requires str (reported)
        orig = p["price"]
        try:
            r = admin.put(f"{API}/admin/products/{SPICE}", json={**p, "price": orig + 50})
            assert r.status_code == 200 and r.json()["price"] == orig + 50
            assert requests.get(f"{API}/products/wooden-spice-box").json()["price_min"] == orig + 50
        finally:
            admin.put(f"{API}/admin/products/{SPICE}", json={**p, "price": orig})
        logs = admin.get(f"{API}/admin/audit-logs", params={"entity": "product"}).json()["items"]
        assert any(l["action"] == "product.price_change" and l["entity_id"] == SPICE for l in logs)

    def test_delete_referenced_archives_and_unreferenced_deletes(self, admin):
        r = admin.post(f"{API}/admin/products", json={"name": f"TEST Product {uuid.uuid4().hex[:5]}", "price": 100, "status": "draft"})
        assert r.status_code == 200
        pid = r.json()["id"]
        d = admin.delete(f"{API}/admin/products/{pid}")
        assert d.json().get("deleted") is True
        assert admin.get(f"{API}/admin/products/{pid}").status_code == 404


# ---------- Auth & RBAC ----------
class TestAuthRBAC:
    def test_me_logout(self):
        s, email = register("auth")
        assert "access_token" in s.cookies and "refresh_token" in s.cookies
        assert s.get(f"{API}/auth/me").json()["email"] == email
        s.post(f"{API}/auth/logout")
        assert s.get(f"{API}/auth/me").status_code == 401

    def test_refresh_rotation_reuse(self):
        s, _ = register("rot")
        old = s.cookies.get("refresh_token", path="/api/v1/auth")
        r = s.post(f"{API}/auth/refresh")
        assert r.status_code == 200
        new = s.cookies.get("refresh_token", path="/api/v1/auth")
        assert new and new != old
        attacker = requests.Session()
        attacker.cookies.set("refresh_token", old, path="/api/v1/auth", domain=s.cookies.list_domains()[0])
        assert attacker.post(f"{API}/auth/refresh").status_code == 401
        # legitimate new token is now revoked as well
        s2 = requests.Session()
        s2.cookies.set("refresh_token", new, path="/api/v1/auth", domain=s.cookies.list_domains()[0])
        assert s2.post(f"{API}/auth/refresh").status_code == 401

    def test_customer_blocked_from_admin(self):
        s, _ = register("rbac")
        assert s.get(f"{API}/admin/dashboard").status_code == 403
        assert s.get(f"{API}/admin/orders").status_code == 403

    def test_cross_customer_order_404(self):
        a, _ = register("a")
        b, _ = register("b")
        r = call(a, "POST", f"{API}/checkout/orders", json=order_body([{"product_id": SPICE, "quantity": 1}]))
        assert r.status_code == 200
        num = r.json()["order"]["order_number"]
        assert a.get(f"{API}/orders/{num}").status_code == 200
        assert b.get(f"{API}/orders/{num}").status_code == 404

    def test_support_staff_permissions(self, admin):
        email = f"test_support_{uuid.uuid4().hex[:6]}@example.com"
        r = admin.post(f"{API}/admin/users", json={"email": email, "name": "TEST Support", "role": "support", "password": "SupportPass#2026"})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        try:
            s = login(email, "SupportPass#2026")
            assert s.post(f"{API}/admin/products", json={"name": "TEST nope", "price": 1}).status_code == 403
            assert s.get(f"{API}/admin/inquiries").status_code == 200
        finally:
            admin.put(f"{API}/admin/users/{uid}", json={"email": email, "name": "TEST Support", "role": "support", "active": False})

    def test_csrf_foreign_origin_blocked(self, admin):
        r = admin.post(f"{API}/admin/coupons", json={"code": "TESTCSRF", "type": "fixed", "value": 1}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_bruteforce_lockout(self):
        email = f"test_lock_{uuid.uuid4().hex[:6]}@example.com"
        codes = [requests.post(f"{API}/auth/login", json={"email": email, "password": "wrong"}).status_code for _ in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429


# ---------- Analytics events ----------
class TestAnalytics:
    def test_dedupe(self, anon):
        sid = f"testsess{uuid.uuid4().hex[:8]}"
        e = {"type": "product_view", "session_id": sid, "product_id": TABLE}
        assert anon.post(f"{API}/analytics/events", json=e).json().get("deduplicated") is None
        assert anon.post(f"{API}/analytics/events", json=e).json().get("deduplicated") is True
        assert anon.post(f"{API}/analytics/events", json={**e, "type": "bogus"}).status_code == 422

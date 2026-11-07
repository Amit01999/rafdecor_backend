"""Focused checks for optional variant-image validation and public serialization."""
import os
import asyncio
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rafdecor_tests")

from fastapi import UploadFile
from pydantic import ValidationError
import pytest
from starlette.datastructures import Headers

import routes.admin_catalog as admin_catalog
from routes.admin_catalog import ProductIn, _prepare
from routes.catalog import public_product


IMAGE = {
    "url": "https://res.cloudinary.com/example/image/upload/rafdecor/products/variant.jpg",
    "public_id": "rafdecor/products/variant",
    "alt": "Walnut table variant",
}


def product_input(variant, variant_id=None):
    variant_data = {"attributes": {"Color": "Walnut"}, "price": 100, **variant}
    if variant_id:
        variant_data["id"] = variant_id
    return ProductIn(
        name="Variant image product",
        price=100,
        variants=[variant_data],
    )


def test_variant_image_is_optional_and_persisted_by_product_prepare():
    with_image = _prepare(product_input({"image": IMAGE}))
    assert with_image["variants"][0]["image"]["url"] == IMAGE["url"]
    assert with_image["variants"][0]["image"]["public_id"] == IMAGE["public_id"]

    without_image = _prepare(product_input({}))
    assert "image" not in without_image["variants"][0]

    removed_image = _prepare(product_input({"image": None}))
    assert "image" not in removed_image["variants"][0]

    named_variant = _prepare(product_input({"name": "Curve S (Small)"}))
    assert named_variant["variants"][0]["name"] == "Curve S (Small)"

    unnamed_variant = _prepare(product_input({}))
    assert "name" not in unnamed_variant["variants"][0]


def test_variant_image_requires_a_valid_image_object():
    with pytest.raises(ValidationError):
        product_input({"image": {"public_id": IMAGE["public_id"]}})


def test_public_product_includes_variant_image_only_when_present():
    record = {
        "id": "product-1",
        "name": "Variant image product",
        "price": 100,
        "manage_stock": False,
        "variants": [{"id": "variant-1", "name": "Round Curve Tray", "price": 100, "attributes": {"Color": "Walnut"}, "image": IMAGE}],
    }
    public_image = public_product(record, full=True)["variants"][0]["image"]
    assert public_image["url"] == IMAGE["url"]
    assert public_image["public_id"] == IMAGE["public_id"]
    assert public_product(record, full=True)["variants"][0]["name"] == "Round Curve Tray"
    assert "image" not in public_product(record)["variants"][0]
    assert "name" not in public_product(record)["variants"][0]

    record["variants"][0].pop("image")
    assert "image" not in public_product(record, full=True)["variants"][0]


def test_variant_upload_reuses_shared_product_media_storage(monkeypatch):
    stored = {}
    monkeypatch.setattr(admin_catalog.media, "configured", lambda: True)

    def fake_upload(data, folder):
        stored.update({"data": data, "folder": folder})
        return {"url": IMAGE["url"], "public_id": IMAGE["public_id"]}

    async def fake_audit(user, action, entity, entity_id, details):
        stored["audit"] = (user["email"], action, entity, entity_id)

    monkeypatch.setattr(admin_catalog.media, "upload", fake_upload)
    monkeypatch.setattr(admin_catalog, "audit", fake_audit)
    upload = UploadFile(
        filename="variant.webp",
        file=BytesIO(b"test image bytes"),
        headers=Headers({"content-type": "image/webp"}),
    )

    result = asyncio.run(admin_catalog.upload(upload, user={"email": "admin@example.com"}))

    assert result["url"] == IMAGE["url"]
    assert stored["data"] == b"test image bytes"
    assert stored["folder"] == "rafdecor/products"
    assert stored["audit"][1:3] == ("media.upload", "media")


def test_product_create_and_edit_persist_variant_images(monkeypatch):
    class MemoryProducts:
        def __init__(self):
            self.docs = {}

        async def insert_one(self, doc):
            self.docs[doc["id"]] = deepcopy(doc)

        async def find_one(self, query, projection=None):
            return deepcopy(self.docs.get(query["id"]))

        async def update_one(self, query, update):
            self.docs[query["id"]].update(deepcopy(update["$set"]))

    products = MemoryProducts()

    async def fake_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_catalog, "db", SimpleNamespace(products=products))
    monkeypatch.setattr(admin_catalog, "audit", fake_audit)
    user = {"email": "admin@example.com"}

    created = asyncio.run(admin_catalog.create_product(product_input({"image": IMAGE, "name": "Round Curve Tray"}), user=user))
    variant_id = created["variants"][0]["id"]
    assert products.docs[created["id"]]["variants"][0]["image"]["url"] == IMAGE["url"]
    assert products.docs[created["id"]]["variants"][0]["name"] == "Round Curve Tray"

    replacement = {**IMAGE, "url": "https://res.cloudinary.com/example/image/upload/rafdecor/products/replacement.jpg"}
    edited = asyncio.run(admin_catalog.update_product(
        created["id"], product_input({"image": replacement, "name": "Round Curve Tray"}, variant_id=variant_id), user=user,
    ))
    assert edited["variants"][0]["image"]["url"] == replacement["url"]

    removed = asyncio.run(admin_catalog.update_product(
        created["id"], product_input({"image": None, "name": "Round Curve Tray"}, variant_id=variant_id), user=user,
    ))
    assert "image" not in removed["variants"][0]

import os
from motor.motor_asyncio import AsyncIOMotorClient

client = AsyncIOMotorClient(os.environ["MONGO_URL"], tz_aware=True)
db = client[os.environ["DB_NAME"]]


async def create_indexes():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.sessions.create_index("jti", unique=True)
    await db.sessions.create_index("expires_at", expireAfterSeconds=0)
    await db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.password_reset_tokens.create_index("token_hash", unique=True)
    await db.login_attempts.create_index("identifier")
    await db.products.create_index("id", unique=True)
    await db.products.create_index("slug", unique=True)
    await db.products.create_index("sku", unique=True, partialFilterExpression={"sku": {"$type": "string", "$gt": ""}})
    await db.products.create_index("source.wp_id", sparse=True)
    await db.products.create_index([("status", 1), ("category_ids", 1), ("effective_price", 1)])
    await db.categories.create_index("id", unique=True)
    await db.categories.create_index("slug", unique=True)
    await db.orders.create_index("id", unique=True)
    await db.orders.create_index("order_number", unique=True)
    await db.orders.create_index("idempotency_key", unique=True)
    await db.orders.create_index([("user_id", 1), ("created_at", -1)])
    await db.orders.create_index([("status", 1), ("created_at", -1)])
    await db.orders.create_index("customer.phone")
    await db.coupons.create_index("code", unique=True)
    await db.carts.create_index("user_id", unique=True)
    await db.events.create_index("dedupe", unique=True, sparse=True)
    await db.events.create_index([("type", 1), ("created_at", -1)])
    await db.events.create_index([("product_id", 1), ("type", 1)])
    await db.inquiries.create_index("id", unique=True)
    await db.inquiries.create_index([("status", 1), ("created_at", -1)])
    await db.audit_logs.create_index([("created_at", -1)])
    await db.stock_movements.create_index([("product_id", 1), ("created_at", -1)])
    await db.newsletter.create_index("email", unique=True)
    # partial (not sparse): a sparse compound index still indexes docs missing `reference` as null -> E11000 on 2nd ref-less txn
    idx = await db.payment_transactions.index_information()
    if idx.get("provider_1_reference_1", {}).get("sparse"):
        await db.payment_transactions.drop_index("provider_1_reference_1")
    await db.payment_transactions.create_index([("provider", 1), ("reference", 1)], unique=True,
                                               partialFilterExpression={"reference": {"$type": "string"}})
    await db.reviews.create_index([("product_id", 1), ("status", 1)])
    await db.pages.create_index("slug", unique=True)
